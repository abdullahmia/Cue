"""Combines AI suggestions, project history, detected scripts, and global history."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .config import ensure_cue_dirs


def _human_age(ts: int, now: int | None = None) -> str:
    cur = now or int(time.time())
    diff = max(0, cur - ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _match_score(cmd: str, query: str) -> int:
    """Return relevance score boost for matching partial user input (0 if no match)."""
    if not query:
        return 10
    q = query.lower()
    c = cmd.lower()
    if c == q:
        return 0  # Exact same command already typed; prefer continuations/alternatives
    if c.startswith(q):
        return 100
    if q in c:
        return 55
    # Token subsequence match (all space-separated query tokens appear in order)
    tokens = [t for t in q.split() if t]
    if tokens:
        pos = 0
        matched = True
        for tok in tokens:
            idx = c.find(tok, pos)
            if idx == -1:
                matched = False
                break
            pos = idx + len(tok)
        if matched:
            return 35
    return 0


def build_ranked_suggestions(
    project_ctx: dict[str, Any],
    partial_input: str = "",
    global_history: list[str] | None = None,
    limit: int = 25,
    apply_caps: bool = True,
) -> list[dict[str, str]]:
    """Merge and rank suggestions: active fixes/retry, then Recent (max 3), AI (max 12), then Others."""
    now = int(time.time())
    query = partial_input.strip()
    candidates: dict[str, dict[str, Any]] = {}

    def _upsert(cmd: str, source: str, reason: str, base_score: float) -> None:
        cleaned = " ".join(cmd.strip().splitlines()).strip()
        if not cleaned:
            return
        m_score = _match_score(cleaned, query)
        if query and m_score == 0:
            return
        total = base_score + (m_score * 2.0 if query else 0.0)
        existing = candidates.get(cleaned)
        if existing is None or total > existing["score"]:
            candidates[cleaned] = {
                "cmd": cleaned,
                "source": source,
                "reason": reason.replace("\t", " ")[:75],
                "score": total,
            }

    # 0. Active error-fix suggestions (highest priority right after a command fails)
    last_err = project_ctx.get("last_error", {})
    if isinstance(last_err, dict) and last_err.get("fixes"):
        err_ts = int(last_err.get("ts", 0))
        if now - err_ts <= 300:
            for idx, item in enumerate(last_err.get("fixes", [])):
                cmd = str(item.get("cmd", ""))
                reason = str(item.get("reason", "Fix failed command"))
                _upsert(cmd, "fix", reason, 260.0 - idx * 4.0)

    # 0b. Pending retry command (surfaced right after a fix command succeeds)
    pending_retry = project_ctx.get("pending_retry", {})
    if isinstance(pending_retry, dict) and pending_retry.get("cmd"):
        retry_ts = int(pending_retry.get("ts", 0))
        if now - retry_ts <= 300:
            _upsert(
                str(pending_retry["cmd"]),
                "retry",
                "Re-run original command after fix",
                250.0,
            )

    # 1. Project recent commands (strictly prioritized ABOVE AI suggestions)
    recent_cmds = project_ctx.get("recent_commands", [])
    total_recent = len(recent_cmds)
    for idx, entry in enumerate(recent_cmds):
        cmd = str(entry.get("cmd", ""))
        ts = int(entry.get("ts", 0))
        count = int(entry.get("count", 1))
        exit_code = int(entry.get("exit_code", 0))
        recency_rank = (idx + 1) / max(1, total_recent)
        freq_bonus = min(count * 3.0, 20.0)
        status_mark = "✓" if exit_code == 0 else f"✗{exit_code}"
        freq_str = f"{count}× · " if count > 1 else ""
        reason = f"{status_mark} {freq_str}{_human_age(ts, now)}"
        score = 190.0 + recency_rank * 40.0 + freq_bonus
        _upsert(cmd, "proj", reason, score)

    # 2. Cached / fresh AI suggestions (shown after recent project commands)
    ai_suggestions = project_ctx.get("cached_suggestions", [])
    for idx, item in enumerate(ai_suggestions):
        cmd = str(item.get("cmd", ""))
        reason = str(item.get("reason", "AI suggestion"))
        score = 120.0 - idx * 4.0
        _upsert(cmd, "ai", reason, score)

    # 3. Detected project scripts/targets
    scripts = project_ctx.get("analysis", {}).get("scripts", [])
    for idx, item in enumerate(scripts):
        cmd = str(item.get("cmd", ""))
        desc = str(item.get("desc", "Project script"))
        score = 80.0 - idx * 2.0
        _upsert(cmd, "script", desc, score)

    # 4. Global history fallback
    if global_history:
        for idx, cmd in enumerate(global_history[:100]):
            score = 45.0 - min(idx, 40) * 0.5
            _upsert(cmd, "hist", "Global shell history", score)

    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    if not apply_caps:
        return [
            {"cmd": item["cmd"], "source": item["source"], "reason": item["reason"]}
            for item in ranked[:limit]
        ]

    fixes = [i for i in ranked if i["source"] in ("fix", "retry")]
    recents = [i for i in ranked if i["source"] == "proj"][:3]
    ais = [i for i in ranked if i["source"] == "ai"][:12]
    others = [i for i in ranked if i["source"] in ("script", "hist")]
    ordered = fixes + recents + ais + others
    return [
        {"cmd": item["cmd"], "source": item["source"], "reason": item["reason"]}
        for item in ordered[:limit]
    ]


def write_zsh_suggestion_cache(
    project_ctx: dict[str, Any],
    home: Path | None = None,
    limit: int = 60,
) -> Path:
    """Write a fast TSV cache (~/.cue/cache/proj_<id>.tsv) for zero-latency ZLE rendering."""
    paths = ensure_cue_dirs(home)
    pid = project_ctx.get("project_id", "default")
    cache_file = paths["cache"] / f"proj_{pid}.tsv"
    ranked = build_ranked_suggestions(project_ctx, partial_input="", limit=limit, apply_caps=False)

    lines: list[str] = []
    for item in ranked:
        src = item["source"].replace("\t", " ")
        cmd = item["cmd"].replace("\t", " ")
        reason = item["reason"].replace("\t", " ")
        lines.append(f"{src}\t{cmd}\t{reason}")

    tmp = cache_file.with_suffix(".tsv.tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(cache_file)

    # Keep dir_map.tsv updated so shells immediately detect existing ~/.cue context on startup
    proj_path = str(project_ctx.get("project_path", "")).strip()
    if proj_path:
        dir_map = paths["cache"] / "dir_map.tsv"
        stacks = ", ".join(project_ctx.get("analysis", {}).get("stacks", [])[:3])
        existing_lines: list[str] = []
        if dir_map.exists():
            try:
                for line in dir_map.read_text(encoding="utf-8").splitlines():
                    if not line.startswith(proj_path + "\t"):
                        existing_lines.append(line)
            except OSError:
                pass
        existing_lines.append(f"{proj_path}\t{pid}\t{stacks}")
        map_tmp = dir_map.with_suffix(".tsv.tmp")
        map_tmp.write_text("\n".join(existing_lines[-100:]) + "\n", encoding="utf-8")
        map_tmp.replace(dir_map)

    return cache_file
