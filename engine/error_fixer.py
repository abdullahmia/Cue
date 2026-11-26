"""Fast, cross-platform local & diagnostic error-fix engine for failed terminal commands."""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _split_cmd(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return cmd.strip().split()


def _find_local_venv_python(root: Path) -> str | None:
    candidates = (
        [root / ".venv" / "Scripts" / "python.exe", root / "venv" / "Scripts" / "python.exe"]
        if os.name == "nt"
        else [root / ".venv" / "bin" / "python3", root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python3"]
    )
    for c in candidates:
        if c.exists():
            return str(c.relative_to(root)) if c.is_relative_to(root) else str(c)
    return None


def _find_local_venv_bin(root: Path, binary: str) -> str | None:
    ext = ".exe" if os.name == "nt" else ""
    folder = "Scripts" if os.name == "nt" else "bin"
    for venv_name in (".venv", "venv", ".env"):
        candidate = root / venv_name / folder / f"{binary}{ext}"
        if candidate.exists():
            return str(candidate.relative_to(root))
    return None


def _python_module_installed(python_exe: str, module_name: str) -> bool:
    if python_exe in ("python3", "python", "py"):
        return importlib.util.find_spec(module_name) is not None
    try:
        proc = subprocess.run(
            [python_exe, "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"],
            capture_output=True,
            timeout=1.5,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def diagnose_command_failure(
    failed_cmd: str,
    exit_code: int,
    directory: str | Path,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce instant local fix suggestions + diagnostic context for a failed command."""
    cmd = failed_cmd.strip()
    if not cmd or exit_code in (0, 130):
        return {"fixes": [], "diagnosis": ""}

    root = Path(directory).expanduser().resolve()
    tokens = _split_cmd(cmd)
    if not tokens:
        return {"fixes": [], "diagnosis": ""}

    is_win = platform.system() == "Windows"
    is_mac = platform.system() == "Darwin"
    fixes: list[dict[str, str]] = []
    notes: list[str] = []
    pm = (analysis or {}).get("package_manager", "")

    def add_fix(fix_cmd: str, reason: str) -> None:
        if fix_cmd and fix_cmd != cmd and not any(f["cmd"] == fix_cmd for f in fixes):
            fixes.append({"cmd": fix_cmd, "reason": reason[:85]})

    # 1. Python `-m <module>` failures (e.g. `python3 -m pytest ...` -> No module named pytest)
    if tokens[0] in ("python", "python3", "py") or tokens[0].endswith(("python", "python3", "python.exe")):
        if "-m" in tokens:
            m_idx = tokens.index("-m")
            if m_idx + 1 < len(tokens):
                mod_name = tokens[m_idx + 1].split(".")[0]
                rest_args = " ".join(tokens[m_idx + 2 :])
                py_exe = shutil.which(tokens[0]) or tokens[0]
                if not _python_module_installed(py_exe, mod_name):
                    notes.append(f"Python module '{mod_name}' is not installed in {tokens[0]}")
                    venv_bin = _find_local_venv_bin(root, mod_name)
                    venv_py = _find_local_venv_python(root)
                    if venv_py:
                        add_fix(f"{venv_py} -m {mod_name} {rest_args}".strip(), f"Run using project virtualenv ({venv_py})")
                    if venv_bin:
                        add_fix(f"{venv_bin} {rest_args}".strip(), f"Run {mod_name} from local virtualenv")
                    if shutil.which("uv") and ((root / "uv.lock").exists() or pm == "uv"):
                        add_fix(f"uv run {mod_name} {rest_args}".strip(), f"Run {mod_name} via uv environment")
                    if shutil.which("poetry") and ((root / "poetry.lock").exists() or pm == "poetry"):
                        add_fix(f"poetry run {mod_name} {rest_args}".strip(), f"Run {mod_name} via Poetry")
                    add_fix(f"{tokens[0]} -m pip install {mod_name}", f"Install missing '{mod_name}' package")
                    if mod_name == "pytest" and (root / "tests").is_dir():
                        add_fix(f"{tokens[0]} -m unittest discover -s tests -v", "Run tests with stdlib unittest")

    # 2. Command not found (POSIX exit 127, or Windows missing executable)
    first_bin = tokens[0]
    bin_missing = (exit_code == 127) or (not first_bin.startswith((".", "/", "~")) and shutil.which(first_bin) is None and not os.path.exists(root / first_bin))
    if bin_missing and first_bin not in ("cd", "export", "set", "alias", "source", "."):
        notes.append(f"Command '{first_bin}' not found in PATH")
        venv_bin = _find_local_venv_bin(root, first_bin)
        if venv_bin:
            rest = " ".join(tokens[1:])
            add_fix(f"{venv_bin} {rest}".strip(), f"Use {first_bin} from local virtualenv")
        node_bin = root / "node_modules" / ".bin" / first_bin
        if node_bin.exists():
            add_fix(f"npx {cmd}", f"Run local node_modules binary '{first_bin}'")
        if first_bin == "pytest":
            py_cmd = "python" if is_win else "python3"
            add_fix(f"{py_cmd} -m pip install pytest", "Install pytest via pip")
            if (root / "tests").is_dir():
                add_fix(f"{py_cmd} -m unittest discover -s tests -v", "Run tests with stdlib unittest")
        elif is_mac and shutil.which("brew"):
            add_fix(f"brew install {first_bin}", f"Install '{first_bin}' with Homebrew")
        elif is_win:
            add_fix(f"winget search {first_bin}", f"Search Windows package for '{first_bin}'")
        elif shutil.which("apt-get"):
            add_fix(f"sudo apt-get install -y {first_bin}", f"Install '{first_bin}' via apt")
        add_fix(f"which {first_bin}" if not is_win else f"Get-Command {first_bin}", f"Check PATH for '{first_bin}'")

    # 3. Permission denied (exit 126)
    if exit_code == 126 and not is_win:
        target = tokens[0]
        notes.append(f"Permission denied executing '{target}'")
        add_fix(f"chmod +x {target} && {cmd}", f"Make '{target}' executable and retry")

    # 4. Git common failure patterns
    if first_bin == "git":
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub == "push":
            notes.append("git push failed (missing upstream, non-fast-forward, or auth)")
            add_fix("git push -u origin HEAD", "Push and set upstream tracking branch")
            add_fix("git pull --rebase && git push", "Rebase on remote changes then push")
        elif sub == "commit":
            notes.append("git commit failed (nothing staged, hook failed, or missing -m)")
            add_fix("git status", "Check staged and unstaged files")
            add_fix("git add -A && git status", "Stage changes before committing")
        elif sub in ("checkout", "switch"):
            notes.append("Branch switch failed (uncommitted changes or missing branch)")
            add_fix("git stash && " + cmd, "Stash uncommitted changes and switch")
            add_fix("git branch -a", "List local and remote branches")
        elif sub == "pull":
            add_fix("git pull --rebase", "Pull with rebase")
            add_fix("git status", "Inspect working tree conflicts/state")

    # 5. Node / npm / pnpm / yarn / bun failures
    if first_bin in ("npm", "pnpm", "yarn", "bun"):
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub in ("run", "dev", "build", "test", "start") and not (root / "node_modules").exists():
            notes.append("node_modules directory is missing")
            add_fix(f"{first_bin} install && {cmd}", "Install dependencies then retry")
        elif sub in ("run", "dev", "build", "test"):
            add_fix(f"{first_bin} install", "Ensure dependencies are up to date")

    # 6. Python pip / uv / pytest general failures
    if first_bin in ("pytest", "uv") and not fixes:
        if first_bin == "pytest" and (root / "uv.lock").exists():
            add_fix(f"uv run {cmd}", "Run pytest inside uv environment")

    # 7. Docker / Compose failures
    if first_bin == "docker":
        notes.append("Docker command failed (daemon stopped, port conflict, or missing container)")
        add_fix("docker ps -a", "Check running and stopped containers")
        if is_mac:
            add_fix("open -a Docker", "Start Docker Desktop on macOS")

    # 8. Port conflict / network server heuristics
    if re.search(r"\b(dev|serve|server|start|uvicorn|gunicorn|next)\b", cmd):
        add_fix("lsof -i :3000" if not is_win else "netstat -ano | findstr :3000", "Check if port 3000 is already in use")

    return {
        "fixes": fixes[:6],
        "diagnosis": "; ".join(notes) if notes else f"Command `{cmd}` exited with status {exit_code}",
    }
