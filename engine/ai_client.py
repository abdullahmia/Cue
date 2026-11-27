"""Multi-provider AI client supporting OpenRouter (:free), OpenAI, Gemini, Claude, Groq, DeepSeek, and Ollama."""

from __future__ import annotations

import http.client
import io
import json
import os
import platform
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    AI_PROVIDERS,
    DEFAULT_FREE_MODELS,
    DEFAULT_MODEL_CACHE_TTL,
    append_log,
    atomic_write_json,
    ensure_cue_dirs,
    get_active_provider,
    get_configured_providers,
    get_ollama_base_url,
    get_provider_api_key,
    get_provider_meta,
    get_provider_preferred_model,
    load_config,
    load_state,
    normalize_provider,
    provider_is_configured,
    read_json_safe,
    save_state,
)

OPENROUTER_BASE_URL = AI_PROVIDERS["openrouter"]["base_url"]
APP_TITLE = "cue-zsh"
APP_REFERER = "https://github.com/abdullahmia/Cue"

SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)

ANTHROPIC_VERSION = "2023-06-01"

TRIVIAL_FILLER_CMDS = {"ls", "ls -la", "ls -l", "pwd", "clear", "cd", "cd ..", "whoami", "date"}
UNSAFE_CMD_PATTERNS = (
    re.compile(r"(^|\s)rm\s+-rf\s+(/|\~|\$HOME)(\s|$)"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r"(^|\s)mkfs\.[a-z0-9]+"),
)
PLACEHOLDER_RE = re.compile(r"<[a-zA-Z0-9_-]{2,24}>")


def _build_ssl_context() -> ssl.SSLContext:
    """Create a verified SSLContext, falling back to system CA bundles on macOS python.org builds."""
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and os.path.exists(defaults.cafile):
        return ssl.create_default_context()
    if defaults.capath and os.path.isdir(defaults.capath):
        return ssl.create_default_context()

    try:
        import certifi  # type: ignore

        cpath = certifi.where()
        if cpath and os.path.exists(cpath):
            return ssl.create_default_context(cafile=cpath)
    except ImportError:
        pass

    for bundle in SYSTEM_CA_BUNDLES:
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)

    return ssl.create_default_context()


class OpenRouterError(Exception):
    """Base error from AI provider client."""


AIProviderError = OpenRouterError


class AuthError(OpenRouterError):
    """Raised when the provider API key is missing or rejected (401/403)."""


class AllModelsExhaustedError(OpenRouterError):
    """Raised when every candidate model/provider failed or is rate-limited."""


def is_free_model(model_id: str, model_meta: dict[str, Any] | None = None) -> bool:
    """Strictly validate that a model is an OpenRouter free model."""
    if not model_id or not isinstance(model_id, str):
        return False
    mid = model_id.strip()
    if mid == "openrouter/free" or mid.endswith(":free"):
        if model_meta and isinstance(model_meta.get("pricing"), dict):
            pricing = model_meta["pricing"]
            prompt_cost = str(pricing.get("prompt", "0"))
            comp_cost = str(pricing.get("completion", "0"))
            try:
                if float(prompt_cost) > 0 or float(comp_cost) > 0:
                    return False
            except ValueError:
                return False
        return True
    return False


def _is_valid_provider_model(provider_id: str, model_id: str, model_meta: dict[str, Any] | None = None) -> bool:
    """Validate whether a model ID is eligible for the given provider."""
    if not model_id or not isinstance(model_id, str):
        return False
    pid = normalize_provider(provider_id)
    if pid == "openrouter":
        return is_free_model(model_id, model_meta)
    low = model_id.strip().lower()
    if not low:
        return False
    if pid == "openai":
        blocked = ("whisper", "tts", "dall-e", "embedding", "moderation", "sora", "realtime", "audio", "transcribe", "image", "babbage", "davinci")
        return not any(tok in low for tok in blocked) and (low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")))
    if pid == "gemini":
        blocked = ("embedding", "imagen", "veo", "aqa", "tts", "robotics", "native-audio", "live-")
        return not any(tok in low for tok in blocked) and "gemini" in low
    if pid == "anthropic":
        return low.startswith("claude-")
    if pid == "groq":
        blocked = ("whisper", "playai", "guard", "tts", "orpheus")
        return not any(tok in low for tok in blocked)
    return True


def _recover_truncated_suggestions(cleaned: str) -> dict[str, Any] | None:
    """Recover valid suggestions and summary even if JSON was truncated near max_tokens."""
    if '"suggestions"' not in cleaned:
        return None
    items: list[dict[str, str]] = []
    for m in re.finditer(r"\{[^{}]*\}", cleaned):
        chunk = m.group(0)
        if '"cmd"' not in chunk:
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("cmd"), str):
            items.append(
                {
                    "cmd": obj["cmd"],
                    "reason": str(obj.get("reason", "")),
                }
            )
    if not items:
        return None
    summary = ""
    sum_m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned)
    if sum_m:
        try:
            summary = json.loads(f'"{sum_m.group(1)}"')
        except json.JSONDecodeError:
            summary = sum_m.group(1)
    return {"suggestions": items, "summary": summary}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse JSON from model output, tolerating thinking blocks, fences, prose, or tail truncation."""
    cleaned = text.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    if "<think>" in cleaned and "</think>" not in cleaned:
        brace_pos = cleaned.find('{"suggestions"')
        if brace_pos != -1:
            cleaned = cleaned[brace_pos:]

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return _recover_truncated_suggestions(cleaned)


def _fold_system_into_user(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge system instructions into the first user message for models without system-role support."""
    sys_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    non_sys = [dict(m) for m in messages if m.get("role") != "system"]
    if not sys_parts:
        return non_sys
    prefix = "Instructions:\n" + "\n".join(sys_parts) + "\n\nRequest:\n"
    for msg in non_sys:
        if msg.get("role") == "user":
            msg["content"] = prefix + msg.get("content", "")
            return non_sys
    return [{"role": "user", "content": prefix}]


def _clean_cmd_text(raw: str) -> str:
    cmd = raw.strip()
    if cmd.startswith("```") and cmd.endswith("```"):
        cmd = cmd.strip("`").strip()
    elif cmd.startswith("`") and cmd.endswith("`") and cmd.count("`") == 2:
        cmd = cmd[1:-1].strip()
    cmd = re.sub(r"^\d+\.\s+", "", cmd)
    if cmd.startswith("$ "):
        cmd = cmd[2:].strip()
    return cmd


def _sanitize_suggestions(
    raw_items: list[Any],
    partial_input: str = "",
    mode: str = "analyze",
) -> list[dict[str, str]]:
    """Filter unsafe, placeholder, duplicate, or off-topic suggestions cleanly."""
    query = partial_input.strip().lower()
    seen: set[str] = set()
    cleaned: list[dict[str, str]] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        cmd = _clean_cmd_text(str(item.get("cmd", "")))
        reason = " ".join(str(item.get("reason", "")).strip().split())
        if not cmd or "\n" in cmd or len(cmd) > 220:
            continue
        if any(p.search(cmd) for p in UNSAFE_CMD_PATTERNS):
            continue
        if "<" not in query and PLACEHOLDER_RE.search(cmd):
            continue
        norm_key = " ".join(cmd.lower().split())
        if norm_key in seen:
            continue
        if query and norm_key == query:
            continue
        if mode in ("analyze", "complete") and norm_key in TRIVIAL_FILLER_CMDS:
            if not query or not norm_key.startswith(query):
                continue
        seen.add(norm_key)
        cleaned.append({"cmd": cmd, "reason": reason[:90] or "Project command"})

    if mode == "complete" and query:
        tokens = [t for t in query.split() if t]

        def _rel_key(entry: dict[str, str]) -> tuple[int, int]:
            lc = entry["cmd"].lower()
            if lc.startswith(query):
                return (0, len(lc))
            if query in lc:
                return (1, len(lc))
            pos = 0
            for tok in tokens:
                idx = lc.find(tok, pos)
                if idx == -1:
                    return (3, len(lc))
                pos = idx + len(tok)
            return (2, len(lc))

        cleaned.sort(key=_rel_key)

    return cleaned[:12]


def _health_key(provider_id: str, model_id: str) -> str:
    """Return canonical health-tracking key; OpenRouter keeps bare ID for compatibility."""
    pid = normalize_provider(provider_id)
    return model_id if pid == "openrouter" else f"{pid}:{model_id}"


def _uses_openai_reasoning_params(model_id: str) -> bool:
    low = model_id.strip().lower()
    return low.startswith(("o1", "o3", "o4", "gpt-5"))


class AIClient:
    """Unified multi-provider AI client with model/provider fallback, health cooldowns, and keep-alive."""

    def __init__(self, home: Path | None = None, config: dict[str, Any] | None = None) -> None:
        self.paths = ensure_cue_dirs(home)
        self.home = self.paths["root"]
        self.config = config or load_config(self.home)
        self._ssl_context = _build_ssl_context()
        self._tls = threading.local()
        self._health_lock = threading.Lock()

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": APP_REFERER,
            "X-Title": APP_TITLE,
            "User-Agent": f"cue-zsh/{__version__}",
        }

    def _provider_base_url(self, provider_id: str) -> str:
        pid = normalize_provider(provider_id)
        if pid == "ollama":
            return get_ollama_base_url(self.config)
        return str(get_provider_meta(pid)["base_url"]).rstrip("/")

    def _http_get_json(self, url: str, headers: dict[str, str], timeout: float) -> Any:
        req = urllib.request.Request(url, headers=headers, method="GET")
        ctx = self._ssl_context if url.startswith("https://") else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def validate_key(
        self,
        api_key: str,
        timeout: float = 12.0,
        provider_id: str | None = None,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Verify provider credentials (or local Ollama daemon) and discover available models."""
        pid = normalize_provider(provider_id or get_active_provider(self.config))
        meta = get_provider_meta(pid)
        key = api_key.strip()

        if pid == "ollama":
            models = self.fetch_models("ollama", force_refresh=True, api_key="", timeout=timeout)
            base_url = self._provider_base_url("ollama")
            try:
                raw = self._http_get_json(f"{base_url}/api/tags", {"User-Agent": f"cue-zsh/{__version__}"}, timeout)
                count = len(raw.get("models", [])) if isinstance(raw, dict) else len(models)
                if count == 0:
                    return True, f"Ollama reachable at {base_url} (pull a model e.g. `ollama pull qwen2.5-coder:7b`).", models
                return True, f"Connected to Ollama! Found {count} local model(s).", models
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
                return False, f"Cannot reach Ollama at {base_url}: {exc}", []

        if not key:
            return False, f"{meta['name']} API key is empty.", []

        if pid == "openrouter":
            req_auth = urllib.request.Request(
                f"{OPENROUTER_BASE_URL}/auth/key",
                headers=self._headers(key),
                method="GET",
            )
            try:
                with urllib.request.urlopen(req_auth, timeout=timeout, context=self._ssl_context) as resp:
                    if resp.status in (401, 403):
                        return False, "API key was rejected by OpenRouter.", []
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    return False, f"Invalid OpenRouter API key (HTTP {exc.code}).", []
            except urllib.error.URLError as exc:
                return False, f"Network error contacting OpenRouter: {exc.reason}", []

            models = self.fetch_free_models(force_refresh=True, api_key=key, timeout=timeout)
            if not models:
                return True, "Key accepted (using built-in free model fallback list).", [
                    {"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS
                ]
            return True, f"Verified! Discovered {len(models)} free models.", models

        # Verify OpenAI, Gemini, Anthropic, Groq, or DeepSeek against their model-listing endpoint
        base_url = self._provider_base_url(pid)
        headers: dict[str, str] = {"User-Agent": f"cue-zsh/{__version__}"}
        if pid == "gemini":
            url = f"{base_url}/models"
            headers["x-goog-api-key"] = key
        elif pid == "anthropic":
            url = f"{base_url}/models"
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            url = f"{base_url}/models"
            headers["Authorization"] = f"Bearer {key}"

        try:
            raw = self._http_get_json(url, headers, timeout)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            detail = self._extract_error_detail(err_body)
            lower = (detail + " " + err_body).lower()
            if exc.code in (401, 403) or "api_key_invalid" in lower or "api key not valid" in lower or "invalid api key" in lower:
                return False, f"Invalid {meta['name']} API key: {detail or f'HTTP {exc.code}'}", []
            if exc.code == 404 and pid == "anthropic":
                models = self._default_model_entries(pid)
                return True, f"{meta['name']} key accepted (using built-in Claude models).", models
            return False, f"{meta['name']} returned HTTP {exc.code}: {detail or exc.reason}", []
        except urllib.error.URLError as exc:
            return False, f"Network error contacting {meta['name']}: {exc.reason}", []
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"Unexpected response from {meta['name']}: {exc}", []

        models = self._parse_provider_models_response(pid, raw)
        self._save_cached_provider_models(pid, models)
        if not models:
            models = self._default_model_entries(pid)
            return True, f"{meta['name']} key accepted (using built-in model list).", models
        return True, f"Verified! Discovered {len(models)} {meta['short_name']} models.", models

    def _default_model_entries(self, provider_id: str) -> list[dict[str, Any]]:
        meta = get_provider_meta(provider_id)
        return [{"id": m, "name": m, "context_length": 32768} for m in meta["default_models"]]

    def _parse_provider_models_response(self, provider_id: str, raw: Any) -> list[dict[str, Any]]:
        pid = normalize_provider(provider_id)
        defaults = get_provider_meta(pid)["default_models"]
        default_rank = {m: idx for idx, m in enumerate(defaults)}
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()

        if pid == "gemini":
            for item in (raw.get("models", []) if isinstance(raw, dict) else []):
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods", [])
                if methods and "generateContent" not in methods:
                    continue
                mid = str(item.get("name", "")).removeprefix("models/").strip()
                if not _is_valid_provider_model(pid, mid, item) or mid in seen:
                    continue
                seen.add(mid)
                parsed.append(
                    {
                        "id": mid,
                        "name": str(item.get("displayName", mid)),
                        "context_length": int(item.get("inputTokenLimit") or 128000),
                    }
                )
        elif pid == "ollama":
            for item in (raw.get("models", []) if isinstance(raw, dict) else []):
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("name") or item.get("model") or "").strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                parsed.append({"id": mid, "name": mid, "context_length": 32768})
        else:
            raw_list = raw.get("data", []) if isinstance(raw, dict) else []
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id", "")).strip()
                if not _is_valid_provider_model(pid, mid, item) or mid in seen:
                    continue
                seen.add(mid)
                parsed.append(
                    {
                        "id": mid,
                        "name": str(item.get("display_name") or item.get("name") or mid),
                        "context_length": int(item.get("context_length") or item.get("context_window") or 128000),
                    }
                )

        if pid != "ollama":
            parsed.sort(key=lambda m: (0 if m["id"] in default_rank else 1, default_rank.get(m["id"], 999), m["id"]))
        return parsed

    def _read_cached_provider_models(self, provider_id: str) -> tuple[int, list[dict[str, Any]]]:
        pid = normalize_provider(provider_id)
        cached = read_json_safe(self.paths["models_cache"], {})
        if not isinstance(cached, dict):
            return 0, []
        providers_map = cached.get("providers")
        if isinstance(providers_map, dict) and isinstance(providers_map.get(pid), dict):
            entry = providers_map[pid]
            models = entry.get("models", [])
            if isinstance(models, list):
                valid = [m for m in models if _is_valid_provider_model(pid, str(m.get("id", "")), m)]
                return int(entry.get("fetched_at", 0) or 0), valid
        if pid == "openrouter" and isinstance(cached.get("models"), list):
            valid = [m for m in cached["models"] if is_free_model(str(m.get("id", "")), m)]
            return int(cached.get("fetched_at", 0) or 0), valid
        return 0, []

    def _save_cached_provider_models(self, provider_id: str, models: list[dict[str, Any]]) -> None:
        pid = normalize_provider(provider_id)
        now = int(time.time())
        cached = read_json_safe(self.paths["models_cache"], {})
        if not isinstance(cached, dict):
            cached = {}
        providers_map = cached.get("providers")
        if not isinstance(providers_map, dict):
            providers_map = {}
        providers_map[pid] = {"fetched_at": now, "models": models}
        cached["providers"] = providers_map
        if pid == "openrouter":
            cached["fetched_at"] = now
            cached["models"] = models
        atomic_write_json(self.paths["models_cache"], cached)

    def fetch_free_models(
        self,
        force_refresh: bool = False,
        api_key: str | None = None,
        timeout: float = 4.0,
    ) -> list[dict[str, Any]]:
        """Fetch and cache free OpenRouter models."""
        now = int(time.time())
        if not force_refresh:
            fetched_at, cached_models = self._read_cached_provider_models("openrouter")
            if cached_models and now - fetched_at < DEFAULT_MODEL_CACHE_TTL:
                return cached_models

        headers = {"User-Agent": f"cue-zsh/{__version__}"}
        key = (api_key or get_provider_api_key(self.config, "openrouter")).strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        req = urllib.request.Request(f"{OPENROUTER_BASE_URL}/models", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_context) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
            append_log(f"Failed to refresh OpenRouter models: {exc}", "WARN", self.home)
            _, cached_models = self._read_cached_provider_models("openrouter")
            if cached_models:
                return cached_models
            return self._default_model_entries("openrouter")

        raw_data = raw.get("data", []) if isinstance(raw, dict) else []
        free_models: list[dict[str, Any]] = []
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id", ""))
            if is_free_model(mid, item):
                free_models.append(
                    {
                        "id": mid,
                        "name": str(item.get("name", mid)),
                        "context_length": int(item.get("context_length") or 32768),
                    }
                )

        if free_models:
            self._save_cached_provider_models("openrouter", free_models)
            return free_models

        return self._default_model_entries("openrouter")

    def fetch_models(
        self,
        provider_id: str | None = None,
        force_refresh: bool = False,
        api_key: str | None = None,
        timeout: float = 4.0,
    ) -> list[dict[str, Any]]:
        """Fetch and cache available models for any supported AI provider."""
        pid = normalize_provider(provider_id or get_active_provider(self.config))
        if pid == "openrouter":
            return self.fetch_free_models(force_refresh=force_refresh, api_key=api_key, timeout=timeout)

        now = int(time.time())
        if not force_refresh:
            fetched_at, cached_models = self._read_cached_provider_models(pid)
            if cached_models and now - fetched_at < DEFAULT_MODEL_CACHE_TTL:
                return cached_models

        key = (api_key if api_key is not None else get_provider_api_key(self.config, pid)).strip()
        meta = get_provider_meta(pid)
        if meta["requires_key"] and not key:
            _, cached_models = self._read_cached_provider_models(pid)
            return cached_models or self._default_model_entries(pid)

        base_url = self._provider_base_url(pid)
        headers: dict[str, str] = {"User-Agent": f"cue-zsh/{__version__}"}
        if pid == "ollama":
            url = f"{base_url}/api/tags"
        elif pid == "gemini":
            url = f"{base_url}/models"
            headers["x-goog-api-key"] = key
        elif pid == "anthropic":
            url = f"{base_url}/models"
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        else:
            url = f"{base_url}/models"
            headers["Authorization"] = f"Bearer {key}"

        try:
            raw = self._http_get_json(url, headers, timeout)
            models = self._parse_provider_models_response(pid, raw)
            if models:
                self._save_cached_provider_models(pid, models)
                return models
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
            append_log(f"Failed to refresh {pid} models: {exc}", "WARN", self.home)

        _, cached_models = self._read_cached_provider_models(pid)
        return cached_models or self._default_model_entries(pid)

    def _load_health(self) -> dict[str, Any]:
        raw = read_json_safe(self.paths["model_health"], {})
        return raw if isinstance(raw, dict) else {}

    def _save_health(self, health: dict[str, Any]) -> None:
        atomic_write_json(self.paths["model_health"], health)

    def mark_model_cooldown(
        self,
        model_id: str,
        reason: str,
        cooldown_seconds: int,
        provider_id: str = "openrouter",
    ) -> None:
        """Record a temporary cooldown on a model when it hits a limit or fails."""
        pid = normalize_provider(provider_id)
        if pid == "openrouter" and model_id == "openrouter/free":
            cooldown_seconds = min(cooldown_seconds, 15)
        hkey = _health_key(pid, model_id)
        with self._health_lock:
            health = self._load_health()
            prev = health.get(hkey, {}) if isinstance(health.get(hkey), dict) else {}
            health[hkey] = {
                "cooldown_until": int(time.time()) + cooldown_seconds,
                "reason": reason[:200],
                "latency_ms": prev.get("latency_ms", 0),
                "updated_at": int(time.time()),
            }
            self._save_health(health)

    def record_model_latency(
        self,
        model_id: str,
        latency_ms: float,
        provider_id: str = "openrouter",
    ) -> None:
        """Track rolling average latency (EMA) to prefer fastest healthy models."""
        hkey = _health_key(provider_id, model_id)
        with self._health_lock:
            health = self._load_health()
            prev = health.get(hkey, {}) if isinstance(health.get(hkey), dict) else {}
            old_ms = float(prev.get("latency_ms", 0) or 0)
            ema_ms = round(latency_ms if old_ms <= 0 else (old_ms * 0.6 + latency_ms * 0.4), 1)
            health[hkey] = {
                "cooldown_until": 0,
                "reason": "",
                "latency_ms": ema_ms,
                "updated_at": int(time.time()),
            }
            self._save_health(health)

    def clear_model_health(self) -> None:
        """Clear all model cooldowns."""
        self._save_health({})

    def get_candidate_models(self, provider_id: str | None = None) -> list[str]:
        """Return ordered list of healthy candidate models for the given or active provider."""
        pid = normalize_provider(provider_id or get_active_provider(self.config))
        meta = get_provider_meta(pid)
        defaults = list(meta["default_models"])

        if pid == "openrouter":
            models = self.fetch_free_models(force_refresh=False)
        else:
            models = self.fetch_models(pid, force_refresh=False)

        available_ids = {m["id"] for m in models if _is_valid_provider_model(pid, str(m.get("id", "")), m)}
        if not available_ids:
            available_ids = set(defaults)

        ordered: list[str] = []
        preferred = get_provider_preferred_model(self.config, pid)
        state = load_state(self.home)
        active_state = str(state.get("active_model", "")).strip() if state.get("active_provider", pid) == pid else ""

        seed_list = [active_state, preferred, *defaults]
        for candidate in seed_list:
            if not candidate or candidate in ordered:
                continue
            if pid == "openrouter":
                if is_free_model(candidate) and candidate in available_ids:
                    ordered.append(candidate)
            elif _is_valid_provider_model(pid, candidate) and (candidate in available_ids or candidate == preferred):
                ordered.append(candidate)

        if pid == "openrouter":
            extra = sorted(
                [m for m in models if m["id"] not in ordered and is_free_model(m["id"], m)],
                key=lambda m: (
                    1 if ("-vl" in m["id"].lower() or "vision" in m["id"].lower()) else 0,
                    -int(m.get("context_length", 0)),
                ),
            )
            for m in extra:
                ordered.append(m["id"])
        else:
            for m in models:
                mid = str(m.get("id", ""))
                if mid and mid not in ordered and _is_valid_provider_model(pid, mid, m):
                    ordered.append(mid)

        now = int(time.time())
        health = self._load_health()

        def _h_entry(mid: str) -> dict[str, Any]:
            hk = _health_key(pid, mid)
            val = health.get(hk)
            if isinstance(val, dict):
                return val
            if pid != "openrouter" and isinstance(health.get(mid), dict):
                return health[mid]
            return {}

        healthy = [mid for mid in ordered if int(_h_entry(mid).get("cooldown_until", 0)) <= now]
        if healthy:
            def _rank_key(mid: str) -> tuple[int, float]:
                pos = ordered.index(mid)
                lat = float(_h_entry(mid).get("latency_ms", 0) or 0)
                is_slow = 1 if lat > 3500 else 0
                return (is_slow, lat if lat > 0 else 900.0 + pos * 50.0)

            return sorted(healthy, key=_rank_key)

        return sorted(ordered, key=lambda mid: int(_h_entry(mid).get("cooldown_until", 0)))

    def get_models_status(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        """Return provider models with live availability/cooldown status for `cue models`."""
        pid = normalize_provider(provider_id or get_active_provider(self.config))
        now = int(time.time())
        health = self._load_health()
        state = load_state(self.home)
        active = state.get("active_model", "")
        if state.get("active_provider", pid) != pid:
            active = get_provider_preferred_model(self.config, pid)
        models = self.fetch_free_models(force_refresh=False) if pid == "openrouter" else self.fetch_models(pid, force_refresh=False)
        by_id = {m["id"]: m for m in models}

        rows: list[dict[str, Any]] = []
        for mid in self.get_candidate_models(pid):
            meta = by_id.get(mid, {"id": mid, "name": mid, "context_length": 32768})
            h = health.get(_health_key(pid, mid), {})
            cooldown_rem = max(0, int(h.get("cooldown_until", 0)) - now)
            rows.append(
                {
                    "provider": pid,
                    "id": mid,
                    "name": meta.get("name", mid),
                    "context_length": meta.get("context_length", 32768),
                    "active": mid == active,
                    "status": "cooldown" if cooldown_rem > 0 else "ready",
                    "cooldown_seconds": cooldown_rem,
                    "reason": h.get("reason", ""),
                }
            )
        return rows

    def _get_host_conn(self, scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        conns = getattr(self._tls, "conns", None)
        if conns is None:
            conns = {}
            self._tls.conns = conns
        ckey = f"{scheme}://{host}:{port}"
        conn = conns.get(ckey)
        if conn is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl_context)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conns[ckey] = conn
            if ckey == "https://openrouter.ai:443":
                self._tls.conn = conn
        else:
            conn.timeout = timeout
        return conn

    def _close_host_conn(self, scheme: str, host: str, port: int) -> None:
        conns = getattr(self._tls, "conns", None)
        ckey = f"{scheme}://{host}:{port}"
        if isinstance(conns, dict) and ckey in conns:
            conn = conns.pop(ckey)
            try:
                conn.close()
            except OSError:
                pass
        if ckey == "https://openrouter.ai:443":
            self._close_conn()

    def _get_conn(self, timeout: float) -> http.client.HTTPSConnection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(
                "openrouter.ai", 443, timeout=timeout, context=self._ssl_context
            )
            self._tls.conn = conn
        else:
            conn.timeout = timeout
        return conn

    def _close_conn(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
            self._tls.conn = None

    def _post_json_keepalive(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme or "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req_headers = dict(headers)
        req_headers["Connection"] = "keep-alive"

        for attempt in range(2):
            try:
                conn = self._get_host_conn(scheme, host, port, timeout)
                conn.request("POST", path, body=body, headers=req_headers)
                resp = conn.getresponse()
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise urllib.error.HTTPError(
                        url=url,
                        code=resp.status,
                        msg=resp.reason or "HTTP Error",
                        hdrs=resp.headers,
                        fp=io.BytesIO(raw_bytes),
                    )
                return json.loads(raw_text)
            except (http.client.HTTPException, BrokenPipeError, ConnectionResetError):
                self._close_host_conn(scheme, host, port)
                if attempt == 1:
                    raise
        raise TimeoutError(f"Keep-alive request failed for {host}")

    def _post_chat_completion(
        self,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.15,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = self._headers(api_key)
        headers["Connection"] = "keep-alive"

        for attempt in range(2):
            try:
                conn = self._get_conn(timeout)
                conn.request("POST", "/api/v1/chat/completions", body=body, headers=headers)
                resp = conn.getresponse()
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise urllib.error.HTTPError(
                        url=f"{OPENROUTER_BASE_URL}/chat/completions",
                        code=resp.status,
                        msg=resp.reason or "HTTP Error",
                        hdrs=resp.headers,
                        fp=io.BytesIO(raw_bytes),
                    )
                return json.loads(raw_text)
            except (http.client.HTTPException, BrokenPipeError, ConnectionResetError):
                self._close_conn()
                if attempt == 1:
                    raise
        raise TimeoutError("OpenRouter keep-alive request failed")

    def _post_openai_compatible(
        self,
        provider_id: str,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """Call OpenAI (ChatGPT), Groq, DeepSeek, or other OpenAI-compatible Chat Completions APIs."""
        base_url = self._provider_base_url(provider_id)
        url = f"{base_url}/chat/completions"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": f"cue-zsh/{__version__}",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        is_reasoning = provider_id == "openai" and _uses_openai_reasoning_params(model_id)
        payload: dict[str, Any] = {"model": model_id, "messages": messages}
        if is_reasoning:
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["temperature"] = 0.15
            payload["max_tokens"] = max_tokens
            if provider_id in ("openai", "deepseek"):
                payload["response_format"] = {"type": "json_object"}

        try:
            return self._post_json_keepalive(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            lower = err_body.lower()
            if exc.code == 400:
                retry_payload = dict(payload)
                changed = False
                if "max_tokens" in lower and "max_completion_tokens" in lower:
                    retry_payload.pop("max_tokens", None)
                    retry_payload["max_completion_tokens"] = max_tokens
                    changed = True
                if "temperature" in lower:
                    retry_payload.pop("temperature", None)
                    changed = True
                if "response_format" in lower:
                    retry_payload.pop("response_format", None)
                    changed = True
                if self._needs_system_role_fallback(400, err_body):
                    retry_payload["messages"] = self._merge_system_into_user(messages)
                    changed = True
                if changed:
                    return self._post_json_keepalive(url, retry_payload, headers, timeout)
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_body.encode("utf-8"))) from exc

    def _post_gemini_generate(
        self,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        """Call Google Gemini generateContent REST API cleanly with JSON response mode."""
        clean_model = model_id.removeprefix("models/").strip()
        base_url = self._provider_base_url("gemini")
        url = f"{base_url}/models/{urllib.parse.quote(clean_model, safe='')}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": f"cue-zsh/{__version__}",
        }

        sys_parts = [m["content"].strip() for m in messages if m.get("role") == "system" and m.get("content")]
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                continue
            gem_role = "model" if role == "assistant" else "user"
            text = str(m.get("content", "")).strip()
            if not text:
                continue
            if contents and contents[-1]["role"] == gem_role:
                contents[-1]["parts"][0]["text"] += "\n" + text
            else:
                contents.append({"role": gem_role, "parts": [{"text": text}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Suggest commands"}]}]

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        if sys_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}

        try:
            return self._post_json_keepalive(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            lower = err_body.lower()
            if exc.code == 400 and ("responsemimetype" in lower or "json mode" in lower or "developer instruction" in lower or "system" in lower):
                retry_payload = {
                    "contents": contents,
                    "generationConfig": {"temperature": 0.15, "maxOutputTokens": max_tokens},
                }
                if "developer instruction" in lower or "system" in lower:
                    if sys_parts and retry_payload["contents"]:
                        first_txt = retry_payload["contents"][0]["parts"][0]["text"]
                        retry_payload["contents"][0]["parts"][0]["text"] = "Instructions:\n" + "\n".join(sys_parts) + "\n\nRequest:\n" + first_txt
                elif sys_parts:
                    retry_payload["systemInstruction"] = payload["systemInstruction"]
                return self._post_json_keepalive(url, retry_payload, headers, timeout)
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_body.encode("utf-8"))) from exc

    def _post_anthropic_messages(
        self,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """Call Anthropic Claude Messages API cleanly with strict role alternation."""
        base_url = self._provider_base_url("anthropic")
        url = f"{base_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": f"cue-zsh/{__version__}",
        }

        sys_parts = [m["content"].strip() for m in messages if m.get("role") == "system" and m.get("content")]
        turns: list[dict[str, str]] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                continue
            cl_role = "assistant" if role == "assistant" else "user"
            text = str(m.get("content", "")).strip()
            if not text:
                continue
            if not turns and cl_role == "assistant":
                continue
            if turns and turns[-1]["role"] == cl_role:
                turns[-1]["content"] += "\n" + text
            else:
                turns.append({"role": cl_role, "content": text})
        if not turns:
            turns = [{"role": "user", "content": "Suggest commands"}]

        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "temperature": 0.15,
            "messages": turns,
        }
        if sys_parts:
            payload["system"] = "\n".join(sys_parts)

        return self._post_json_keepalive(url, payload, headers, timeout)

    def _post_ollama_chat(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 12.0,
        max_tokens: int = 900,
    ) -> dict[str, Any]:
        """Call local Ollama OpenAI-compatible chat endpoint, falling back to /api/chat."""
        base_url = self._provider_base_url("ollama")
        headers = {"Content-Type": "application/json", "User-Agent": f"cue-zsh/{__version__}"}
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.15,
            "max_tokens": max_tokens,
        }
        try:
            return self._post_json_keepalive(f"{base_url}/v1/chat/completions", payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                native_payload = {
                    "model": model_id,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.15, "num_predict": max_tokens},
                }
                return self._post_json_keepalive(f"{base_url}/api/chat", native_payload, headers, timeout)
            raise

    def _post_provider_completion(
        self,
        provider_id: str,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        pid = normalize_provider(provider_id)
        if pid == "openrouter":
            if max_tokens is None:
                return self._post_chat_completion(api_key, model_id, messages, timeout=timeout)
            return self._post_chat_completion(api_key, model_id, messages, timeout=timeout, max_tokens=max_tokens)
        eff_max = max_tokens if max_tokens is not None else 900
        if pid == "gemini":
            return self._post_gemini_generate(api_key, model_id, messages, timeout=timeout, max_tokens=max(eff_max, 1200))
        if pid == "anthropic":
            return self._post_anthropic_messages(api_key, model_id, messages, timeout=timeout, max_tokens=eff_max)
        if pid == "ollama":
            return self._post_ollama_chat(model_id, messages, timeout=max(timeout, 12.0), max_tokens=eff_max)
        return self._post_openai_compatible(pid, api_key, model_id, messages, timeout=timeout, max_tokens=eff_max)

    @staticmethod
    def _extract_response_text(provider_id: str, resp: dict[str, Any]) -> str:
        """Extract generated text content across OpenAI, OpenRouter, Gemini, Claude, and Ollama formats."""
        if not isinstance(resp, dict):
            return ""
        pid = normalize_provider(provider_id)

        if pid == "gemini":
            cands = resp.get("candidates", [])
            if isinstance(cands, list) and cands and isinstance(cands[0], dict):
                parts = cands[0].get("content", {}).get("parts", [])
                if isinstance(parts, list):
                    answer_parts = [str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
                    if answer_parts:
                        return "".join(answer_parts)
                    all_parts = [str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text")]
                    return "".join(all_parts)
            return ""

        if pid == "anthropic":
            blocks = resp.get("content", [])
            if isinstance(blocks, list):
                texts = [str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                return "".join(texts)
            return ""

        choices = resp.get("choices", [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                content = str(msg.get("content") or "")
                if not content.strip() and msg.get("reasoning"):
                    content = str(msg.get("reasoning") or "")
                return content

        if isinstance(resp.get("message"), dict):
            return str(resp["message"].get("content") or "")
        return ""

    @staticmethod
    def _extract_error_detail(body: str) -> str:
        """Extract concise human-readable error message from provider JSON error payload."""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    raw_msg = str(err.get("metadata", {}).get("raw", "") or err.get("message", ""))
                    if raw_msg:
                        return " ".join(raw_msg.splitlines()).strip()[:90]
                if isinstance(parsed.get("message"), str):
                    return " ".join(parsed["message"].splitlines()).strip()[:90]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return " ".join(body.splitlines()).strip()[:90]

    def _classify_failure(
        self,
        code: int | None,
        body: str,
        provider_id: str = "openrouter",
    ) -> tuple[bool, int, str]:
        """Return (is_auth_fatal, cooldown_seconds, short_reason)."""
        pid = normalize_provider(provider_id)
        pname = get_provider_meta(pid)["short_name"]
        detail = self._extract_error_detail(body)
        lower = (detail + " " + body).lower()

        if code in (401, 403):
            return True, 0, f"HTTP {code}: invalid or unauthorized {pname} API key"
        if pid == "gemini" and ("api_key_invalid" in lower or "api key not valid" in lower):
            return True, 0, "Invalid Gemini API key"

        if pid == "openrouter" and ("data policy" in lower or "privacy" in lower):
            return True, 0, "OpenRouter privacy blocks :free endpoints (enable at openrouter.ai/settings/privacy)"

        if "insufficient_quota" in lower or "credit balance is too low" in lower or "billing" in lower:
            return False, 6 * 3600, f"{pname} quota/billing exhausted ({detail[:50]})"

        if code == 429 or "rate limit" in lower or "rate_limit" in lower or "quota" in lower or "resource_exhausted" in lower or "limit exceeded" in lower:
            if pid == "openrouter":
                if "free-models-per-day" in lower or ("10 credits" in lower and "1000" in lower):
                    return True, 0, "OpenRouter daily :free cap reached (50/day without $10 credits; resets 00:00 UTC)"
                if "free-models-per-min" in lower:
                    return True, 0, "OpenRouter 20 req/min :free rate limit reached; wait ~60s"
            is_daily = "daily" in lower or "per-day" in lower or "day" in lower or "credits" in lower
            cooldown = 6 * 3600 if is_daily else 90
            short_detail = detail or f"HTTP {code or '429'}"
            return False, cooldown, f"429 {short_detail}"

        if code == 404 or "no endpoints found" in lower or "not found" in lower:
            return False, 24 * 3600, f"Model unavailable ({code or '404'})"
        if code == 402:
            return False, 24 * 3600, "Model requires credits/payment (skipping)"
        if code in (500, 502, 503, 504, 529) or "overload" in lower or "timeout" in lower:
            return False, 2 * 60, f"{pname} transient error ({code or 'timeout'})"
        return False, 3 * 60, f"Model error ({code or 'unknown'}): {detail[:70]}"

    @staticmethod
    def _merge_system_into_user(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Fold any 'system' messages into the first user message for models without developer-role support."""
        sys_parts: list[str] = []
        rest: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                content = str(msg.get("content", "")).strip()
                if content:
                    sys_parts.append(content)
            else:
                rest.append(dict(msg))
        if not sys_parts:
            return messages
        prefix = "Instructions:\n" + "\n".join(sys_parts) + "\n\nRequest:\n"
        if rest and rest[0].get("role") == "user":
            rest[0]["content"] = prefix + str(rest[0].get("content", ""))
        else:
            rest.insert(0, {"role": "user", "content": prefix})
        return rest

    @staticmethod
    def _needs_system_role_fallback(code: int | None, body: str) -> bool:
        lower = body.lower()
        return code == 400 and (
            "developer instruction" in lower
            or "system message" in lower
            or "system role" in lower
            or "role 'system'" in lower
        )

    @staticmethod
    def _sanitize_command(raw_cmd: str) -> str:
        cmd = " ".join(str(raw_cmd).strip().splitlines()).strip()
        if not cmd:
            return ""
        if len(cmd) >= 2 and cmd.startswith("`") and cmd.endswith("`"):
            cmd = cmd[1:-1].strip()
        if cmd.startswith("$ ") and len(cmd) > 2:
            cmd = cmd[2:].strip()
        if not cmd or len(cmd) > 220:
            return ""
        if re.search(r"<[A-Za-z0-9_.-]{2,}>", cmd) or "YOUR_API_KEY" in cmd or "YOUR_TOKEN" in cmd:
            return ""
        if re.search(r"(?:^|\s|;|&)(?:rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)|:\(\)\s*\{|mkfs\.|dd\s+if=/dev/)", cmd):
            return ""
        return cmd

    def _clean_and_rank_suggestions(
        self,
        raw_items: list[Any],
        partial_input: str = "",
        mode: str = "analyze",
    ) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        seen: set[str] = set()
        q = partial_input.strip().lower()

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            cmd = self._sanitize_command(str(item.get("cmd", "")))
            if not cmd:
                continue
            key = " ".join(cmd.split())
            if key in seen:
                continue
            if mode == "complete" and q and key.lower() == q:
                continue
            seen.add(key)
            reason = " ".join(str(item.get("reason", "")).strip().splitlines()).strip()
            cleaned.append({"cmd": cmd, "reason": (reason or "Project command")[:90]})

        if mode == "complete" and q and cleaned:
            def _comp_rank(entry: dict[str, str]) -> tuple[int, int]:
                c = entry["cmd"].lower()
                if c.startswith(q):
                    return (0, len(c))
                if q in c:
                    return (1, len(c))
                return (2, len(c))

            cleaned = [
                item
                for _, item in sorted(
                    enumerate(cleaned),
                    key=lambda pair: (_comp_rank(pair[1])[0], pair[0]),
                )
            ]
        return cleaned[:12]

    def _run_provider_attempts(
        self,
        pid: str,
        api_key: str,
        messages: list[dict[str, str]],
        partial_input: str,
        mode: str,
        prev_provider: str,
        prev_model: str,
        per_req_timeout: float,
        deadline: float,
        errors: list[str],
        has_fallback_provider: bool,
    ) -> dict[str, Any] | None:
        """Try candidate models for a single provider; return result dict or None if all failed."""
        candidates = self.get_candidate_models(pid)
        max_attempts = min(len(candidates), 12 if pid == "openrouter" else 6)
        state = load_state(self.home)

        for idx, model_id in enumerate(candidates[:max_attempts]):
            if pid == "openrouter" and not is_free_model(model_id):
                continue
            if idx > 0 and time.monotonic() >= deadline:
                break
            t0 = time.monotonic()
            try:
                req_msgs = messages
                try:
                    resp = self._post_provider_completion(
                        pid, api_key, model_id, req_msgs, timeout=per_req_timeout
                    )
                except urllib.error.HTTPError as exc:
                    err_body = ""
                    try:
                        err_body = exc.read().decode("utf-8", errors="replace")
                    except OSError:
                        pass
                    if self._needs_system_role_fallback(exc.code, err_body):
                        req_msgs = self._merge_system_into_user(messages)
                        resp = self._post_provider_completion(
                            pid, api_key, model_id, req_msgs, timeout=per_req_timeout
                        )
                    else:
                        raise urllib.error.HTTPError(
                            exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_body.encode("utf-8"))
                        ) from exc

                if isinstance(resp, dict) and "error" in resp:
                    err_obj = resp["error"]
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                    int_code = int(err_code) if isinstance(err_code, int) else None
                    if self._needs_system_role_fallback(int_code, err_msg):
                        req_msgs = self._merge_system_into_user(messages)
                        resp = self._post_provider_completion(
                            pid, api_key, model_id, req_msgs, timeout=per_req_timeout
                        )
                    if isinstance(resp, dict) and "error" in resp:
                        err_obj = resp["error"]
                        err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                        err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                        is_auth, cd, reason = self._classify_failure(
                            int(err_code) if isinstance(err_code, int) else None, err_msg, provider_id=pid
                        )
                        if is_auth:
                            if has_fallback_provider and "rate limit" in reason.lower():
                                errors.append(f"{pid}/{model_id}: {reason}")
                                break
                            state["last_ai_error"] = reason[:220]
                            state["last_ai_error_at"] = int(time.time())
                            save_state(state, self.home)
                            raise AuthError(reason)
                        self.mark_model_cooldown(model_id, reason, cd, provider_id=pid)
                        errors.append(f"{model_id}: {reason}")
                        append_log(f"Switching from {pid}/{model_id} ({reason})", "WARN", self.home)
                        continue

                content = self._extract_response_text(pid, resp)
                parsed = _extract_json_object(content)
                if not parsed or not isinstance(parsed.get("suggestions"), list):
                    self.mark_model_cooldown(model_id, "Malformed JSON response", 3 * 60, provider_id=pid)
                    errors.append(f"{model_id}: malformed JSON")
                    append_log(f"Model {pid}/{model_id} returned malformed JSON ({len(content)} chars).", "WARN", self.home)
                    continue

                clean_suggestions = self._clean_and_rank_suggestions(
                    parsed["suggestions"],
                    partial_input=partial_input,
                    mode=mode,
                )
                if not clean_suggestions:
                    self.mark_model_cooldown(model_id, "Empty suggestion list", 3 * 60, provider_id=pid)
                    errors.append(f"{model_id}: empty suggestions")
                    continue

                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.record_model_latency(model_id, elapsed_ms, provider_id=pid)
                switched = bool((prev_model and prev_model != model_id) or (prev_provider and prev_provider != pid))
                state["active_provider"] = pid
                state["active_model"] = model_id
                state["last_ai_error"] = ""
                state["last_ai_error_at"] = 0
                state["total_ai_calls"] = int(state.get("total_ai_calls", 0)) + 1
                if switched or idx > 0 or errors:
                    state["total_fallbacks"] = int(state.get("total_fallbacks", 0)) + 1
                    state["last_switch_reason"] = errors[-1] if errors else f"Switched to {pid}/{model_id}"
                    state["last_switch_at"] = int(time.time())
                save_state(state, self.home)

                return {
                    "provider": pid,
                    "model": model_id,
                    "latency_ms": round(elapsed_ms, 1),
                    "switched": switched or idx > 0 or bool(errors),
                    "suggestions": clean_suggestions[:12],
                    "summary": str(parsed.get("summary", "")).strip()[:400],
                }

            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except OSError:
                    pass
                is_auth, cd, reason = self._classify_failure(exc.code, body, provider_id=pid)
                if is_auth:
                    if has_fallback_provider and ("rate limit" in reason.lower() or "cap reached" in reason.lower()):
                        errors.append(f"{pid}/{model_id}: {reason}")
                        append_log(f"Provider {pid} quota hit ({reason}); trying fallback provider.", "WARN", self.home)
                        break
                    state["last_ai_error"] = reason[:220]
                    state["last_ai_error_at"] = int(time.time())
                    save_state(state, self.home)
                    raise AuthError(reason) from exc
                self.mark_model_cooldown(model_id, reason, cd, provider_id=pid)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {pid}/{model_id} hit {reason}; switching.", "WARN", self.home)
                if "quota/billing exhausted" in reason:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                reason = f"Connection/timeout error: {exc}"
                self.mark_model_cooldown(model_id, reason, 3 * 60, provider_id=pid)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {pid}/{model_id} error: {exc}; switching.", "WARN", self.home)
        return None

    def generate_suggestions(
        self,
        project_ctx: dict[str, Any],
        partial_input: str = "",
        cwd: str = "",
        mode: str = "analyze",
        error_context: str = "",
        timeout: float = 9.0,
    ) -> dict[str, Any]:
        """Request smart command suggestions from the active provider, switching models/providers on limits."""
        active_pid = get_active_provider(self.config)
        meta = get_provider_meta(active_pid)
        allow_fallback = bool(self.config.get("auto_provider_fallback", True))

        if active_pid == "ollama":
            provider_queue = ["ollama"]
            if allow_fallback:
                provider_queue.extend([p for p in get_configured_providers(self.config, include_local=False) if p != "ollama"])
        else:
            configured = get_configured_providers(self.config, include_local=False)
            if not configured:
                raise AuthError(f"No {meta['name']} API key configured. Run `cue token --provider {active_pid}` or `cue settings`.")
            provider_queue = configured if allow_fallback else [configured[0]]

        messages = self._build_messages(
            project_ctx,
            partial_input=partial_input,
            cwd=cwd,
            mode=mode,
            error_context=error_context,
        )
        state = load_state(self.home)
        prev_provider = str(state.get("active_provider", active_pid))
        prev_model = str(state.get("active_model", ""))

        errors: list[str] = []
        per_req_timeout = max(4.0, min(float(timeout), 12.0))
        deadline = time.monotonic() + max(per_req_timeout * 2.5, 18.0)

        for p_idx, pid in enumerate(provider_queue):
            if p_idx > 0 and time.monotonic() >= deadline:
                break
            api_key = get_provider_api_key(self.config, pid)
            if get_provider_meta(pid)["requires_key"] and not api_key:
                continue
            has_more = p_idx + 1 < len(provider_queue)
            result = self._run_provider_attempts(
                pid=pid,
                api_key=api_key,
                messages=messages,
                partial_input=partial_input,
                mode=mode,
                prev_provider=prev_provider,
                prev_model=prev_model,
                per_req_timeout=per_req_timeout,
                deadline=deadline,
                errors=errors,
                has_fallback_provider=has_more,
            )
            if result is not None:
                return result

        label = "free models" if (len(provider_queue) == 1 and provider_queue[0] == "openrouter") else "models"
        final_msg = f"All tried {label} failed: " + "; ".join(errors[-3:])
        state = load_state(self.home)
        state["last_ai_error"] = final_msg[:220]
        state["last_ai_error_at"] = int(time.time())
        save_state(state, self.home)
        raise AllModelsExhaustedError(final_msg)

    def _build_messages(
        self,
        project_ctx: dict[str, Any],
        partial_input: str = "",
        cwd: str = "",
        mode: str = "analyze",
        error_context: str = "",
    ) -> list[dict[str, str]]:
        os_name = platform.system()
        shell_name = "powershell" if os_name == "Windows" else Path(os.environ.get("SHELL", "zsh")).name
        analysis = project_ctx.get("analysis", {})
        ai_ctx = project_ctx.get("ai_context", {})
        recent_cmds = project_ctx.get("recent_commands", [])[-8:]

        system_parts = [
            f"You are cue ({os_name}/{shell_name} project-aware terminal assistant).",
            'Return ONLY valid JSON without markdown: {"suggestions":[{"cmd":"<exact command>","reason":"<3-7 words>"}],"summary":"<concise project note or direct answer>"}.',
            "Rules:",
            "1. Suggest 6-12 safe, single-line, immediately runnable commands grounded strictly in the provided project context (stacks, package_manager, scripts, files/tree, git state, and recent commands).",
            "2. Use the project's exact package manager (e.g. pnpm vs npm, uv vs pip), real detected scripts/targets, and real paths from 'tree' or 'git.changed'. Never invent non-existent scripts, targets, or placeholder arguments like <file>.",
            "3. When mode=='complete' and 'input' is given, prioritize valid completions, flags, subcommands, and next-step workflows matching 'input' for this project.",
            "4. When mode=='fix', inspect 'input' and 'error' and suggest exact safe commands tailored to this project's stack/pm to diagnose or fix the failure.",
            "5. When mode=='prompt', treat 'input' as a natural-language question or task: provide a direct concise answer in 'summary', plus up to 12 exact safe commands for this repository in 'suggestions'.",
        ]
        summary = str(ai_ctx.get("summary", "")).strip()
        if summary:
            system_parts.append(f"Session context: {summary[:260]}")

        messages: list[dict[str, str]] = [{"role": "system", "content": "\n".join(system_parts)}]

        for turn in ai_ctx.get("turns", [])[-2:]:
            role = turn.get("role", "user")
            content = str(turn.get("content", "")).strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:300]})

        git_data = analysis.get("git", {}) if isinstance(analysis.get("git"), dict) else {}
        compact_scripts = []
        for s in analysis.get("scripts", [])[:12]:
            if isinstance(s, dict) and s.get("cmd"):
                desc = str(s.get("desc", "")).strip()
                compact_scripts.append(f"{s['cmd']} ({desc[:36]})" if desc else str(s["cmd"]))

        compact_project: dict[str, Any] = {
            "project": project_ctx.get("name", ""),
            "cwd": cwd or project_ctx.get("project_path", ""),
            "stacks": analysis.get("stacks", []),
            "pm": analysis.get("package_manager", ""),
            "scripts": compact_scripts,
            "tree": analysis.get("tree", [])[:28],
            "git": {
                "is_repo": bool(git_data.get("is_repo")),
                "branch": git_data.get("branch", ""),
                "dirty": bool(git_data.get("dirty", False)),
                "changed": git_data.get("status", [])[:6],
                "commits": git_data.get("recent_commits", [])[:2],
            },
            "recent": [{"c": c.get("cmd"), "e": c.get("exit_code", 0)} for c in recent_cmds],
            "mode": mode,
        }
        if mode == "prompt":
            compact_project["cue_version"] = __version__
        if partial_input:
            compact_project["input"] = partial_input
        if error_context:
            compact_project["error"] = error_context[:300]

        messages.append({"role": "user", "content": json.dumps(compact_project, separators=(",", ":"), ensure_ascii=False)})
        return messages


OpenRouterClient = AIClient
