"""Configuration, ~/.cue directory layout, state, and size-capped logging."""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

DEFAULT_MAX_PROJECT_BYTES = 256 * 1024  # 256 KB per project context file
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20 MB total projects storage
DEFAULT_MAX_LOG_BYTES = 512 * 1024  # 512 KB log cap
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MODEL_CACHE_TTL = 6 * 3600  # 6 hours

DEFAULT_FREE_MODELS = [
    "openrouter/free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "qwen/qwen3-coder:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3.5-lightning:free",
    "google/gemma-3-27b-it:free",
    "google/gemma-3-12b-it:free",
    "qwen/qwen3-14b:free",
    "deepseek/deepseek-r1-0528:free",
]

AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "short_name": "OpenRouter",
        "protocol": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "env_vars": ("OPENROUTER_API_KEY",),
        "key_url": "https://openrouter.ai/keys",
        "key_hint": "sk-or-v1-...",
        "requires_key": True,
        "free_only": True,
        "default_models": list(DEFAULT_FREE_MODELS),
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI (ChatGPT)",
        "short_name": "ChatGPT",
        "protocol": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "env_vars": ("OPENAI_API_KEY",),
        "key_url": "https://platform.openai.com/api-keys",
        "key_hint": "sk-proj-...",
        "requires_key": True,
        "free_only": False,
        "default_models": [
            "gpt-4o-mini",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o",
            "o4-mini",
            "gpt-4.1",
        ],
    },
    "gemini": {
        "id": "gemini",
        "name": "Google Gemini",
        "short_name": "Gemini",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "env_vars": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "key_url": "https://aistudio.google.com/apikey",
        "key_hint": "AIza...",
        "requires_key": True,
        "free_only": False,
        "default_models": [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.5-pro",
        ],
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic (Claude)",
        "short_name": "Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "env_vars": ("ANTHROPIC_API_KEY",),
        "key_url": "https://console.anthropic.com/settings/keys",
        "key_hint": "sk-ant-...",
        "requires_key": True,
        "free_only": False,
        "default_models": [
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-sonnet-4-0",
            "claude-3-5-haiku-latest",
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
        ],
    },
    "groq": {
        "id": "groq",
        "name": "Groq",
        "short_name": "Groq",
        "protocol": "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "env_vars": ("GROQ_API_KEY",),
        "key_url": "https://console.groq.com/keys",
        "key_hint": "gsk_...",
        "requires_key": True,
        "free_only": False,
        "default_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "qwen/qwen3-32b",
            "openai/gpt-oss-20b",
            "gemma2-9b-it",
        ],
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "short_name": "DeepSeek",
        "protocol": "openai_compat",
        "base_url": "https://api.deepseek.com",
        "env_vars": ("DEEPSEEK_API_KEY",),
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_hint": "sk-...",
        "requires_key": True,
        "free_only": False,
        "default_models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "ollama": {
        "id": "ollama",
        "name": "Ollama (Local)",
        "short_name": "Ollama",
        "protocol": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "env_vars": (),
        "key_url": "https://ollama.com",
        "key_hint": "local runtime (no key)",
        "requires_key": False,
        "free_only": False,
        "default_models": [
            "qwen2.5-coder:7b",
            "llama3.2",
            "mistral",
        ],
    },
}

PROVIDER_ALIASES: dict[str, str] = {
    "openrouter": "openrouter",
    "or": "openrouter",
    "openai": "openai",
    "chatgpt": "openai",
    "gpt": "openai",
    "gemini": "gemini",
    "google": "gemini",
    "google-gemini": "gemini",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "groq": "groq",
    "deepseek": "deepseek",
    "ollama": "ollama",
    "local": "ollama",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "ai_provider": "openrouter",
    "openrouter_api_key": "",
    "api_keys": {pid: "" for pid, meta in AI_PROVIDERS.items() if meta["requires_key"]},
    "provider_models": {pid: meta["default_models"][0] for pid, meta in AI_PROVIDERS.items()},
    "ollama_base_url": "http://127.0.0.1:11434",
    "auto_provider_fallback": True,
    "ai_suggestions_enabled": False,
    "preferred_model": "openrouter/free",
    "free_models_only": True,
    "auto_analyze": True,
    "prompt_mode": False,
    "ai_debounce_ms": 350,
    "max_project_bytes": DEFAULT_MAX_PROJECT_BYTES,
    "max_total_bytes": DEFAULT_MAX_TOTAL_BYTES,
    "max_log_bytes": DEFAULT_MAX_LOG_BYTES,
    "legacy_retention_days": DEFAULT_RETENTION_DAYS,
    "max_dropdown_items": 8,
    "onboarded": False,
    "onboarded_at": 0,
}


def normalize_provider(name: str | None) -> str:
    """Resolve provider name or common alias (chatgpt, claude, gemini, ...) to canonical ID."""
    key = (name or "").strip().lower()
    if not key:
        return "openrouter"
    return PROVIDER_ALIASES.get(key, key if key in AI_PROVIDERS else "openrouter")


def is_valid_provider(name: str | None) -> bool:
    """Return True if name resolves to a supported provider ID."""
    key = (name or "").strip().lower()
    return key in PROVIDER_ALIASES or key in AI_PROVIDERS


def list_provider_ids() -> list[str]:
    """Return ordered list of supported AI provider IDs."""
    return list(AI_PROVIDERS.keys())


def get_provider_meta(provider_id: str | None) -> dict[str, Any]:
    """Return metadata dictionary for the normalized AI provider."""
    pid = normalize_provider(provider_id)
    return AI_PROVIDERS[pid]


def get_active_provider(cfg: dict[str, Any]) -> str:
    """Return the active normalized AI provider from config."""
    return normalize_provider(str(cfg.get("ai_provider", "openrouter")))


def get_provider_api_key(cfg: dict[str, Any], provider_id: str | None = None) -> str:
    """Return the configured API key (or env var fallback) for a provider without mutating cfg."""
    pid = normalize_provider(provider_id or str(cfg.get("ai_provider", "openrouter")))
    meta = AI_PROVIDERS[pid]
    if not meta["requires_key"]:
        return ""
    api_keys = cfg.get("api_keys")
    if isinstance(api_keys, dict):
        val = str(api_keys.get(pid, "") or "").strip()
        if val:
            return val
    if pid == "openrouter":
        legacy = str(cfg.get("openrouter_api_key", "") or "").strip()
        if legacy:
            return legacy
    for env_name in meta.get("env_vars", ()):
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    return ""


def set_provider_api_key(cfg: dict[str, Any], provider_id: str, api_key: str) -> None:
    """Store an API key for provider_id cleanly (keeping openrouter_api_key in sync)."""
    pid = normalize_provider(provider_id)
    cleaned = api_key.strip()
    api_keys = cfg.get("api_keys")
    if not isinstance(api_keys, dict):
        api_keys = {k: "" for k in DEFAULT_CONFIG["api_keys"]}
    else:
        api_keys = dict(api_keys)
    api_keys[pid] = cleaned
    cfg["api_keys"] = api_keys
    if pid == "openrouter":
        cfg["openrouter_api_key"] = cleaned


def provider_is_configured(cfg: dict[str, Any], provider_id: str | None = None) -> bool:
    """Return True if the selected (or active) provider can serve AI requests right now."""
    pid = normalize_provider(provider_id or str(cfg.get("ai_provider", "openrouter")))
    meta = AI_PROVIDERS[pid]
    if not meta["requires_key"]:
        return bool(get_ollama_base_url(cfg))
    return bool(get_provider_api_key(cfg, pid))


def get_configured_providers(cfg: dict[str, Any], include_local: bool = False) -> list[str]:
    """Return list of provider IDs with configured credentials in priority order."""
    active = get_active_provider(cfg)
    ordered = [active] + [pid for pid in AI_PROVIDERS if pid != active]
    out: list[str] = []
    for pid in ordered:
        meta = AI_PROVIDERS[pid]
        if not meta["requires_key"]:
            if include_local or pid == active:
                out.append(pid)
            continue
        if get_provider_api_key(cfg, pid):
            out.append(pid)
    return out


def get_provider_preferred_model(cfg: dict[str, Any], provider_id: str | None = None) -> str:
    """Return preferred model ID for the given or active provider."""
    pid = normalize_provider(provider_id or str(cfg.get("ai_provider", "openrouter")))
    pm = cfg.get("provider_models")
    if isinstance(pm, dict):
        model = str(pm.get(pid, "") or "").strip()
        if model:
            return model
    if pid == get_active_provider(cfg):
        pref = str(cfg.get("preferred_model", "") or "").strip()
        if pref:
            return pref
    return AI_PROVIDERS[pid]["default_models"][0]


def set_provider_preferred_model(cfg: dict[str, Any], provider_id: str, model_id: str) -> None:
    """Set preferred model for provider_id and keep preferred_model synced when active."""
    pid = normalize_provider(provider_id)
    pm = cfg.get("provider_models")
    if not isinstance(pm, dict):
        pm = dict(DEFAULT_CONFIG["provider_models"])
    else:
        pm = dict(pm)
    pm[pid] = model_id.strip()
    cfg["provider_models"] = pm
    if pid == get_active_provider(cfg):
        cfg["preferred_model"] = model_id.strip()


def get_ollama_base_url(cfg: dict[str, Any]) -> str:
    """Resolve local/remote Ollama base URL from config or OLLAMA_HOST."""
    raw = str(cfg.get("ollama_base_url", "") or "").strip()
    env_host = os.environ.get("OLLAMA_HOST", "").strip()
    candidate = raw or env_host or "http://127.0.0.1:11434"
    if not candidate.startswith(("http://", "https://")):
        candidate = f"http://{candidate}"
    return candidate.rstrip("/")


def _is_cue_managed_dir(path: Path) -> bool:
    """Return True if path already belongs to the cue plugin."""
    if not path.is_dir():
        return False
    return (
        (path / ".cue_home").exists()
        or (path / "config.json").exists()
        or ((path / "projects").is_dir() and (path / "cache").is_dir())
    )


def resolve_default_cue_home(user_home: Path | None = None) -> Path:
    """Return ~/.cue unless a pre-existing unrelated ~/.cue exists, in which case return ~/.cuee."""
    base = user_home or Path.home()
    primary = base / ".cue"
    fallback = base / ".cuee"

    if _is_cue_managed_dir(primary):
        return primary
    if _is_cue_managed_dir(fallback):
        return fallback
    if primary.exists():
        # ~/.cue already existed beforehand for another tool/purpose -> use ~/.cuee
        return fallback
    return primary


def get_cue_home() -> Path:
    """Return the root ~/.cue (or ~/.cuee) directory path (overridable via CUE_HOME)."""
    env_home = os.environ.get("CUE_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return resolve_default_cue_home()


def migrate_legacy_tawh_home(cue_root: Path, legacy_root: Path | None = None) -> bool:
    """Migrate existing ~/.tawh config & project contexts into ~/.cue once if ~/.cue/config.json is absent."""
    legacy = legacy_root or (Path.home() / ".tawh")
    cue_cfg = cue_root / "config.json"
    legacy_cfg = legacy / "config.json"
    if cue_cfg.exists() or not legacy_cfg.exists() or legacy.resolve() == cue_root.resolve():
        return False

    cue_root.mkdir(parents=True, exist_ok=True)
    for fname in ("config.json", "state.json", "models_cache.json", "model_health.json"):
        src = legacy / fname
        dst = cue_root / fname
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    legacy_projects = legacy / "projects"
    cue_projects = cue_root / "projects"
    if legacy_projects.is_dir():
        cue_projects.mkdir(parents=True, exist_ok=True)
        for pfile in legacy_projects.glob("*.json"):
            dst = cue_projects / pfile.name
            if not dst.exists():
                try:
                    shutil.copy2(pfile, dst)
                except OSError:
                    pass
    return True


def ensure_cue_dirs(home: Path | None = None) -> dict[str, Path]:
    """Ensure the ~/.cue (or ~/.cuee) directory hierarchy exists and return key paths."""
    root = home or get_cue_home()
    default_candidates = {
        (Path.home() / ".cue").expanduser().resolve(),
        (Path.home() / ".cuee").expanduser().resolve(),
    }
    if home is None and root.expanduser().resolve() in default_candidates:
        migrate_legacy_tawh_home(root)
    projects_dir = root / "projects"
    cache_dir = root / "cache"
    logs_dir = root / "logs"

    for d in (root, projects_dir, cache_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    marker = root / ".cue_home"
    if not marker.exists():
        try:
            marker.write_text("cue\n", encoding="utf-8")
        except OSError:
            pass

    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass

    return {
        "root": root,
        "config": root / "config.json",
        "state": root / "state.json",
        "models_cache": root / "models_cache.json",
        "model_health": root / "model_health.json",
        "projects": projects_dir,
        "projects_index": projects_dir / "index.json",
        "cache": cache_dir,
        "logs": logs_dir,
        "log_file": logs_dir / "cue.log",
    }


def atomic_write_json(path: Path, data: Any, private: bool = False) -> None:
    """Write JSON atomically using a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.write("\n")
    if private and os.name != "nt":
        try:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    os.replace(tmp_path, path)


def read_json_safe(path: Path, default: Any = None) -> Any:
    """Read JSON from disk, returning default if missing or corrupted."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _fresh_default_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg["api_keys"] = dict(DEFAULT_CONFIG["api_keys"])
    cfg["provider_models"] = dict(DEFAULT_CONFIG["provider_models"])
    return cfg


def load_config(home: Path | None = None) -> dict[str, Any]:
    """Load config.json merged with defaults."""
    paths = ensure_cue_dirs(home)
    raw = read_json_safe(paths["config"], {})
    if not isinstance(raw, dict):
        raw = {}
    cfg = _fresh_default_config()
    raw_keys = raw.get("api_keys")
    raw_models = raw.get("provider_models")
    cfg.update({k: v for k, v in raw.items() if k not in ("api_keys", "provider_models")})
    if isinstance(raw_keys, dict):
        for k, v in raw_keys.items():
            cfg["api_keys"][normalize_provider(str(k))] = str(v or "").strip()
    if isinstance(raw_models, dict):
        for k, v in raw_models.items():
            if v:
                cfg["provider_models"][normalize_provider(str(k))] = str(v).strip()

    cfg["ai_provider"] = normalize_provider(str(cfg.get("ai_provider", "openrouter")))
    # Enforce free-models-only invariant for OpenRouter
    cfg["free_models_only"] = True

    # Bidirectional sync between legacy openrouter_api_key and api_keys['openrouter']
    legacy_or = str(cfg.get("openrouter_api_key", "") or "").strip()
    nested_or = str(cfg["api_keys"].get("openrouter", "") or "").strip()
    if legacy_or and not nested_or:
        cfg["api_keys"]["openrouter"] = legacy_or
    elif nested_or and not legacy_or:
        cfg["openrouter_api_key"] = nested_or

    # Sync preferred_model with provider_models[ai_provider]
    active_pid = cfg["ai_provider"]
    raw_pref = str(raw.get("preferred_model", "") or "").strip()
    if raw_pref and not (isinstance(raw_models, dict) and raw_models.get(active_pid)):
        cfg["provider_models"][active_pid] = raw_pref
    cfg["preferred_model"] = get_provider_preferred_model(cfg, active_pid)

    # Allow environment override for OpenRouter key without mutating disk when unset
    env_or = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_or and not cfg.get("openrouter_api_key"):
        cfg["openrouter_api_key"] = env_or
        cfg["api_keys"]["openrouter"] = env_or
    return cfg


def save_config(cfg: dict[str, Any], home: Path | None = None) -> Path:
    """Persist configuration safely with 0600 permissions."""
    paths = ensure_cue_dirs(home)
    merged = _fresh_default_config()
    raw_keys = cfg.get("api_keys")
    raw_models = cfg.get("provider_models")
    merged.update({k: v for k, v in cfg.items() if k not in ("api_keys", "provider_models")})
    if isinstance(raw_keys, dict):
        for k, v in raw_keys.items():
            merged["api_keys"][normalize_provider(str(k))] = str(v or "").strip()
    if isinstance(raw_models, dict):
        for k, v in raw_models.items():
            if v:
                merged["provider_models"][normalize_provider(str(k))] = str(v).strip()

    merged["ai_provider"] = normalize_provider(str(merged.get("ai_provider", "openrouter")))
    active_pid = merged["ai_provider"]

    if "openrouter_api_key" in cfg and not (isinstance(raw_keys, dict) and "openrouter" in raw_keys):
        merged["api_keys"]["openrouter"] = str(merged.get("openrouter_api_key", "") or "").strip()
    elif isinstance(raw_keys, dict) and "openrouter" in raw_keys:
        merged["openrouter_api_key"] = merged["api_keys"]["openrouter"]
    else:
        or_val = str(merged["api_keys"].get("openrouter", "") or merged.get("openrouter_api_key", "") or "").strip()
        merged["openrouter_api_key"] = or_val
        merged["api_keys"]["openrouter"] = or_val

    if "preferred_model" in cfg and cfg["preferred_model"] and not (isinstance(raw_models, dict) and active_pid in raw_models):
        merged["provider_models"][active_pid] = str(cfg["preferred_model"]).strip()
    merged["preferred_model"] = get_provider_preferred_model(merged, active_pid)
    merged["free_models_only"] = True
    atomic_write_json(paths["config"], merged, private=True)
    return paths["config"]


def load_state(home: Path | None = None) -> dict[str, Any]:
    """Load runtime state.json."""
    paths = ensure_cue_dirs(home)
    raw = read_json_safe(paths["state"], {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "active_provider": normalize_provider(str(raw.get("active_provider", "openrouter"))),
        "active_model": raw.get("active_model", DEFAULT_FREE_MODELS[0]),
        "last_switch_reason": raw.get("last_switch_reason", ""),
        "last_switch_at": raw.get("last_switch_at", 0),
        "last_ai_error": raw.get("last_ai_error", ""),
        "last_ai_error_at": raw.get("last_ai_error_at", 0),
        "total_ai_calls": raw.get("total_ai_calls", 0),
        "total_fallbacks": raw.get("total_fallbacks", 0),
    }


def save_state(state: dict[str, Any], home: Path | None = None) -> None:
    """Save runtime state.json."""
    paths = ensure_cue_dirs(home)
    atomic_write_json(paths["state"], state)


def append_log(message: str, level: str = "INFO", home: Path | None = None, max_bytes: int | None = None) -> None:
    """Append a line to ~/.cue/logs/cue.log, rotating if above size cap."""
    paths = ensure_cue_dirs(home)
    log_path = paths["log_file"]
    limit = max_bytes or DEFAULT_MAX_LOG_BYTES
    try:
        if log_path.exists() and log_path.stat().st_size >= limit:
            backup = log_path.with_name("cue.log.1")
            if backup.exists():
                backup.unlink()
            os.replace(log_path, backup)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [{level}] {message}\n")
    except OSError:
        pass
