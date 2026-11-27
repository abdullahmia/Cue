"""Lightweight persistent local HTTP daemon for ultra-low-latency OpenRouter keep-alive & caching."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    append_log,
    atomic_write_json,
    ensure_cue_dirs,
    get_configured_providers,
    load_config,
    provider_is_configured,
    read_json_safe,
)
from .context_manager import ContextManager, make_project_id, resolve_project_root
from .error_fixer import diagnose_command_failure
from .openrouter_client import AllModelsExhaustedError, AuthError, OpenRouterClient
from .project_analyzer import analyze_project, compute_fingerprint, is_project_directory
from .suggester import build_ranked_suggestions, write_zsh_suggestion_cache

DAEMON_IDLE_TIMEOUT = 1800  # Exit after 30 minutes of inactivity


def get_daemon_meta_path(home: Path | None = None) -> Path:
    return ensure_cue_dirs(home)["cache"] / "daemon.json"


def _daemon_matches_current(meta: dict[str, Any]) -> bool:
    """Return True if running daemon matches the current cue version and plugin path."""
    root_dir = str(Path(__file__).resolve().parent.parent)
    return meta.get("version") == __version__ and meta.get("plugin_dir") == root_dir


def _stop_daemon_meta(meta: dict[str, Any], meta_path: Path) -> None:
    pid = int(meta.get("pid", 0) or 0)
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_daemon_alive(meta: dict[str, Any]) -> bool:
    port = int(meta.get("port", 0))
    token = str(meta.get("token", ""))
    if not port or not token:
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.35)
        conn.request("GET", "/health", headers={"X-Cue-Token": token})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except OSError:
        return False


def request_daemon(
    endpoint: str,
    payload: dict[str, Any],
    home: Path | None = None,
    timeout: float = 12.0,
) -> dict[str, Any] | None:
    """Send a fast localhost request to the cue keep-alive daemon if active."""
    meta_path = get_daemon_meta_path(home)
    meta = read_json_safe(meta_path, {})
    if not isinstance(meta, dict) or not meta.get("port") or not meta.get("token"):
        return None
    if not _daemon_matches_current(meta):
        _stop_daemon_meta(meta, meta_path)
        ensure_daemon_running(home)
        return None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(meta["port"]), timeout=timeout)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request(
            "POST",
            endpoint,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Cue-Token": str(meta["token"]),
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status == 200:
            return json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


def ensure_daemon_running(home: Path | None = None) -> bool:
    """Start the background keep-alive daemon detached if not already running cleanly."""
    meta_path = get_daemon_meta_path(home)
    meta = read_json_safe(meta_path, {})
    if isinstance(meta, dict) and _is_daemon_alive(meta):
        if _daemon_matches_current(meta):
            return True
        _stop_daemon_meta(meta, meta_path)

    root_dir = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if home is not None:
        env["CUE_HOME"] = str(home)

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([sys.executable, "-m", "engine.cli", "daemon", "--serve"], **kwargs)
        return True
    except OSError:
        return False


def run_daemon_server(home: Path | None = None) -> int:
    """Run the localhost keep-alive OpenRouter & context daemon."""
    paths = ensure_cue_dirs(home)
    cue_home = paths["root"]
    meta_path = get_daemon_meta_path(cue_home)
    existing = read_json_safe(meta_path, {})
    if isinstance(existing, dict) and _is_daemon_alive(existing):
        return 0

    token = secrets.token_hex(16)
    last_activity = [time.time()]
    lock = threading.Lock()
    client_holder = [OpenRouterClient(home=cue_home)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth_ok(self) -> bool:
            return self.headers.get("X-Cue-Token") == token

        def do_GET(self) -> None:
            if not self._auth_ok():
                self._send_json(403, {"error": "forbidden"})
                return
            if self.path == "/health":
                last_activity[0] = time.time()
                self._send_json(200, {"ok": True, "pid": os.getpid()})
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._auth_ok():
                self._send_json(403, {"error": "forbidden"})
                return
            last_activity[0] = time.time()
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 1024 * 1024)).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid json"})
                return

            with lock:
                cfg = load_config(cue_home)
                cm = ContextManager(home=cue_home, config=cfg)
                client = client_holder[0]
                client.config = cfg

                if self.path == "/reload-config":
                    client.clear_model_health()
                    self._send_json(200, {"ok": True})
                    return

                directory = Path(body.get("dir") or os.getcwd()).expanduser().resolve()
                ai_enabled = bool(cfg.get("ai_suggestions_enabled", False))
                has_key = provider_is_configured(cfg) or (
                    bool(cfg.get("auto_provider_fallback", True)) and bool(get_configured_providers(cfg))
                )

                if self.path == "/analyze":
                    root = resolve_project_root(directory)
                    force = bool(body.get("force", False))
                    has_existing = cm.has_project(root)
                    if not force and not has_existing and not is_project_directory(root):
                        self._send_json(
                            200,
                            {
                                "project_id": "",
                                "is_project": False,
                                "skipped": True,
                                "cache_file": "",
                                "analysis": {"root": str(root), "name": root.name, "is_project": False, "stacks": []},
                                "suggestions": [],
                            },
                        )
                        return

                    ctx = cm.load_project(directory)
                    fp = compute_fingerprint(root)
                    now = int(time.time())
                    needs_ai = (
                        force
                        or not ctx.get("cached_suggestions")
                        or fp != ctx.get("fingerprint")
                        or (now - int(ctx.get("last_analyzed", 0)) > 1800)
                    )
                    if force or fp != ctx.get("fingerprint") or not ctx.get("analysis"):
                        ctx["analysis"] = analyze_project(root)
                        ctx["fingerprint"] = fp
                        ctx["last_analyzed"] = now
                    if needs_ai and not body.get("no_ai") and ai_enabled and has_key:
                        try:
                            res = client.generate_suggestions(ctx, cwd=str(directory), mode="analyze", timeout=8.0)
                            ctx["cached_suggestions"] = res["suggestions"]
                            if res.get("summary"):
                                ctx.setdefault("ai_context", {})["summary"] = res["summary"]
                            cm.append_ai_turn(ctx, f"Analyzed {root.name}", res.get("summary", ""), res["model"])
                        except (AuthError, AllModelsExhaustedError) as exc:
                            append_log(f"Daemon analyze AI skipped: {exc}", "WARN", cue_home)
                    cm.save_project(ctx)
                    cache_file = write_zsh_suggestion_cache(ctx, home=cue_home, ai_enabled=ai_enabled)
                    self._send_json(
                        200,
                        {
                            "project_id": ctx["project_id"],
                            "is_project": True,
                            "skipped": False,
                            "cache_file": str(cache_file),
                            "analysis": ctx.get("analysis", {}),
                            "suggestions": build_ranked_suggestions(ctx, ai_enabled=ai_enabled),
                        },
                    )
                    return

                if self.path == "/record":
                    cmd_str = str(body.get("command", ""))
                    exit_code = int(body.get("exit_code", 0))
                    ctx = cm.record_command(directory, cmd_str, exit_code=exit_code, project_only=True)
                    if ctx is None:
                        fixes_only: list[dict[str, str]] = []
                        diag_text = ""
                        if exit_code not in (0, 130) and cmd_str.strip() and not cmd_str.strip().startswith("cue"):
                            diag = diagnose_command_failure(cmd_str.strip(), exit_code, directory)
                            fixes_only = list(diag.get("fixes", []))
                            diag_text = str(diag.get("diagnosis", ""))
                        self._send_json(
                            200,
                            {
                                "recorded": False,
                                "skipped": True,
                                "last_error": {"cmd": cmd_str.strip(), "exit_code": exit_code, "diagnosis": diag_text, "fixes": fixes_only} if fixes_only else {},
                                "fixes": fixes_only,
                            },
                        )
                        return
                    if exit_code not in (0, 130):
                        diag = diagnose_command_failure(cmd_str, exit_code, directory, ctx.get("analysis"))
                        ctx["pending_retry"] = {}
                        if diag["fixes"]:
                            ctx["last_error"] = {
                                "cmd": cmd_str,
                                "exit_code": exit_code,
                                "diagnosis": diag["diagnosis"],
                                "fixes": diag["fixes"],
                                "ts": int(time.time()),
                            }
                            if ai_enabled and has_key and body.get("ai_fix", True):
                                try:
                                    res = client.generate_suggestions(
                                        ctx,
                                        partial_input=cmd_str,
                                        cwd=str(directory),
                                        mode="fix",
                                        error_context=diag["diagnosis"],
                                        timeout=7.5,
                                    )
                                    seen = {f["cmd"] for f in diag["fixes"]}
                                    merged = list(diag["fixes"])
                                    for s in res["suggestions"]:
                                        if s["cmd"] not in seen:
                                            merged.append({"cmd": s["cmd"], "reason": s["reason"], "source": "ai"})
                                    ctx["last_error"]["fixes"] = merged[:8]
                                except (AuthError, AllModelsExhaustedError):
                                    pass
                        else:
                            ctx["last_error"] = {"cmd": cmd_str, "exit_code": exit_code, "fixes": [], "ts": int(time.time())}
                    else:
                        prev_err = ctx.get("last_error", {})
                        if isinstance(prev_err, dict) and prev_err.get("cmd") and prev_err.get("cmd") != cmd_str.strip():
                            ctx["pending_retry"] = {"cmd": prev_err["cmd"], "ts": int(time.time())}
                        else:
                            ctx["pending_retry"] = {}
                        ctx["last_error"] = {}
                    cm.save_project(ctx)
                    write_zsh_suggestion_cache(ctx, home=cue_home, ai_enabled=ai_enabled)
                    self._send_json(200, {"recorded": True, "last_error": ctx.get("last_error", {}), "pending_retry": ctx.get("pending_retry", {})})
                    return

                if self.path == "/suggest":
                    root = resolve_project_root(directory)
                    is_tracked = cm.has_project(root) or is_project_directory(root)
                    ctx = cm.load_project(directory)
                    if not ctx.get("analysis"):
                        ctx["analysis"] = analyze_project(root)
                        ctx["fingerprint"] = ctx["analysis"].get("fingerprint", "")
                    query = str(body.get("input", ""))
                    req_mode = str(body.get("mode", "")).strip() or ("complete" if query else "analyze")
                    prompt_err = ""
                    can_call_ai = has_key and (req_mode == "prompt" or ai_enabled)
                    if body.get("live") and req_mode == "prompt" and not has_key:
                        prompt_err = "No AI provider configured (enable AI in `cue settings` or run: cue token)"
                    elif body.get("live") and can_call_ai:
                        try:
                            res = client.generate_suggestions(
                                ctx,
                                partial_input=query,
                                cwd=str(directory),
                                mode=req_mode,
                                timeout=8.0,
                            )
                            seen = {s["cmd"] for s in res["suggestions"]}
                            merged = list(res["suggestions"])
                            for old in ctx.get("cached_suggestions", []):
                                if old.get("cmd") not in seen:
                                    merged.append(old)
                            ctx["cached_suggestions"] = merged[:16]
                            if is_tracked:
                                cm.save_project(ctx)
                            if req_mode == "prompt":
                                cache_file = write_zsh_suggestion_cache(ctx, home=cue_home, ai_enabled=ai_enabled) if is_tracked else ""
                                self._send_json(
                                    200,
                                    {
                                        "cache_file": str(cache_file),
                                        "model": res.get("model", ""),
                                        "answer": res.get("summary", ""),
                                        "suggestions": [{"cmd": s["cmd"], "source": "ai", "reason": s["reason"]} for s in res["suggestions"]],
                                    },
                                )
                                return
                        except (AuthError, AllModelsExhaustedError) as exc:
                            prompt_err = str(exc)
                            append_log(f"Daemon suggest error: {exc}", "WARN", cue_home)
                    cache_file = write_zsh_suggestion_cache(ctx, home=cue_home, ai_enabled=ai_enabled) if is_tracked else ""
                    payload: dict[str, Any] = {
                        "cache_file": str(cache_file),
                        "suggestions": build_ranked_suggestions(ctx, partial_input=query, limit=int(body.get("limit", 12)), ai_enabled=ai_enabled),
                    }
                    if req_mode == "prompt" and prompt_err:
                        payload["error"] = prompt_err
                    self._send_json(200, payload)
                    return

                self._send_json(404, {"error": "unknown endpoint"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    atomic_write_json(
        meta_path,
        {
            "pid": os.getpid(),
            "port": port,
            "token": token,
            "version": __version__,
            "plugin_dir": str(Path(__file__).resolve().parent.parent),
            "started_at": int(time.time()),
        },
        private=True,
    )

    def _watchdog() -> None:
        while True:
            time.sleep(30)
            if time.time() - last_activity[0] > DAEMON_IDLE_TIMEOUT:
                server.shutdown()
                break

    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if meta_path.exists():
                meta_path.unlink()
        except OSError:
            pass
    return 0
