"""OpenRouter API client enforcing :free models only, health cooldowns, and fallback switching."""

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
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_FREE_MODELS,
    DEFAULT_MODEL_CACHE_TTL,
    append_log,
    atomic_write_json,
    ensure_cue_dirs,
    load_config,
    load_state,
    read_json_safe,
    save_state,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
APP_TITLE = "cue-zsh"
APP_REFERER = "https://github.com/abdullahmia/Cue"

SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)


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
    """Base error from OpenRouter client."""


class AuthError(OpenRouterError):
    """Raised when the OpenRouter API key is missing or rejected (401/403)."""


class AllModelsExhaustedError(OpenRouterError):
    """Raised when every free model failed or is rate-limited."""


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


def _recover_truncated_suggestions(cleaned: str) -> dict[str, Any] | None:
    """Recover valid suggestions and summary even if JSON was truncated near max_tokens."""
    if '"suggestions"' not in cleaned:
        return None
    items: list[dict[str, str]] = []
    # Decode complete JSON objects inside the suggestions array even if closing brackets are missing
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
    # Strip <think>...</think> blocks emitted by reasoning models like deepseek-r1
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    if "<think>" in cleaned and "</think>" not in cleaned:
        # Handle unclosed <think> block if a JSON object still follows or sits inside
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


TRIVIAL_FILLER_CMDS = {"ls", "ls -la", "ls -l", "pwd", "clear", "cd", "cd ..", "whoami", "date"}
UNSAFE_CMD_PATTERNS = (
    re.compile(r"(^|\s)rm\s+-rf\s+(/|\~|\$HOME)(\s|$)"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    re.compile(r"(^|\s)mkfs\.[a-z0-9]+"),
)
PLACEHOLDER_RE = re.compile(r"<[a-zA-Z0-9_-]{2,24}>")


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


class OpenRouterClient:
    """OpenRouter client that only uses :free models and switches automatically on rate limits."""

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

    def validate_key(self, api_key: str, timeout: float = 12.0) -> tuple[bool, str, list[dict[str, Any]]]:
        """Verify an OpenRouter API key and fetch live free models."""
        key = api_key.strip()
        if not key:
            return False, "API key is empty.", []

        # 1. Check key endpoint when available
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

        # 2. Fetch live models list and filter strictly to :free models
        models = self.fetch_free_models(force_refresh=True, api_key=key, timeout=timeout)
        if not models:
            return True, "Key accepted (using built-in free model fallback list).", [
                {"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS
            ]
        return True, f"Verified! Discovered {len(models)} free models.", models

    def fetch_free_models(
        self,
        force_refresh: bool = False,
        api_key: str | None = None,
        timeout: float = 4.0,
    ) -> list[dict[str, Any]]:
        """Fetch and cache free OpenRouter models."""
        cache_path = self.paths["models_cache"]
        now = int(time.time())

        if not force_refresh:
            cached = read_json_safe(cache_path, {})
            if isinstance(cached, dict) and now - int(cached.get("fetched_at", 0)) < DEFAULT_MODEL_CACHE_TTL:
                models = cached.get("models", [])
                if isinstance(models, list) and models:
                    return [m for m in models if is_free_model(m.get("id", ""), m)]

        headers = {"User-Agent": f"cue-zsh/{__version__}"}
        key = (api_key or self.config.get("openrouter_api_key", "")).strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"

        req = urllib.request.Request(f"{OPENROUTER_BASE_URL}/models", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_context) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
            append_log(f"Failed to refresh OpenRouter models: {exc}", "WARN", self.home)
            cached = read_json_safe(cache_path, {})
            if isinstance(cached, dict) and isinstance(cached.get("models"), list):
                return [m for m in cached["models"] if is_free_model(m.get("id", ""), m)]
            return [{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS]

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
            atomic_write_json(cache_path, {"fetched_at": now, "models": free_models})
            return free_models

        return [{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS]

    def _load_health(self) -> dict[str, Any]:
        raw = read_json_safe(self.paths["model_health"], {})
        return raw if isinstance(raw, dict) else {}

    def _save_health(self, health: dict[str, Any]) -> None:
        atomic_write_json(self.paths["model_health"], health)

    def mark_model_cooldown(self, model_id: str, reason: str, cooldown_seconds: int) -> None:
        """Record a temporary cooldown on a free model when it hits a limit or fails."""
        # Meta-router openrouter/free picks a different backend per request, so keep its cooldown brief
        if model_id == "openrouter/free":
            cooldown_seconds = min(cooldown_seconds, 15)
        with self._health_lock:
            health = self._load_health()
            prev = health.get(model_id, {}) if isinstance(health.get(model_id), dict) else {}
            health[model_id] = {
                "cooldown_until": int(time.time()) + cooldown_seconds,
                "reason": reason[:200],
                "latency_ms": prev.get("latency_ms", 0),
                "updated_at": int(time.time()),
            }
            self._save_health(health)

    def record_model_latency(self, model_id: str, latency_ms: float) -> None:
        """Track rolling average latency (EMA) to prefer fastest healthy free models."""
        with self._health_lock:
            health = self._load_health()
            prev = health.get(model_id, {}) if isinstance(health.get(model_id), dict) else {}
            old_ms = float(prev.get("latency_ms", 0) or 0)
            ema_ms = round(latency_ms if old_ms <= 0 else (old_ms * 0.6 + latency_ms * 0.4), 1)
            health[model_id] = {
                "cooldown_until": 0,
                "reason": "",
                "latency_ms": ema_ms,
                "updated_at": int(time.time()),
            }
            self._save_health(health)

    def clear_model_health(self) -> None:
        """Clear all model cooldowns."""
        self._save_health({})

    def get_candidate_models(self) -> list[str]:
        """Return ordered list of healthy :free models prioritized by preference and low latency."""
        free_models = self.fetch_free_models(force_refresh=False)
        available_ids = {m["id"] for m in free_models if is_free_model(m.get("id", ""), m)}
        if not available_ids:
            available_ids = set(DEFAULT_FREE_MODELS)

        ordered: list[str] = []
        preferred = str(self.config.get("preferred_model", "")).strip()
        state = load_state(self.home)
        active = str(state.get("active_model", "")).strip()

        for candidate in [active, preferred, *DEFAULT_FREE_MODELS]:
            if candidate and is_free_model(candidate) and candidate in available_ids and candidate not in ordered:
                ordered.append(candidate)

        # Append any other discovered free models, preferring text/coder models over vision (-vl)
        extra = sorted(
            [m for m in free_models if m["id"] not in ordered and is_free_model(m["id"], m)],
            key=lambda m: (
                1 if ("-vl" in m["id"].lower() or "vision" in m["id"].lower()) else 0,
                -int(m.get("context_length", 0)),
            ),
        )
        for m in extra:
            ordered.append(m["id"])

        now = int(time.time())
        health = self._load_health()
        healthy = [mid for mid in ordered if int(health.get(mid, {}).get("cooldown_until", 0)) <= now]
        if healthy:
            def _rank_key(mid: str) -> tuple[int, float]:
                pos = ordered.index(mid)
                lat = float(health.get(mid, {}).get("latency_ms", 0) or 0)
                is_slow = 1 if lat > 3500 else 0
                return (is_slow, lat if lat > 0 else 900.0 + pos * 50.0)

            return sorted(healthy, key=_rank_key)

        return sorted(ordered, key=lambda mid: int(health.get(mid, {}).get("cooldown_until", 0)))

    def get_models_status(self) -> list[dict[str, Any]]:
        """Return free models with live availability/cooldown status for `cue models`."""
        now = int(time.time())
        health = self._load_health()
        state = load_state(self.home)
        active = state.get("active_model", "")
        free_models = self.fetch_free_models(force_refresh=False)
        by_id = {m["id"]: m for m in free_models}

        rows: list[dict[str, Any]] = []
        for mid in self.get_candidate_models():
            meta = by_id.get(mid, {"id": mid, "name": mid, "context_length": 32768})
            h = health.get(mid, {})
            cooldown_rem = max(0, int(h.get("cooldown_until", 0)) - now)
            rows.append(
                {
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

    @staticmethod
    def _extract_error_detail(body: str) -> str:
        """Extract concise human-readable error message from OpenRouter JSON error payload."""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                raw_msg = str(parsed["error"].get("metadata", {}).get("raw", "") or parsed["error"].get("message", ""))
                if raw_msg:
                    return " ".join(raw_msg.splitlines()).strip()[:90]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return " ".join(body.splitlines()).strip()[:90]

    def _classify_failure(self, code: int | None, body: str) -> tuple[bool, int, str]:
        """Return (is_auth_fatal, cooldown_seconds, short_reason)."""
        detail = self._extract_error_detail(body)
        lower = (detail + " " + body).lower()
        if code in (401, 403):
            return True, 0, f"HTTP {code}: invalid or unauthorized API key"
        if "data policy" in lower or "privacy" in lower:
            return True, 0, "OpenRouter privacy blocks :free endpoints (enable at openrouter.ai/settings/privacy)"
        if code == 429 or "rate limit" in lower or "quota" in lower or "limit exceeded" in lower:
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
            return False, 24 * 3600, "Model requires payment (skipping to free model)"
        if code in (500, 502, 503, 504) or "overload" in lower or "timeout" in lower:
            return False, 2 * 60, f"Provider transient error ({code or 'timeout'})"
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
        # Strip accidental markdown backticks or leading '$ ' prompt copied by LLM
        if len(cmd) >= 2 and cmd.startswith("`") and cmd.endswith("`"):
            cmd = cmd[1:-1].strip()
        if cmd.startswith("$ ") and len(cmd) > 2:
            cmd = cmd[2:].strip()
        if not cmd or len(cmd) > 220:
            return ""
        # Reject unexpanded template placeholders and catastrophic destructive commands
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

            # Keep original relative order inside each relevance tier
            cleaned = [
                item
                for _, item in sorted(
                    enumerate(cleaned),
                    key=lambda pair: (_comp_rank(pair[1])[0], pair[0]),
                )
            ]
        return cleaned[:12]

    def generate_suggestions(
        self,
        project_ctx: dict[str, Any],
        partial_input: str = "",
        cwd: str = "",
        mode: str = "analyze",
        error_context: str = "",
        timeout: float = 9.0,
    ) -> dict[str, Any]:
        """Request smart command suggestions from OpenRouter free models, switching models on limits."""
        api_key = str(self.config.get("openrouter_api_key", "")).strip()
        if not api_key:
            raise AuthError("No OpenRouter API key configured. Run `cue token` or `cue settings`.")

        candidates = self.get_candidate_models()
        messages = self._build_messages(
            project_ctx,
            partial_input=partial_input,
            cwd=cwd,
            mode=mode,
            error_context=error_context,
        )
        state = load_state(self.home)
        prev_model = state.get("active_model", "")

        errors: list[str] = []
        max_attempts = min(len(candidates), 12)
        per_req_timeout = max(4.0, min(float(timeout), 10.0))
        deadline = time.monotonic() + max(per_req_timeout * 2.5, 16.0)

        for idx, model_id in enumerate(candidates[:max_attempts]):
            if not is_free_model(model_id):
                continue
            if idx > 0 and time.monotonic() >= deadline:
                break
            t0 = time.monotonic()
            try:
                req_msgs = messages
                try:
                    resp = self._post_chat_completion(
                        api_key, model_id, req_msgs, timeout=per_req_timeout
                    )
                except urllib.error.HTTPError as exc:
                    err_body = ""
                    try:
                        err_body = exc.read().decode("utf-8", errors="replace")
                    except OSError:
                        pass
                    if self._needs_system_role_fallback(exc.code, err_body):
                        req_msgs = self._merge_system_into_user(messages)
                        resp = self._post_chat_completion(
                            api_key, model_id, req_msgs, timeout=per_req_timeout
                        )
                    else:
                        raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_body.encode("utf-8"))) from exc

                if isinstance(resp, dict) and "error" in resp:
                    err_obj = resp["error"]
                    err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                    err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                    int_code = int(err_code) if isinstance(err_code, int) else None
                    if self._needs_system_role_fallback(int_code, err_msg):
                        req_msgs = self._merge_system_into_user(messages)
                        resp = self._post_chat_completion(
                            api_key, model_id, req_msgs, timeout=per_req_timeout
                        )
                    if isinstance(resp, dict) and "error" in resp:
                        err_obj = resp["error"]
                        err_code = err_obj.get("code") if isinstance(err_obj, dict) else None
                        err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                        is_auth, cd, reason = self._classify_failure(
                            int(err_code) if isinstance(err_code, int) else None, err_msg
                        )
                        if is_auth:
                            raise AuthError(reason)
                        self.mark_model_cooldown(model_id, reason, cd)
                        errors.append(f"{model_id}: {reason}")
                        append_log(f"Switching from {model_id} ({reason})", "WARN", self.home)
                        continue

                choices = resp.get("choices", []) if isinstance(resp, dict) else []
                content = ""
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message", {})
                    if isinstance(msg, dict):
                        content = str(msg.get("content") or "")
                        if not content.strip() and msg.get("reasoning"):
                            content = str(msg.get("reasoning") or "")

                parsed = _extract_json_object(content)
                if not parsed or not isinstance(parsed.get("suggestions"), list):
                    self.mark_model_cooldown(model_id, "Malformed JSON response", 3 * 60)
                    errors.append(f"{model_id}: malformed JSON")
                    append_log(f"Model {model_id} returned malformed JSON ({len(content)} chars).", "WARN", self.home)
                    continue

                clean_suggestions = self._clean_and_rank_suggestions(
                    parsed["suggestions"],
                    partial_input=partial_input,
                    mode=mode,
                )

                if not clean_suggestions:
                    self.mark_model_cooldown(model_id, "Empty suggestion list", 3 * 60)
                    errors.append(f"{model_id}: empty suggestions")
                    continue

                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.record_model_latency(model_id, elapsed_ms)
                switched = bool(prev_model and prev_model != model_id)
                state["active_model"] = model_id
                state["last_ai_error"] = ""
                state["last_ai_error_at"] = 0
                state["total_ai_calls"] = int(state.get("total_ai_calls", 0)) + 1
                if switched or idx > 0:
                    state["total_fallbacks"] = int(state.get("total_fallbacks", 0)) + 1
                    state["last_switch_reason"] = errors[-1] if errors else "Model rotated"
                    state["last_switch_at"] = int(time.time())
                save_state(state, self.home)

                return {
                    "model": model_id,
                    "latency_ms": round(elapsed_ms, 1),
                    "switched": switched or idx > 0,
                    "suggestions": clean_suggestions[:12],
                    "summary": str(parsed.get("summary", "")).strip()[:400],
                }

            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except OSError:
                    pass
                is_auth, cd, reason = self._classify_failure(exc.code, body)
                if is_auth:
                    state["last_ai_error"] = reason
                    state["last_ai_error_at"] = int(time.time())
                    save_state(state, self.home)
                    raise AuthError(reason) from exc
                self.mark_model_cooldown(model_id, reason, cd)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {model_id} hit {reason}; switching.", "WARN", self.home)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                reason = f"Connection/timeout error: {exc}"
                self.mark_model_cooldown(model_id, reason, 3 * 60)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {model_id} error: {exc}; switching.", "WARN", self.home)

        final_msg = "All tried free models failed: " + "; ".join(errors[-3:])
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
