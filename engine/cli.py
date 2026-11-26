"""CLI and Zsh-bridge entrypoint for cue."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_FREE_MODELS,
    DEFAULT_MAX_PROJECT_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    append_log,
    ensure_cue_dirs,
    get_cue_home,
    load_config,
    load_state,
    resolve_default_cue_home,
    save_config,
    save_state,
)
from .context_manager import ContextManager, make_project_id, resolve_project_root
from .daemon import ensure_daemon_running, get_daemon_meta_path, request_daemon, run_daemon_server
from .error_fixer import diagnose_command_failure
from .openrouter_client import AllModelsExhaustedError, AuthError, OpenRouterClient, is_free_model
from .project_analyzer import analyze_project, compute_fingerprint
from .suggester import build_ranked_suggestions, write_zsh_suggestion_cache
from .tui import (
    Theme,
    format_age,
    format_bytes,
    print_banner,
    prompt_api_key,
    prompt_yes_no,
    render_history_table,
    render_projects_table,
    tui_confirm_card,
    tui_input_card,
    tui_menu_loop,
    tui_multi_select_projects,
)


def cmd_onboard(args: argparse.Namespace) -> int:
    """Interactive first-session onboarding flow."""
    theme = Theme()
    cfg = load_config()
    ensure_cue_dirs()
    print_banner(theme, "First-session setup wizard")

    existing_key = cfg.get("openrouter_api_key", "")
    if existing_key and not args.force:
        print(f"\n  {theme.green('✓')} OpenRouter API key is already configured.")
        if not prompt_yes_no(theme, "Reconfigure OpenRouter API key?", default=False):
            cfg["onboarded"] = True
            cfg["onboarded_at"] = int(time.time())
            save_config(cfg)
            return 0

    api_key = prompt_api_key(theme)
    client = OpenRouterClient(config=cfg)

    if api_key:
        print(f"\n  {theme.cyan('◌')} Verifying OpenRouter key & discovering :free models...")
        ok, msg, models = client.validate_key(api_key)
        if ok:
            print(f"  {theme.green('✓')} {msg}")
            cfg["openrouter_api_key"] = api_key
            # Pick best available default free model
            available = {m["id"] for m in models}
            chosen = next((m for m in DEFAULT_FREE_MODELS if m in available), models[0]["id"])
            cfg["preferred_model"] = chosen
            state = load_state()
            state["active_model"] = chosen
            save_state(state)
            print(f"  {theme.green('✓')} Active free model: {theme.bold(theme.cyan(chosen))}")
        else:
            print(f"  {theme.red('✗')} {msg}")
            if prompt_yes_no(theme, "Save key anyway (or retry later with `cue onboard`)?", default=False):
                cfg["openrouter_api_key"] = api_key
    else:
        print(f"\n  {theme.yellow('!')} Skipped API key. Local history, scripts & error-fix autocomplete enabled.")
        print(theme.dim("    Run `cue token` anytime to add or change your OpenRouter API key."))

    # Always auto-analyze in the background without asking permission
    cfg["auto_analyze"] = True
    cfg["free_models_only"] = True
    cfg["onboarded"] = True
    cfg["onboarded_at"] = int(time.time())
    save_config(cfg)

    # Check if current folder already exists in ~/.cue/projects or initialize it
    cwd = os.getcwd()
    cm = ContextManager(config=cfg)
    root = resolve_project_root(cwd)
    pfile = cm.project_file(make_project_id(root))
    existed = pfile.exists()
    ctx = cm.load_project(cwd)
    if not existed or not ctx.get("analysis"):
        ctx["analysis"] = analyze_project(cwd)
        ctx["fingerprint"] = ctx["analysis"].get("fingerprint", "")
        ctx["last_analyzed"] = int(time.time())
    if cfg.get("openrouter_api_key") and not ctx.get("cached_suggestions"):
        try:
            print(f"  {theme.cyan('◌')} Preparing context for {Path(cwd).name}...")
            res = OpenRouterClient(config=cfg).generate_suggestions(ctx, cwd=cwd, mode="analyze", timeout=8.0)
            ctx["cached_suggestions"] = res["suggestions"]
            cm.append_ai_turn(ctx, f"Opened {cwd}", res.get("summary", ""), res["model"])
            print(f"  {theme.green('✓')} Ready with {len(res['suggestions'])} AI suggestions!")
        except (AuthError, AllModelsExhaustedError) as exc:
            print(f"  {theme.yellow('!')} AI warmup skipped: {exc}")
    cm.save_project(ctx)
    write_zsh_suggestion_cache(ctx)
    ensure_daemon_running()

    print("")
    print(theme.green("  ✓ Setup complete!"))
    print(theme.dim("  Tips: Press ↓/↑ for dropdown · Tab/→ to fill · Enter to run · Ctrl+Space for AI refresh"))
    print(theme.dim("        Change token anytime with `cue token` · Manage contexts with `cue context list`.\n"))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Find existing project context in ~/.cue (or create one), analyze in background, and update cache."""
    directory = Path(args.dir or os.getcwd()).expanduser().resolve()
    if not directory.exists():
        return 1

    ensure_daemon_running()
    if not args.force:
        daemon_res = request_daemon(
            "/analyze",
            {"dir": str(directory), "force": bool(args.force), "no_ai": bool(args.no_ai)},
            timeout=9.0,
        )
        if isinstance(daemon_res, dict) and "project_id" in daemon_res:
            if args.json:
                print(json.dumps(daemon_res, ensure_ascii=False))
            elif not args.quiet:
                theme = Theme()
                stacks = ", ".join(daemon_res.get("analysis", {}).get("stacks", []))
                print(f"{theme.green('✓')} Ready {theme.bold(directory.name)} ({theme.cyan(stacks)})")
            return 0

    cfg = load_config()
    cm = ContextManager(config=cfg)
    root = resolve_project_root(directory)
    pfile = cm.project_file(make_project_id(root))
    already_exists = pfile.exists()
    ctx = cm.load_project(directory)
    fp = compute_fingerprint(root)
    now = int(time.time())

    # If existing context exists in ~/.cue, continue using it unless forced or fingerprint/TTL stale
    if not already_exists or args.force or fp != ctx.get("fingerprint") or not ctx.get("analysis"):
        ctx["analysis"] = analyze_project(root)
        ctx["fingerprint"] = fp
        ctx["last_analyzed"] = now

    needs_ai = (
        bool(args.force)
        or not ctx.get("cached_suggestions")
        or (fp != ctx.get("fingerprint"))
        or (now - int(ctx.get("last_analyzed", 0)) > 1800)
    )

    if needs_ai and not args.no_ai and cfg.get("openrouter_api_key"):
        try:
            client = OpenRouterClient(config=cfg)
            res = client.generate_suggestions(ctx, cwd=str(directory), mode="analyze", timeout=8.5)
            ctx["cached_suggestions"] = res["suggestions"]
            if res.get("summary"):
                ctx.setdefault("ai_context", {})["summary"] = res["summary"]
            cm.append_ai_turn(ctx, f"Analyzed {root.name}", res.get("summary", ""), res["model"])
        except (AuthError, AllModelsExhaustedError) as exc:
            append_log(f"Analyze AI skipped for {root}: {exc}", "WARN")

    cm.save_project(ctx)
    cache_path = write_zsh_suggestion_cache(ctx)

    if args.json:
        print(
            json.dumps(
                {
                    "project_id": ctx["project_id"],
                    "existed": already_exists,
                    "cache_file": str(cache_path),
                    "analysis": ctx.get("analysis", {}),
                    "suggestions": build_ranked_suggestions(ctx),
                },
                ensure_ascii=False,
            )
        )
    elif not args.quiet:
        theme = Theme()
        stacks = ", ".join(ctx.get("analysis", {}).get("stacks", []))
        label = "Resumed" if already_exists else "Analyzed"
        print(f"{theme.green('✓')} {label} {theme.bold(ctx['name'])} ({theme.cyan(stacks)})")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Record an executed shell command and diagnose fixes if it exited with an error."""
    directory = Path(args.dir or os.getcwd()).expanduser().resolve()
    daemon_res = request_daemon(
        "/record",
        {"dir": str(directory), "command": args.command, "exit_code": args.exit_code, "ai_fix": not getattr(args, "no_ai", False)},
        timeout=8.5,
    )
    if isinstance(daemon_res, dict) and daemon_res.get("recorded") is not None:
        if getattr(args, "json", False):
            print(json.dumps(daemon_res, ensure_ascii=False))
        return 0

    cfg = load_config()
    cm = ContextManager(config=cfg)
    ctx = cm.record_command(directory, args.command, exit_code=args.exit_code)
    if ctx is not None:
        cmd_clean = args.command.strip()
        if int(args.exit_code) not in (0, 130):
            diag = diagnose_command_failure(cmd_clean, args.exit_code, directory, ctx.get("analysis"))
            ctx["pending_retry"] = {}
            if diag["fixes"]:
                ctx["last_error"] = {
                    "cmd": cmd_clean,
                    "exit_code": int(args.exit_code),
                    "diagnosis": diag["diagnosis"],
                    "fixes": diag["fixes"],
                    "ts": int(time.time()),
                }
                if cfg.get("openrouter_api_key") and not getattr(args, "no_ai", False):
                    try:
                        res = OpenRouterClient(config=cfg).generate_suggestions(
                            ctx,
                            partial_input=cmd_clean,
                            cwd=str(directory),
                            mode="fix",
                            error_context=diag["diagnosis"],
                            timeout=7.5,
                        )
                        seen = {f["cmd"] for f in diag["fixes"]}
                        merged = list(diag["fixes"])
                        for s in res["suggestions"]:
                            if s["cmd"] not in seen:
                                merged.append({"cmd": s["cmd"], "reason": s["reason"]})
                        ctx["last_error"]["fixes"] = merged[:8]
                    except (AuthError, AllModelsExhaustedError):
                        pass
            else:
                ctx["last_error"] = {"cmd": cmd_clean, "exit_code": int(args.exit_code), "fixes": [], "ts": int(time.time())}
        else:
            prev_err = ctx.get("last_error", {})
            if isinstance(prev_err, dict) and prev_err.get("cmd") and prev_err.get("cmd") != cmd_clean:
                ctx["pending_retry"] = {"cmd": prev_err["cmd"], "ts": int(time.time())}
            else:
                ctx["pending_retry"] = {}
            ctx["last_error"] = {}
        cm.save_project(ctx)
        write_zsh_suggestion_cache(ctx)
        if getattr(args, "json", False):
            print(json.dumps({"recorded": True, "last_error": ctx.get("last_error", {}), "pending_retry": ctx.get("pending_retry", {})}, ensure_ascii=False))
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    """Return ranked suggestions (optionally refreshing AI predictions for partial input or prompt mode)."""
    directory = Path(args.dir or os.getcwd()).expanduser().resolve()
    req_mode = getattr(args, "mode", "auto")
    if req_mode == "auto":
        req_mode = "complete" if args.input else "analyze"

    if args.live:
        daemon_res = request_daemon(
            "/suggest",
            {"dir": str(directory), "input": args.input or "", "limit": args.limit, "live": True, "mode": req_mode},
            timeout=8.5,
        )
        if isinstance(daemon_res, dict) and "suggestions" in daemon_res:
            if args.tsv:
                if req_mode == "prompt":
                    ans = str(daemon_res.get("answer", "")).replace("\t", " ").replace("\n", " ").strip()
                    err = str(daemon_res.get("error", "")).replace("\t", " ").replace("\n", " ").strip()
                    if ans:
                        print(f"answer\t{ans}\t{daemon_res.get('model', '')}")
                    elif err:
                        print(f"error\t{err}\t")
                for item in daemon_res["suggestions"]:
                    print(f"{item['source']}\t{item['cmd']}\t{item['reason']}")
                return 0
            if args.json:
                print(json.dumps(daemon_res, ensure_ascii=False))
                return 0
            if req_mode == "prompt":
                theme = Theme()
                ans = str(daemon_res.get("answer", "")).strip()
                err = str(daemon_res.get("error", "")).strip()
                print("")
                if ans:
                    print(f"  {theme.cyan('✦')} {theme.bold(ans)}\n")
                elif err:
                    print(f"  {theme.yellow('⚠')} {theme.dim(err)}\n")
                for item in daemon_res["suggestions"]:
                    badge = {"ai": "✦ AI", "proj": "↺ Recent", "script": "⚙ Script", "hist": "⌘ Hist"}.get(item["source"], item["source"])
                    print(f"  {theme.cyan(f'{badge:<9}')} {theme.bold(item['cmd']):<36} {theme.dim(item['reason'])}")
                print("")
                return 0

    cfg = load_config()
    cm = ContextManager(config=cfg)
    ctx = cm.load_project(directory)

    if not ctx.get("analysis"):
        ctx["analysis"] = analyze_project(resolve_project_root(directory))
        ctx["fingerprint"] = ctx["analysis"].get("fingerprint", "")

    prompt_suggestions: list[dict[str, str]] = []
    prompt_answer = ""
    prompt_model = ""
    prompt_error = ""
    if args.live and not cfg.get("openrouter_api_key") and req_mode == "prompt":
        prompt_error = "OpenRouter API key not configured (run: cue token)"
    elif args.live and cfg.get("openrouter_api_key"):
        try:
            client = OpenRouterClient(config=cfg)
            res = client.generate_suggestions(
                ctx,
                partial_input=args.input or "",
                cwd=str(directory),
                mode=req_mode,
                timeout=8.0,
            )
            prompt_suggestions = res["suggestions"]
            prompt_answer = str(res.get("summary", "")).strip()
            prompt_model = str(res.get("model", "")).strip()
            seen = {s["cmd"] for s in res["suggestions"]}
            merged = list(res["suggestions"])
            for old in ctx.get("cached_suggestions", []):
                if old.get("cmd") not in seen:
                    merged.append(old)
            ctx["cached_suggestions"] = merged[:16]
            cm.append_ai_turn(
                ctx,
                f"{req_mode}: {args.input or '<empty>'}",
                f"Suggested: {', '.join(s['cmd'] for s in res['suggestions'][:3])}",
                res["model"],
            )
            cm.save_project(ctx)
        except (AuthError, AllModelsExhaustedError) as exc:
            prompt_error = str(exc)
            append_log(f"Live suggest error: {exc}", "WARN")

    cache_path = write_zsh_suggestion_cache(ctx)
    if req_mode == "prompt" and prompt_suggestions:
        ranked = [{"cmd": s["cmd"], "source": "ai", "reason": s["reason"]} for s in prompt_suggestions[: args.limit]]
    else:
        ranked = build_ranked_suggestions(ctx, partial_input=args.input or "", limit=args.limit)

    if args.tsv:
        if req_mode == "prompt":
            if prompt_answer:
                clean_ans = prompt_answer.replace("\t", " ").replace("\n", " ").strip()
                print(f"answer\t{clean_ans}\t{prompt_model}")
            elif prompt_error:
                clean_err = prompt_error.replace("\t", " ").replace("\n", " ").strip()
                print(f"error\t{clean_err}\t")
        for item in ranked:
            print(f"{item['source']}\t{item['cmd']}\t{item['reason']}")
    elif args.json:
        out_obj: dict[str, Any] = {"cache_file": str(cache_path), "suggestions": ranked}
        if req_mode == "prompt":
            if prompt_answer:
                out_obj["answer"] = prompt_answer
            if prompt_model:
                out_obj["model"] = prompt_model
            if prompt_error:
                out_obj["error"] = prompt_error
        print(json.dumps(out_obj, ensure_ascii=False))
    else:
        theme = Theme()
        if req_mode == "prompt":
            print("")
            if prompt_answer:
                print(f"  {theme.cyan('✦')} {theme.bold(prompt_answer)}\n")
            elif prompt_error:
                print(f"  {theme.yellow('⚠')} {theme.dim(prompt_error)}\n")
        for item in ranked:
            badge = {
                "fix": "✦ Fix",
                "retry": "↻ Retry",
                "ai": "✦ AI",
                "proj": "↺ Recent",
                "script": "⚙ Script",
                "hist": "⌘ Hist",
            }.get(item["source"], item["source"])
            print(f"  {theme.cyan(f'{badge:<9}')} {theme.bold(item['cmd']):<36} {theme.dim(item['reason'])}")
        if req_mode == "prompt":
            print("")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Manage ~/.cue project context files and legacy cleanup."""
    theme = Theme()
    cfg = load_config()
    cm = ContextManager(config=cfg)

    sub = args.context_action
    if sub == "list":
        projects = cm.list_projects()
        render_projects_table(theme, projects)
        stats = cm.get_storage_stats()
        print(
            theme.dim(
                f"  Storage: {format_bytes(stats['total_bytes'])} total "
                f"({stats['project_count']} projects, {stats['missing_count']} missing folders)\n"
            )
        )
        return 0

    if sub == "stats":
        stats = cm.get_storage_stats()
        print_banner(theme, f"Context storage in {stats['home']}")
        print(f"  Total disk usage:    {theme.bold(format_bytes(stats['total_bytes']))}")
        print(f"  Projects stored:     {stats['project_count']} ({stats['missing_count']} missing/legacy)")
        print(f"  Projects folder:     {format_bytes(stats['breakdown']['projects'])} / {format_bytes(stats['max_total_bytes'])}")
        print(f"  Per-project cap:     {format_bytes(stats['max_project_bytes'])}")
        print(f"  Logs & cache:        {format_bytes(stats['breakdown']['logs'] + stats['breakdown']['cache'])}\n")
        return 0

    if sub == "clear":
        if args.current:
            deleted = cm.clear_project(os.getcwd())
            _print_deleted(theme, deleted)
            return 0
        if args.target:
            deleted = cm.clear_project(args.target)
            _print_deleted(theme, deleted)
            return 0
        if args.all:
            if not args.yes and not prompt_yes_no(theme, "Delete ALL stored project context files in ~/.cue?", default=False):
                print(theme.dim("  Cancelled."))
                return 0
            deleted = cm.clear_legacy(all_projects=True)
            _print_deleted(theme, deleted)
            return 0

        older_days = _parse_duration_days(args.older_than) if args.older_than else None
        if args.missing or older_days is not None:
            deleted = cm.clear_legacy(older_than_days=older_days, missing_only=bool(args.missing))
            _print_deleted(theme, deleted)
            return 0

        # Interactive modern multi-select TUI for legacy / specific project cleanup
        projects = cm.list_projects()
        if not projects:
            print(theme.dim("  No project context files found."))
            return 0
        if sys.stdin.isatty() and sys.stdout.isatty():
            entries = [{**p, "src_root": cm.home.name} for p in projects]
            indices = tui_multi_select_projects(theme, "Clear Stored Projects", entries)
            deleted_items = []
            for idx in indices:
                if 0 <= idx < len(projects):
                    deleted_items.extend(cm.clear_project(projects[idx]["project_id"]))
            _print_deleted(theme, deleted_items)
            return 0
        render_projects_table(theme, projects)
        return 0

    return 1


def _parse_duration_days(spec: str) -> float:
    s = spec.strip().lower()
    if s.endswith("d"):
        return float(s[:-1])
    if s.endswith("w"):
        return float(s[:-1]) * 7.0
    if s.endswith("h"):
        return float(s[:-1]) / 24.0
    return float(s)


def _print_deleted(theme: Theme, deleted: list[dict[str, Any]]) -> None:
    if not deleted:
        print(theme.dim("  No matching project context files were removed."))
        return
    freed = sum(int(d.get("size_bytes", 0)) for d in deleted)
    print(f"  {theme.green('✓')} Removed {len(deleted)} context file(s) from ~/.cue/projects ({format_bytes(freed)} freed):")
    for item in deleted:
        print(f"    {theme.dim('•')} {item.get('name')} {theme.dim(str(item.get('project_path', '')))}")


def cmd_models(args: argparse.Namespace) -> int:
    """List OpenRouter free models and current health/cooldown status."""
    theme = Theme()
    cfg = load_config()
    client = OpenRouterClient(config=cfg)
    if args.reset_health:
        client.clear_model_health()
        print(f"  {theme.green('✓')} Cleared all model cooldowns.")
    if args.refresh:
        client.fetch_free_models(force_refresh=True)
    if args.use:
        chosen = args.use.strip()
        if not is_free_model(chosen):
            print(f"  {theme.red('✗')} '{chosen}' is not an OpenRouter :free model. Paid models are disallowed.")
            return 1
        cfg["preferred_model"] = chosen
        save_config(cfg)
        state = load_state()
        state["active_model"] = chosen
        save_state(state)
        print(f"  {theme.green('✓')} Preferred free model set to {theme.cyan(chosen)}.")
        return 0

    rows = client.get_models_status()
    print_banner(theme, "OpenRouter :free models & fallback order")
    for row in rows[:20]:
        marker = theme.green("●") if row["active"] else theme.slate("○")
        status = (
            theme.green("ready")
            if row["status"] == "ready"
            else theme.yellow(f"cooldown {row['cooldown_seconds'] // 60}m ({row['reason']})")
        )
        print(f"  {marker} {theme.bold(row['id']):<48} {status}")
    print("")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    """Display cue plugin status, active free model, and ~/.cue storage."""
    theme = Theme()
    cfg = load_config()
    state = load_state()
    cm = ContextManager(config=cfg)
    stats = cm.get_storage_stats()
    ctx = cm.load_project(os.getcwd())

    print_banner(theme, f"v{__version__} · Status")
    key_ok = bool(cfg.get("openrouter_api_key"))
    print(f"  API Key:         {theme.green('configured') if key_ok else theme.yellow('not set (`cue onboard`)')}")
    prompt_hint_cli = theme.cyan('cue ask "<prompt>"') + " " + theme.dim('/ cue prompt "<prompt>"')
    print(f"  Prompt CLI:      {prompt_hint_cli}")
    print(f"  Free-only mode:  {theme.green('enforced (:free models only)')}")
    print(f"  Active model:    {theme.cyan(state.get('active_model', DEFAULT_FREE_MODELS[0]))}")
    if state.get("total_fallbacks"):
        print(f"  Auto-switches:   {state['total_fallbacks']} (last: {state.get('last_switch_reason', '')})")
    print(f"  Current project: {theme.bold(ctx['name'])} ({', '.join(ctx.get('analysis', {}).get('stacks', ['not analyzed']))})")
    print(f"  Storage ({stats['home']}): {format_bytes(stats['total_bytes'])} across {stats['project_count']} projects\n")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """Set or change the OpenRouter API token anytime."""
    theme = Theme()
    cfg = load_config()
    if cfg.get("openrouter_api_key") and not args.key:
        k = cfg["openrouter_api_key"]
        masked = f"{k[:8]}...{k[-4:]}" if len(k) > 12 else "***"
        print(f"  Current token: {theme.dim(masked)}")

    key = (args.key or "").strip() or prompt_api_key(theme)
    if not key:
        print(theme.yellow("  No token provided; existing configuration kept."))
        return 1

    print(f"  {theme.cyan('◌')} Verifying new OpenRouter token...")
    client = OpenRouterClient(config=cfg)
    ok, msg, models = client.validate_key(key)
    if ok:
        print(f"  {theme.green('✓')} {msg}")
        cfg["openrouter_api_key"] = key
        cfg["onboarded"] = True
        save_config(cfg)
        client.clear_model_health()
        available = {m["id"] for m in models}
        if cfg.get("preferred_model") not in available and models:
            chosen = next((m for m in DEFAULT_FREE_MODELS if m in available), models[0]["id"])
            cfg["preferred_model"] = chosen
            save_config(cfg)
            state = load_state()
            state["active_model"] = chosen
            save_state(state)
        request_daemon("/reload-config", {}, timeout=1.5)
        return 0

    print(f"  {theme.red('✗')} {msg}")
    if prompt_yes_no(theme, "Save this token anyway?", default=False):
        cfg["openrouter_api_key"] = key
        cfg["onboarded"] = True
        save_config(cfg)
        request_daemon("/reload-config", {}, timeout=1.5)
        return 0
    return 1


def cmd_fix(args: argparse.Namespace) -> int:
    """Diagnose a failed command and show instant + AI fix suggestions."""
    directory = Path(args.dir or os.getcwd()).expanduser().resolve()
    cfg = load_config()
    cm = ContextManager(config=cfg)
    ctx = cm.load_project(directory)
    if not ctx.get("analysis"):
        ctx["analysis"] = analyze_project(resolve_project_root(directory))

    failed_cmd = args.failed_cmd or ctx.get("last_error", {}).get("cmd", "")
    exit_code = args.exit_code if args.exit_code is not None else int(ctx.get("last_error", {}).get("exit_code", 1))
    if not failed_cmd:
        if not args.json:
            print(Theme().dim("  No failed command specified or recorded recently."))
        return 0

    diag = diagnose_command_failure(failed_cmd, exit_code, directory, ctx.get("analysis"))
    fixes = list(diag["fixes"])
    if not args.no_ai and cfg.get("openrouter_api_key"):
        try:
            res = OpenRouterClient(config=cfg).generate_suggestions(
                ctx,
                partial_input=failed_cmd,
                cwd=str(directory),
                mode="fix",
                error_context=diag["diagnosis"],
                timeout=7.5,
            )
            seen = {f["cmd"] for f in fixes}
            for s in res["suggestions"]:
                if s["cmd"] not in seen:
                    fixes.append({"cmd": s["cmd"], "reason": s["reason"]})
        except (AuthError, AllModelsExhaustedError):
            pass

    if fixes:
        ctx["last_error"] = {
            "cmd": failed_cmd,
            "exit_code": exit_code,
            "diagnosis": diag["diagnosis"],
            "fixes": fixes[:8],
            "ts": int(time.time()),
        }
        cm.save_project(ctx)
        write_zsh_suggestion_cache(ctx)

    if args.json:
        print(json.dumps({"diagnosis": diag["diagnosis"], "fixes": fixes[:8]}, ensure_ascii=False))
    else:
        theme = Theme()
        print(f"\n  {theme.yellow('⚠')} {theme.bold(diag['diagnosis'])}")
        for item in fixes[:8]:
            print(f"    {theme.red('✦ FIX')}  {theme.bold(item['cmd']):<38} {theme.dim(item['reason'])}")
        print("")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Manage the low-latency background keep-alive daemon."""
    if args.serve:
        return run_daemon_server()
    if args.ensure:
        ok = ensure_daemon_running()
        return 0 if ok else 1
    meta_path = get_daemon_meta_path()
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    theme = Theme()
    if args.stop:
        pid = int(meta.get("pid", 0) or 0)
        if pid:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        if meta_path.exists():
            meta_path.unlink(missing_ok=True)
        print(f"  {theme.green('✓')} Stopped cue background daemon.")
        return 0
    if meta.get("port"):
        print(f"  {theme.green('●')} Daemon running on 127.0.0.1:{meta['port']} (pid {meta.get('pid')})")
    else:
        print(f"  {theme.slate('○')} Daemon not currently running (auto-starts on shell activity).")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Display modern TUI table of recent commands in the current project."""
    directory = Path(args.dir or os.getcwd()).expanduser().resolve()
    cfg = load_config()
    cm = ContextManager(config=cfg)
    ctx = cm.load_project(directory)
    render_history_table(Theme(), ctx.get("name", directory.name), ctx.get("recent_commands", []), limit=args.limit)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Translate a free-text prompt into runnable shell commands using OpenRouter."""
    prompt_text = " ".join(args.prompt_words).strip()
    if not prompt_text:
        print(Theme().dim("  Usage: cue ask \"<what you want to do>\""))
        return 1
    args.input = prompt_text
    args.live = True
    args.mode = "prompt"
    args.limit = args.limit or 12
    return cmd_suggest(args)


def _settings_choose_model(theme: Theme, cfg: dict[str, Any]) -> str:
    client = OpenRouterClient(config=cfg)
    rows = client.get_models_status()[:25]
    if not rows:
        return "No :free models discovered."

    chosen_msg = [""]

    def _items() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for r in rows:
            ctx_k = f"{int(r.get('context_length', 32768)) // 1000}k ctx"
            st = "● active" if r["active"] else ("ready" if r["status"] == "ready" else "cooldown")
            tone = "green" if r["active"] else ("cyan" if r["status"] == "ready" else "yellow")
            out.append({"label": r["id"], "value": st, "hint": ctx_k, "tone": tone, "id": r["id"]})
        return out

    def _on_select(item: dict[str, Any], _idx: int) -> str | None:
        selected = str(item["id"])
        if not is_free_model(selected):
            return "Paid models are disallowed."
        cfg["preferred_model"] = selected
        save_config(cfg)
        state = load_state()
        state["active_model"] = selected
        save_state(state)
        request_daemon("/reload-config", {}, timeout=1.5)
        chosen_msg[0] = f"Preferred model set to {selected}"
        return "__EXIT__"

    tui_menu_loop(
        theme,
        "Preferred :free Model",
        f"{len(rows)} free models",
        _items,
        _on_select,
        footer="↑/↓ select model · Enter confirm · Esc back",
        width=90,
    )
    return chosen_msg[0]


def _settings_manage_projects(theme: Theme, cfg: dict[str, Any]) -> str:
    cm = ContextManager(config=cfg)
    active_root = cm.home
    legacy_tawh = Path.home() / ".tawh"
    has_legacy_tawh = (
        legacy_tawh.is_dir()
        and (legacy_tawh / "projects").is_dir()
        and legacy_tawh.resolve() != active_root.resolve()
    )

    cm_legacy = ContextManager(home=legacy_tawh, config=cfg) if has_legacy_tawh else None
    combined: list[tuple[ContextManager, dict[str, Any], str]] = []
    for p in cm.list_projects():
        combined.append((cm, p, active_root.name))
    if cm_legacy:
        for p in cm_legacy.list_projects():
            combined.append((cm_legacy, p, ".tawh"))

    if not combined:
        return f"No stored projects found in {active_root.name}/projects."

    entries = [{**p, "src_root": src_root} for (_mgr, p, src_root) in combined]
    selected_indices = tui_multi_select_projects(theme, "Manage Stored Projects", entries)
    if not selected_indices:
        return ""

    sel_bytes = sum(int(entries[i].get("size_bytes", 0)) for i in selected_indices if 0 <= i < len(entries))
    if not tui_confirm_card(
        theme,
        "Delete Project Contexts",
        f"Delete {len(selected_indices)} project(s) ({format_bytes(sel_bytes)})?",
        confirm_label=f"Delete {len(selected_indices)} project(s)",
        cancel_label="Cancel",
    ):
        return "Deletion cancelled"

    removed: list[dict[str, Any]] = []
    for i in selected_indices:
        if 0 <= i < len(combined):
            mgr, proj, _ = combined[i]
            removed.extend(mgr.clear_project(proj["project_id"]))

    if not removed:
        return ""
    freed = sum(int(d.get("size_bytes", 0)) for d in removed)
    return f"Removed {len(removed)} project(s) ({format_bytes(freed)} freed)"


def cmd_settings(args: argparse.Namespace) -> int:
    """Modern interactive arrow-key settings TUI."""
    theme = Theme()

    if getattr(args, "prompt_mode", ""):
        cfg = load_config()
        val = args.prompt_mode.strip().lower()
        enabled = val in ("on", "true", "1", "enable", "enabled", "yes")
        cfg["prompt_mode"] = enabled
        save_config(cfg)
        request_daemon("/reload-config", {}, timeout=1.5)
        lbl = theme.green("Enabled") if enabled else theme.yellow("Disabled")
        print(f"  {theme.green('✓')} Prompt Mode: {lbl}")
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print_banner(theme, "Settings (interactive TTY required)")
        return 0

    uninstalled = [False]

    def _build_items() -> list[dict[str, str]]:
        cfg = load_config()
        cm = ContextManager(config=cfg)
        stats = cm.get_storage_stats()
        key = str(cfg.get("openrouter_api_key", ""))
        key_badge = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ("configured" if key else "not set")
        model_badge = str(cfg.get("preferred_model", DEFAULT_FREE_MODELS[0]))

        return [
            {
                "id": "model",
                "label": "Preferred Model",
                "value": model_badge,
                "hint": "Select OpenRouter :free model",
                "tone": "cyan",
            },
            {
                "id": "token",
                "label": "OpenRouter Token",
                "value": key_badge,
                "hint": "Add or rotate API key",
                "tone": "green" if key else "yellow",
            },
            {
                "id": "projects",
                "label": "Manage Projects",
                "value": f"{stats['project_count']} stored ({format_bytes(stats['breakdown']['projects'])})",
                "hint": "Remove stored / .tawh projects",
                "tone": "cyan",
            },
            {
                "id": "cleanup",
                "label": "Clear Legacy/Old",
                "value": f"{stats['missing_count']} missing",
                "hint": f"Prune >{cfg.get('legacy_retention_days', 30)}d & missing",
                "tone": "yellow" if stats["missing_count"] else "slate",
            },
            {
                "id": "uninstall",
                "label": "Uninstall cue",
                "value": "Remove plugin",
                "hint": "Clean shell hooks & data",
                "tone": "red",
            },
        ]

    def _on_select(item: dict[str, Any], _idx: int) -> str | None:
        action = item["id"]
        cfg = load_config()
        if action == "prompt_mode":
            cfg["prompt_mode"] = not bool(cfg.get("prompt_mode", False))
            save_config(cfg)
            request_daemon("/reload-config", {}, timeout=1.5)
            return f"Prompt Mode {'Enabled' if cfg['prompt_mode'] else 'Disabled'}"
        if action == "model":
            return _settings_choose_model(theme, cfg)
        if action == "token":
            cur_key = str(cfg.get("openrouter_api_key", ""))
            sub_hint = "Current key set — enter new key below" if cur_key else "Get free key at https://openrouter.ai/keys"
            new_key = tui_input_card(
                theme,
                "OpenRouter API Token",
                sub_hint,
                "Paste your OpenRouter API key:",
                masked=True,
            )
            if not new_key:
                return ""
            client = OpenRouterClient(config=cfg)
            ok, msg, models = client.validate_key(new_key)
            cfg["openrouter_api_key"] = new_key
            cfg["onboarded"] = True
            save_config(cfg)
            if ok:
                client.clear_model_health()
                available = {m["id"] for m in models}
                if cfg.get("preferred_model") not in available and models:
                    chosen = next((m for m in DEFAULT_FREE_MODELS if m in available), models[0]["id"])
                    cfg["preferred_model"] = chosen
                    save_config(cfg)
                    st = load_state()
                    st["active_model"] = chosen
                    save_state(st)
                request_daemon("/reload-config", {}, timeout=1.5)
                return f"Token verified ({len(models)} free models)"
            request_daemon("/reload-config", {}, timeout=1.5)
            return f"Saved token (warning: {msg[:45]})"
        if action == "projects":
            return _settings_manage_projects(theme, cfg)
        if action == "cleanup":
            cm = ContextManager(config=cfg)
            retention = float(cfg.get("legacy_retention_days", 30))
            candidates = [
                p for p in cm.list_projects()
                if (not p["exists"]) or p["age_days"] >= retention
            ]
            if not candidates:
                return "No legacy projects to clean"
            if not tui_confirm_card(
                theme,
                "Clear Legacy Projects",
                f"Remove {len(candidates)} missing/old project(s)?",
                confirm_label=f"Clear {len(candidates)} project(s)",
                cancel_label="Cancel",
            ):
                return "Cleanup cancelled"
            deleted = cm.clear_legacy(
                older_than_days=retention,
                missing_only=True,
            )
            return f"Cleaned {len(deleted)} legacy project(s)" if deleted else "No legacy projects to clean"
        if action == "uninstall":
            choice_res = [""]

            def _u_items() -> list[dict[str, str]]:
                return [
                    {"id": "keep", "label": "Uninstall hooks only", "value": "Keep data", "hint": "Remove from shell profiles", "tone": "yellow"},
                    {"id": "purge", "label": "Uninstall & purge data", "value": "Delete all", "hint": "Also delete ~/.cue / ~/.cuee", "tone": "red"},
                    {"id": "cancel", "label": "Cancel", "value": "", "hint": "Return to Settings", "tone": "slate"},
                ]

            def _u_sel(u_item: dict[str, Any], _i: int) -> str | None:
                uid = u_item["id"]
                if uid == "cancel":
                    return "__EXIT__"
                uninstall_cue_plugin(purge_data=(uid == "purge"))
                choice_res[0] = uid
                return "__EXIT__"

            tui_menu_loop(
                theme,
                "Uninstall cue",
                "Confirm action",
                _u_items,
                _u_sel,
                footer="↑/↓ choose · Enter confirm · Esc/← back",
                width=82,
            )
            if choice_res[0] in ("keep", "purge"):
                uninstalled[0] = True
                return "__EXIT__"
            return ""
        return None

    stats0 = ContextManager().get_storage_stats()
    tui_menu_loop(
        theme,
        "Settings",
        f"v{__version__} · {stats0['home']}",
        _build_items,
        _on_select,
        footer="↑/↓ navigate · Enter/Space toggle or open · Esc/q exit",
        width=90,
    )
    return 0


def _strip_lines_matching(file_path: Path, substrings: tuple[str, ...]) -> bool:
    if not file_path.is_file():
        return False
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return False
    filtered = [ln for ln in lines if not any(sub in ln for sub in substrings)]
    if len(filtered) == len(lines):
        return False
    try:
        file_path.write_text("".join(filtered), encoding="utf-8")
        return True
    except OSError:
        return False


def uninstall_cue_plugin(
    purge_data: bool = False,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Remove cue shell hooks, symlinks, daemon, and optionally ~/.cue/.cuee/.tawh data."""
    base_home = home_dir or Path.home()
    removed_rc: list[str] = []

    # 1. Stop running daemon if active (without creating directories during uninstall)
    cue_root = Path(os.environ.get("CUE_HOME", "")).expanduser() if os.environ.get("CUE_HOME", "").strip() else resolve_default_cue_home(base_home)
    for meta_path in (
        cue_root / "cache" / "daemon.json",
        base_home / ".cue" / "cache" / "daemon.json",
        base_home / ".cuee" / "cache" / "daemon.json",
    ):
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                pid = int(meta.get("pid", 0) or 0)
                if pid:
                    os.kill(pid, 15)
            except Exception:
                pass
            try:
                meta_path.unlink(missing_ok=True)
            except OSError:
                pass

    # 2. Clean shell RC files
    zdotdir = Path(os.environ.get("ZDOTDIR", str(base_home))).expanduser()
    rc_candidates = [
        zdotdir / ".zshrc",
        base_home / ".zshrc",
        base_home / ".bashrc",
        base_home / ".bash_profile",
    ]
    markers = (
        "cue.plugin.zsh",
        "tawh.plugin.zsh",
        "bash/cue.bash",
        "bash/tawh.bash",
        "# cue — Smart AI",
        "# tawh — Smart AI",
        "powershell\\cue.ps1",
        "powershell/cue.ps1",
    )
    seen_paths: set[Path] = set()
    for rc in rc_candidates:
        resolved = rc.expanduser()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        if _strip_lines_matching(resolved, markers):
            removed_rc.append(str(resolved))

    # Windows PowerShell profiles
    for ps_dir in (
        base_home / "Documents" / "PowerShell",
        base_home / "Documents" / "WindowsPowerShell",
        base_home / ".config" / "powershell",
    ):
        for prof in ("Microsoft.PowerShell_profile.ps1", "profile.ps1"):
            p = ps_dir / prof
            if _strip_lines_matching(p, markers):
                removed_rc.append(str(p))

    # 3. Remove Oh My Zsh symlinks
    omz_custom = Path(os.environ.get("ZSH_CUSTOM", str(base_home / ".oh-my-zsh" / "custom"))).expanduser()
    for name in ("cue", "tawh"):
        link = omz_custom / "plugins" / name
        if link.is_symlink() or link.exists():
            try:
                if link.is_symlink() or link.is_file():
                    link.unlink()
                elif link.is_dir():
                    shutil.rmtree(link)
                removed_rc.append(str(link))
            except OSError:
                pass

    # 4. Optionally purge data folders
    removed_data: list[str] = []
    if purge_data:
        candidates = {cue_root.resolve(), (base_home / ".cue").resolve(), (base_home / ".cuee").resolve(), (base_home / ".tawh").resolve()}
        for d in candidates:
            if d.is_dir() and ((d / ".cue_home").exists() or (d / "config.json").exists() or d.name in (".cuee", ".tawh")):
                try:
                    shutil.rmtree(d)
                    removed_data.append(str(d))
                except OSError:
                    pass

    return {"removed_rc": removed_rc, "removed_data": removed_data}


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Uninstall cue plugin from terminal profiles and optionally remove stored data."""
    theme = Theme()
    print_banner(theme, "Uninstall cue")

    if not getattr(args, "yes", False):
        if not prompt_yes_no(theme, "Remove cue plugin hooks from your shell configuration?", default=True):
            print(theme.dim("  Cancelled."))
            return 0

    if getattr(args, "purge", False):
        purge = True
    elif getattr(args, "keep_data", False):
        purge = False
    elif getattr(args, "yes", False):
        purge = False
    else:
        purge = prompt_yes_no(theme, f"Also delete stored context & config folder ({get_cue_home()})?", default=False)

    res = uninstall_cue_plugin(purge_data=purge)
    print("")
    if res["removed_rc"]:
        print(f"  {theme.green('✓')} Cleaned shell configuration:")
        for p in res["removed_rc"]:
            print(f"    {theme.dim('•')} {p}")
    else:
        print(f"  {theme.dim('•')} No shell profile references found.")

    if res["removed_data"]:
        print(f"  {theme.green('✓')} Removed data folder(s):")
        for d in res["removed_data"]:
            print(f"    {theme.dim('•')} {d}")

    print(f"\n  {theme.green('✓')} cue uninstalled. Restart your terminal or run `exec $SHELL`.\n")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Configure API key or limits."""
    theme = Theme()
    cfg = load_config()
    if args.config_action == "set-key":
        return cmd_token(args)
    if args.config_action == "show":
        redacted = dict(cfg)
        if redacted.get("openrouter_api_key"):
            k = redacted["openrouter_api_key"]
            redacted["openrouter_api_key"] = f"{k[:8]}...{k[-4:]}" if len(k) > 12 else "***"
        print(json.dumps(redacted, indent=2))
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cue", description="Smart AI Zsh autocomplete & context manager")
    parser.add_argument("-v", "--version", action="version", version=f"cue {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_onboard = sub.add_parser("onboard", help="Run interactive first-session onboarding")
    p_onboard.add_argument("--force", action="store_true")
    p_onboard.set_defaults(func=cmd_onboard)

    p_analyze = sub.add_parser("analyze", help="Analyze current or target folder")
    p_analyze.add_argument("--dir", default="")
    p_analyze.add_argument("--force", action="store_true")
    p_analyze.add_argument("--no-ai", action="store_true")
    p_analyze.add_argument("--quiet", action="store_true")
    p_analyze.add_argument("--json", action="store_true")
    p_analyze.set_defaults(func=cmd_analyze)

    p_record = sub.add_parser("record", help="Record an executed command for a project")
    p_record.add_argument("--dir", default="")
    p_record.add_argument("--exit-code", type=int, default=0)
    p_record.add_argument("--no-ai", action="store_true")
    p_record.add_argument("--json", action="store_true")
    p_record.add_argument("command_str", metavar="COMMAND", nargs="?", default="")
    p_record.set_defaults(func=lambda a: cmd_record(_fix_record_ns(a)))

    p_fix = sub.add_parser("fix", help="Suggest fixes for a failed command")
    p_fix.add_argument("--dir", default="")
    p_fix.add_argument("--exit-code", type=int, default=None)
    p_fix.add_argument("--no-ai", action="store_true")
    p_fix.add_argument("--json", action="store_true")
    p_fix.add_argument("failed_cmd", nargs="?", default="")
    p_fix.set_defaults(func=cmd_fix)

    p_token = sub.add_parser("token", aliases=["key"], help="Set or change your OpenRouter API token")
    p_token.add_argument("key", nargs="?", default="")
    p_token.set_defaults(func=cmd_token)

    p_daemon = sub.add_parser("daemon", help="Manage low-latency OpenRouter keep-alive daemon")
    p_daemon.add_argument("--serve", action="store_true")
    p_daemon.add_argument("--ensure", action="store_true")
    p_daemon.add_argument("--stop", action="store_true")
    p_daemon.set_defaults(func=cmd_daemon)

    p_suggest = sub.add_parser("suggest", help="Generate ranked command suggestions")
    p_suggest.add_argument("--dir", default="")
    p_suggest.add_argument("--input", default="")
    p_suggest.add_argument("--limit", type=int, default=20)
    p_suggest.add_argument("--mode", default="auto", choices=["auto", "complete", "prompt"])
    p_suggest.add_argument("--live", action="store_true")
    p_suggest.add_argument("--tsv", action="store_true")
    p_suggest.add_argument("--json", action="store_true")
    p_suggest.set_defaults(func=cmd_suggest)

    p_ask = sub.add_parser("ask", aliases=["prompt"], help="Convert free-text prompt to runnable commands")
    p_ask.add_argument("--dir", default="")
    p_ask.add_argument("--limit", type=int, default=12)
    p_ask.add_argument("--tsv", action="store_true")
    p_ask.add_argument("--json", action="store_true")
    p_ask.add_argument("prompt_words", nargs="*", help="Natural-language prompt")
    p_ask.set_defaults(func=cmd_ask)

    p_settings = sub.add_parser("settings", help="Open interactive cue settings TUI")
    p_settings.add_argument("--prompt-mode", default="", help="Enable/disable prompt mode (on|off)")
    p_settings.set_defaults(func=cmd_settings)

    p_uninstall = sub.add_parser("uninstall", help="Uninstall cue plugin from shell profiles")
    p_uninstall.add_argument("--purge", action="store_true", help="Also delete ~/.cue / ~/.cuee data")
    p_uninstall.add_argument("--keep-data", action="store_true", help="Keep stored context data")
    p_uninstall.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_ctx = sub.add_parser("context", help="Manage ~/.cue project contexts and legacy cleanup")
    ctx_sub = p_ctx.add_subparsers(dest="context_action", required=True)
    ctx_sub.add_parser("list", help="List stored project contexts").set_defaults(func=cmd_context)
    ctx_sub.add_parser("stats", help="Show ~/.cue storage usage").set_defaults(func=cmd_context)
    p_clear = ctx_sub.add_parser("clear", help="Delete legacy or specific project context files")
    p_clear.add_argument("target", nargs="?", default="")
    p_clear.add_argument("--current", action="store_true", help="Clear current project context")
    p_clear.add_argument("--missing", action="store_true", help="Clear contexts whose folders no longer exist")
    p_clear.add_argument("--older-than", default="", help="Clear contexts older than e.g. 30d, 2w")
    p_clear.add_argument("--all", action="store_true", help="Clear all stored project contexts")
    p_clear.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p_clear.set_defaults(func=cmd_context)

    p_models = sub.add_parser("models", help="Inspect or switch OpenRouter :free models")
    p_models.add_argument("--refresh", action="store_true")
    p_models.add_argument("--reset-health", action="store_true")
    p_models.add_argument("--use", default="", help="Set preferred :free model ID")
    p_models.set_defaults(func=cmd_models)

    p_status = sub.add_parser("status", help="Show plugin and model status")
    p_status.set_defaults(func=cmd_status)

    p_hist = sub.add_parser("history", aliases=["hist"], help="Show modern project command history TUI")
    p_hist.add_argument("--dir", default="")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    p_cfg = sub.add_parser("config", help="Manage cue configuration")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)
    p_setkey = cfg_sub.add_parser("set-key", help="Set OpenRouter API key")
    p_setkey.add_argument("key", nargs="?", default="")
    p_setkey.set_defaults(func=cmd_config)
    cfg_sub.add_parser("show", help="Show config (key redacted)").set_defaults(func=cmd_config)

    return parser


def _fix_record_ns(ns: argparse.Namespace) -> argparse.Namespace:
    ns.command = ns.command_str
    return ns


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()
        return 130


if __name__ == "__main__":
    sys.exit(main())
