"""Per-project context storage inside ~/.cue/projects with strict size caps & legacy cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_MAX_PROJECT_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    atomic_write_json,
    ensure_cue_dirs,
    load_config,
    read_json_safe,
)
from .project_analyzer import is_project_directory

SENSITIVE_CMD_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|auth)\s*="),
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
]

MAX_RECENT_COMMANDS = 80
MAX_AI_TURNS = 12
MAX_CACHED_SUGGESTIONS = 16


def is_sensitive_command(cmd: str) -> bool:
    """Return True if a command looks like it contains inline credentials or secrets."""
    return any(pattern.search(cmd) for pattern in SENSITIVE_CMD_PATTERNS)


def resolve_project_root(directory: str | Path) -> Path:
    """Resolve the canonical project root (git top-level if inside a git repo, else directory)."""
    path = Path(directory).expanduser().resolve()
    if path.is_file():
        path = path.parent
    cur = path
    home = Path.home().resolve()
    while True:
        if (cur / ".git").exists():
            # Avoid treating $HOME itself as a project if someone accidentally git-inited ~
            if cur != home:
                return cur
        if cur == cur.parent or cur == home:
            break
        cur = cur.parent
    return path


def make_project_id(project_path: str | Path) -> str:
    """Create a readable, collision-resistant project ID like 'cue-a1b2c3d4'."""
    resolved = str(Path(project_path).resolve())
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(resolved).name).strip("-").lower() or "root"
    slug = slug[:32]
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


class ContextManager:
    """Manages ~/.cue/projects context files, size budgets, and legacy project cleanup."""

    def __init__(self, home: Path | None = None, config: dict[str, Any] | None = None) -> None:
        self.paths = ensure_cue_dirs(home)
        self.home = self.paths["root"]
        self.config = config or load_config(self.home)
        self.max_project_bytes = int(self.config.get("max_project_bytes", DEFAULT_MAX_PROJECT_BYTES))
        self.max_total_bytes = int(self.config.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES))

    def project_file(self, project_id: str) -> Path:
        """Return the JSON file path for a given project ID."""
        return self.paths["projects"] / f"{project_id}.json"

    def _load_index(self) -> dict[str, Any]:
        raw = read_json_safe(self.paths["projects_index"], {})
        if not isinstance(raw, dict):
            return {}
        return raw

    def _save_index(self, index: dict[str, Any]) -> None:
        atomic_write_json(self.paths["projects_index"], index)

    def _default_context(self, project_path: Path, project_id: str) -> dict[str, Any]:
        now = int(time.time())
        return {
            "version": 1,
            "project_id": project_id,
            "project_path": str(project_path),
            "name": project_path.name or str(project_path),
            "created_at": now,
            "last_accessed": now,
            "last_analyzed": 0,
            "fingerprint": "",
            "analysis": {},
            "recent_commands": [],
            "ai_context": {
                "summary": "",
                "turns": [],
                "active_model": "",
            },
            "cached_suggestions": [],
        }

    def has_project(self, directory: str | Path) -> bool:
        """Return True if a stored context file already exists for directory's project root."""
        root = resolve_project_root(directory)
        return self.project_file(make_project_id(root)).exists()

    def load_project(self, directory: str | Path) -> dict[str, Any]:
        """Load (or initialize in memory) context for the project containing directory."""
        root = resolve_project_root(directory)
        pid = make_project_id(root)
        pfile = self.project_file(pid)
        data = read_json_safe(pfile)
        if not isinstance(data, dict):
            data = self._default_context(root, pid)
        data["last_accessed"] = int(time.time())
        return data

    def save_project(self, ctx: dict[str, Any]) -> Path:
        """Compact context to fit size limits, save it to ~/.cue/projects, and enforce total cap."""
        pid = ctx["project_id"]
        ctx["last_accessed"] = int(time.time())
        compacted = self._enforce_project_size(ctx)
        pfile = self.project_file(pid)
        atomic_write_json(pfile, compacted)

        size_bytes = pfile.stat().st_size if pfile.exists() else 0
        index = self._load_index()
        index[pid] = {
            "project_id": pid,
            "project_path": compacted.get("project_path", ""),
            "name": compacted.get("name", pid),
            "stacks": compacted.get("analysis", {}).get("stacks", [])[:6],
            "last_accessed": compacted["last_accessed"],
            "size_bytes": size_bytes,
            "command_count": len(compacted.get("recent_commands", [])),
        }
        self._save_index(index)
        self.enforce_total_size(protected_id=pid)
        return pfile

    def _serialized_size(self, data: dict[str, Any]) -> int:
        return len(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")) + 1

    def _enforce_project_size(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Shrink oldest context segments deterministically until under max_project_bytes."""
        limit = max(8192, self.max_project_bytes)

        # Hard caps first
        ai_ctx = ctx.setdefault("ai_context", {"summary": "", "turns": [], "active_model": ""})
        turns = ai_ctx.get("turns", [])
        if len(turns) > MAX_AI_TURNS:
            ai_ctx["turns"] = turns[-MAX_AI_TURNS:]

        cmds = ctx.get("recent_commands", [])
        if len(cmds) > MAX_RECENT_COMMANDS:
            ctx["recent_commands"] = cmds[-MAX_RECENT_COMMANDS:]

        sugs = ctx.get("cached_suggestions", [])
        if len(sugs) > MAX_CACHED_SUGGESTIONS:
            ctx["cached_suggestions"] = sugs[:MAX_CACHED_SUGGESTIONS]

        if self._serialized_size(ctx) <= limit:
            return ctx

        # Step 1: Trim AI turns from oldest, folding brief note into summary
        while len(ai_ctx.get("turns", [])) > 2 and self._serialized_size(ctx) > limit:
            dropped = ai_ctx["turns"].pop(0)
            snippet = str(dropped.get("content", ""))[:120].replace("\n", " ")
            if snippet:
                summary = ai_ctx.get("summary", "")
                ai_ctx["summary"] = (summary + " | " + snippet).strip(" |")[-1000:]

        # Step 2: Trim older recent_commands
        while len(ctx.get("recent_commands", [])) > 10 and self._serialized_size(ctx) > limit:
            ctx["recent_commands"] = ctx["recent_commands"][len(ctx["recent_commands"]) // 4 :]

        # Step 3: Compact analysis tree/scripts if still oversized
        analysis = ctx.get("analysis", {})
        if isinstance(analysis, dict) and self._serialized_size(ctx) > limit:
            if isinstance(analysis.get("tree"), list):
                analysis["tree"] = analysis["tree"][:15]
            if isinstance(analysis.get("scripts"), list):
                analysis["scripts"] = analysis["scripts"][:15]
            if isinstance(analysis.get("git"), dict) and "status" in analysis["git"]:
                analysis["git"]["status"] = analysis["git"]["status"][:5]

        # Step 4: Last-resort truncation of summary and remaining turns
        if self._serialized_size(ctx) > limit:
            ai_ctx["summary"] = str(ai_ctx.get("summary", ""))[-400:]
            ai_ctx["turns"] = ai_ctx.get("turns", [])[-1:]
        while len(ctx.get("recent_commands", [])) > 1 and self._serialized_size(ctx) > limit:
            ctx["recent_commands"].pop(0)

        return ctx

    def enforce_total_size(self, protected_id: str | None = None) -> list[str]:
        """Evict least-recently-used project context files if total exceeds max_total_bytes."""
        projects_dir = self.paths["projects"]
        entries: list[tuple[int, int, Path]] = []
        total = 0
        for p in projects_dir.glob("*.json"):
            if p.name == "index.json":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            data = read_json_safe(p, {})
            last_acc = int(data.get("last_accessed", st.st_mtime)) if isinstance(data, dict) else int(st.st_mtime)
            entries.append((last_acc, st.st_size, p))

        if total <= self.max_total_bytes:
            return []

        entries.sort(key=lambda item: item[0])
        removed: list[str] = []
        index = self._load_index()
        for _, sz, path in entries:
            if total <= self.max_total_bytes:
                break
            pid = path.stem
            if protected_id and pid == protected_id:
                continue
            try:
                path.unlink()
                total -= sz
                removed.append(pid)
                index.pop(pid, None)
            except OSError:
                continue

        if removed:
            self._save_index(index)
        return removed

    def record_command(
        self,
        directory: str | Path,
        command: str,
        exit_code: int = 0,
        project_only: bool = False,
    ) -> dict[str, Any] | None:
        """Record a shell command executed in directory into that project's context."""
        cmd = command.strip()
        if not cmd or len(cmd) > 1000 or is_sensitive_command(cmd):
            return None

        # Skip recording cue's own maintenance commands to reduce noise
        if cmd.startswith("cue ") or cmd == "cue":
            return None

        root = resolve_project_root(directory)
        if project_only and not is_project_directory(root) and not self.has_project(root):
            return None

        ctx = self.load_project(directory)
        now = int(time.time())
        cwd = str(Path(directory).expanduser().resolve())
        cmds: list[dict[str, Any]] = ctx.setdefault("recent_commands", [])

        # Update existing matching command entry or append
        existing_idx = next((i for i, item in enumerate(cmds) if item.get("cmd") == cmd), None)
        if existing_idx is not None:
            entry = cmds.pop(existing_idx)
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["exit_code"] = int(exit_code)
            entry["cwd"] = cwd
            entry["ts"] = now
            cmds.append(entry)
        else:
            cmds.append(
                {
                    "cmd": cmd,
                    "exit_code": int(exit_code),
                    "cwd": cwd,
                    "ts": now,
                    "count": 1,
                }
            )

        self.save_project(ctx)
        return ctx

    def append_ai_turn(
        self,
        ctx: dict[str, Any],
        user_summary: str,
        assistant_summary: str,
        model_id: str,
    ) -> None:
        """Persist a compact AI conversation turn so context survives model switches."""
        ai_ctx = ctx.setdefault("ai_context", {"summary": "", "turns": [], "active_model": ""})
        ai_ctx["active_model"] = model_id
        now = int(time.time())
        turns: list[dict[str, Any]] = ai_ctx.setdefault("turns", [])
        turns.append({"role": "user", "content": user_summary[:600], "ts": now})
        turns.append({"role": "assistant", "content": assistant_summary[:900], "ts": now})
        if len(turns) > MAX_AI_TURNS:
            ai_ctx["turns"] = turns[-MAX_AI_TURNS:]

    def list_projects(self) -> list[dict[str, Any]]:
        """Return detailed info for all stored project context files inside ~/.cue/projects."""
        now = int(time.time())
        results: list[dict[str, Any]] = []
        for pfile in sorted(self.paths["projects"].glob("*.json")):
            if pfile.name == "index.json":
                continue
            try:
                size_bytes = pfile.stat().st_size
                mtime = int(pfile.stat().st_mtime)
            except OSError:
                continue
            data = read_json_safe(pfile, {})
            if not isinstance(data, dict):
                data = {}
            proj_path = str(data.get("project_path", ""))
            last_acc = int(data.get("last_accessed", mtime))
            age_days = max(0.0, (now - last_acc) / 86400.0)
            results.append(
                {
                    "project_id": pfile.stem,
                    "name": data.get("name", pfile.stem),
                    "project_path": proj_path,
                    "exists": bool(proj_path and Path(proj_path).exists()),
                    "stacks": data.get("analysis", {}).get("stacks", []),
                    "command_count": len(data.get("recent_commands", [])),
                    "size_bytes": size_bytes,
                    "last_accessed": last_acc,
                    "age_days": age_days,
                    "file_path": str(pfile),
                }
            )
        results.sort(key=lambda r: r["last_accessed"], reverse=True)
        return results

    def clear_project(self, target: str) -> list[dict[str, Any]]:
        """Delete project context file(s) matching project_id, exact path, or name."""
        target_str = target.strip()
        resolved_target = ""
        try:
            resolved_target = str(Path(target_str).expanduser().resolve())
        except OSError:
            resolved_target = target_str

        projects = self.list_projects()
        matches = [
            p
            for p in projects
            if p["project_id"] == target_str
            or p["project_path"] == resolved_target
            or p["project_path"] == target_str
            or p["name"] == target_str
        ]
        # Also allow matching resolved project root if user passed a subpath
        if not matches and os.path.exists(resolved_target):
            root_id = make_project_id(resolve_project_root(resolved_target))
            matches = [p for p in projects if p["project_id"] == root_id]

        index = self._load_index()
        deleted: list[dict[str, Any]] = []
        for match in matches:
            pfile = Path(match["file_path"])
            try:
                if pfile.exists():
                    pfile.unlink()
                index.pop(match["project_id"], None)
                deleted.append(match)
            except OSError:
                continue
        if deleted:
            self._save_index(index)
        return deleted

    def clear_legacy(
        self,
        older_than_days: float | None = None,
        missing_only: bool = False,
        all_projects: bool = False,
    ) -> list[dict[str, Any]]:
        """Delete legacy project context files matching age, missing-folder, or all criteria."""
        projects = self.list_projects()
        index = self._load_index()
        deleted: list[dict[str, Any]] = []

        for proj in projects:
            should_delete = False
            if all_projects:
                should_delete = True
            else:
                if missing_only and not proj["exists"]:
                    should_delete = True
                if older_than_days is not None and proj["age_days"] >= older_than_days:
                    should_delete = True

            if should_delete:
                pfile = Path(proj["file_path"])
                try:
                    if pfile.exists():
                        pfile.unlink()
                    index.pop(proj["project_id"], None)
                    deleted.append(proj)
                except OSError:
                    continue

        if deleted:
            self._save_index(index)
        return deleted

    def get_storage_stats(self) -> dict[str, Any]:
        """Compute total size and breakdown of ~/.cue."""
        breakdown: dict[str, int] = {"projects": 0, "logs": 0, "cache": 0, "meta": 0}
        for path in self.home.rglob("*"):
            if not path.is_file():
                continue
            try:
                sz = path.stat().st_size
            except OSError:
                continue
            rel = path.relative_to(self.home)
            top = rel.parts[0] if rel.parts else "meta"
            if top in ("projects", "logs", "cache"):
                breakdown[top] += sz
            else:
                breakdown["meta"] += sz

        projects = self.list_projects()
        return {
            "home": str(self.home),
            "total_bytes": sum(breakdown.values()),
            "breakdown": breakdown,
            "project_count": len(projects),
            "missing_count": sum(1 for p in projects if not p["exists"]),
            "max_project_bytes": self.max_project_bytes,
            "max_total_bytes": self.max_total_bytes,
        }
