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
    "google/gemma-3-12b-it:free",
    "qwen/qwen3-coder:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-14b:free",
    "deepseek/deepseek-r1-0528:free",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "openrouter_api_key": "",
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


def load_config(home: Path | None = None) -> dict[str, Any]:
    """Load config.json merged with defaults."""
    paths = ensure_cue_dirs(home)
    raw = read_json_safe(paths["config"], {})
    if not isinstance(raw, dict):
        raw = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw)
    # Enforce free-models-only invariant
    cfg["free_models_only"] = True
    # Allow environment override for API key without mutating disk config
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key and not cfg.get("openrouter_api_key"):
        cfg["openrouter_api_key"] = env_key
    return cfg


def save_config(cfg: dict[str, Any], home: Path | None = None) -> Path:
    """Persist configuration safely with 0600 permissions."""
    paths = ensure_cue_dirs(home)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
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
        "active_model": raw.get("active_model", DEFAULT_FREE_MODELS[0]),
        "last_switch_reason": raw.get("last_switch_reason", ""),
        "last_switch_at": raw.get("last_switch_at", 0),
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
