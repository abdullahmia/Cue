"""OpenRouter API client enforcing :free models only, health cooldowns, and fallback switching."""

from __future__ import annotations

import http.client
import io
import json
import os
import platform
import re
import ssl
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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse JSON from model output, tolerating markdown fences or surrounding prose."""
    cleaned = text.strip()
    if not cleaned:
        return None
    # Strip <think>...</think> blocks emitted by reasoning models like deepseek-r1
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

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
    return None


class OpenRouterClient:
    """OpenRouter client that only uses :free models and switches automatically on rate limits."""

    def __init__(self, home: Path | None = None, config: dict[str, Any] | None = None) -> None:
        self.paths = ensure_cue_dirs(home)
        self.home = self.paths["root"]
        self.config = config or load_config(self.home)
        self._ssl_context = _build_ssl_context()
        self._http_conn: http.client.HTTPSConnection | None = None

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

        # Append any other discovered free models sorted by larger context length
        extra = sorted(
            [m for m in free_models if m["id"] not in ordered and is_free_model(m["id"], m)],
            key=lambda m: int(m.get("context_length", 0)),
            reverse=True,
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

    def _post_chat_completion(
        self,
        api_key: str,
        model_id: str,
        messages: list[dict[str, str]],
        timeout: float = 9.0,
    ) -> dict[str, Any]:
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.15,
            "max_tokens": 260,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = self._headers(api_key)
        headers["Connection"] = "keep-alive"

        for attempt in range(2):
            try:
                if self._http_conn is None:
                    self._http_conn = http.client.HTTPSConnection(
                        "openrouter.ai", 443, timeout=timeout, context=self._ssl_context
                    )
                else:
                    self._http_conn.timeout = timeout
                self._http_conn.request("POST", "/api/v1/chat/completions", body=body, headers=headers)
                resp = self._http_conn.getresponse()
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
                if self._http_conn is not None:
                    try:
                        self._http_conn.close()
                    except OSError:
                        pass
                    self._http_conn = None
                if attempt == 1:
                    raise
        raise TimeoutError("OpenRouter keep-alive request failed")

    def _classify_failure(self, code: int | None, body: str) -> tuple[bool, int, str]:
        """Return (is_auth_fatal, cooldown_seconds, short_reason)."""
        lower = body.lower()
        if code in (401, 403):
            return True, 0, f"HTTP {code}: invalid or unauthorized API key"
        if code == 429 or "rate limit" in lower or "quota" in lower or "limit exceeded" in lower:
            is_daily = "daily" in lower or "per-day" in lower or "day" in lower or "credits" in lower
            cooldown = 6 * 3600 if is_daily else 10 * 60
            return False, cooldown, f"Rate/quota limit reached ({code or 'API'})"
        if code == 404 or "no endpoints found" in lower or "not found" in lower:
            return False, 24 * 3600, f"Model unavailable ({code or '404'})"
        if code == 402:
            return False, 24 * 3600, "Model requires payment (skipping to free model)"
        if code in (500, 502, 503, 504) or "overload" in lower or "timeout" in lower:
            return False, 5 * 60, f"Provider transient error ({code or 'timeout'})"
        return False, 5 * 60, f"Model error ({code or 'unknown'}): {body[:80]}"

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
            raise AuthError("No OpenRouter API key configured. Run `cue onboard`.")

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
        max_attempts = min(len(candidates), 6)

        for idx, model_id in enumerate(candidates[:max_attempts]):
            if not is_free_model(model_id):
                continue
            t0 = time.monotonic()
            try:
                resp = self._post_chat_completion(api_key, model_id, messages, timeout=timeout)
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
                        content = str(msg.get("content", ""))

                parsed = _extract_json_object(content)
                if not parsed or not isinstance(parsed.get("suggestions"), list):
                    self.mark_model_cooldown(model_id, "Malformed JSON response", 3 * 60)
                    errors.append(f"{model_id}: malformed JSON")
                    continue

                clean_suggestions: list[dict[str, str]] = []
                for item in parsed["suggestions"]:
                    if not isinstance(item, dict):
                        continue
                    cmd = str(item.get("cmd", "")).strip()
                    reason = str(item.get("reason", "")).strip()
                    if cmd and "\n" not in cmd and len(cmd) <= 220:
                        clean_suggestions.append({"cmd": cmd, "reason": reason[:90]})

                if not clean_suggestions:
                    self.mark_model_cooldown(model_id, "Empty suggestion list", 3 * 60)
                    errors.append(f"{model_id}: empty suggestions")
                    continue

                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.record_model_latency(model_id, elapsed_ms)
                switched = bool(prev_model and prev_model != model_id)
                state["active_model"] = model_id
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
                    raise AuthError(reason) from exc
                self.mark_model_cooldown(model_id, reason, cd)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {model_id} hit {reason}; switching.", "WARN", self.home)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                reason = f"Connection/timeout error: {exc}"
                self.mark_model_cooldown(model_id, reason, 3 * 60)
                errors.append(f"{model_id}: {reason}")
                append_log(f"Model {model_id} error: {exc}; switching.", "WARN", self.home)

        raise AllModelsExhaustedError("All tried free models failed: " + "; ".join(errors[-3:]))

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

        system_prompt = (
            f"You are cue ({os_name}/{shell_name} CLI assistant). "
            'Return ONLY JSON: {"suggestions":[{"cmd":"<cmd>","reason":"<3-7 words>"}],"summary":"<concise answer or note>"}. '
            "Give up to 12 safe, runnable single-line commands (aim for 6-12 relevant options). "
            "Use the detected package manager and recent commands. "
            "If mode=='fix', suggest exact commands to diagnose or resolve the failed command. "
            "If mode=='prompt', treat 'input' as a natural-language question or task: "
            "if it asks a question, give a direct concise answer in 'summary', and give up to 12 exact safe shell commands in 'suggestions'."
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

        summary = str(ai_ctx.get("summary", "")).strip()
        if summary:
            messages.append({"role": "system", "content": f"Context: {summary[:260]}"})

        for turn in ai_ctx.get("turns", [])[-2:]:
            role = turn.get("role", "user")
            content = str(turn.get("content", "")).strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:300]})

        compact_project: dict[str, Any] = {
            "project": project_ctx.get("name", ""),
            "cwd": cwd or project_ctx.get("project_path", ""),
            "stacks": analysis.get("stacks", []),
            "pm": analysis.get("package_manager", ""),
            "scripts": [s.get("cmd") for s in analysis.get("scripts", [])[:8]],
            "git_branch": analysis.get("git", {}).get("branch", ""),
            "git_dirty": analysis.get("git", {}).get("dirty", False),
            "recent": [{"c": c.get("cmd"), "e": c.get("exit_code", 0)} for c in recent_cmds],
            "mode": mode,
        }
        if mode == "prompt":
            from . import __version__

            compact_project["cue_version"] = __version__
        if partial_input:
            compact_project["input"] = partial_input
        if error_context:
            compact_project["error"] = error_context[:300]

        messages.append({"role": "user", "content": json.dumps(compact_project, separators=(",", ":"), ensure_ascii=False)})
        return messages
