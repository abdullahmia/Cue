"""Project & folder analyzer: detects tech stack, package managers, scripts, and git state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

IGNORE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    ".cache",
    ".cue",
    "vendor",
    "coverage",
}

MANIFEST_FILES = (
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "Pipfile",
    "uv.lock",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Makefile",
    "justfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "mix.exs",
    "flake.nix",
)


def _run_git(cwd: Path, args: list[str], timeout: float = 1.5) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _read_text_limited(path: Path, max_bytes: int = 32768) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except OSError:
        return ""


def _detect_node(root: Path, stacks: list[str], scripts: list[dict[str, str]]) -> str:
    pkg_path = root / "package.json"
    if not pkg_path.exists():
        return ""

    pm = "npm"
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        pm = "bun"
    elif (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"

    stacks.append("Node.js")
    if (root / "tsconfig.json").exists():
        stacks.append("TypeScript")

    try:
        pkg = json.loads(_read_text_limited(pkg_path, 65536) or "{}")
    except json.JSONDecodeError:
        pkg = {}

    deps: dict[str, Any] = {}
    if isinstance(pkg, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            val = pkg.get(key)
            if isinstance(val, dict):
                deps.update(val)

        dep_names = set(deps.keys())
        framework_map = [
            ("next", "Next.js"),
            ("react", "React"),
            ("vue", "Vue"),
            ("nuxt", "Nuxt"),
            ("svelte", "Svelte"),
            ("astro", "Astro"),
            ("@nestjs/core", "NestJS"),
            ("express", "Express"),
            ("fastify", "Fastify"),
            ("vite", "Vite"),
            ("tailwindcss", "Tailwind CSS"),
            ("vitest", "Vitest"),
            ("jest", "Jest"),
            ("playwright", "Playwright"),
            ("prisma", "Prisma"),
        ]
        for dep, label in framework_map:
            if dep in dep_names and label not in stacks:
                stacks.append(label)

        pkg_scripts = pkg.get("scripts")
        if isinstance(pkg_scripts, dict):
            for name, target_cmd in list(pkg_scripts.items())[:12]:
                run_prefix = f"{pm} run " if pm == "npm" and name not in ("test", "start") else f"{pm} "
                scripts.append(
                    {
                        "cmd": f"{run_prefix}{name}",
                        "desc": f"package.json: {str(target_cmd)[:55]}",
                    }
                )
    return pm


def _detect_python(root: Path, stacks: list[str], scripts: list[dict[str, str]]) -> str:
    has_py = any(
        (root / f).exists()
        for f in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "manage.py")
    )
    if not has_py and not any(root.glob("*.py")):
        return ""

    stacks.append("Python")
    pm = "pip"
    if (root / "uv.lock").exists():
        pm = "uv"
        stacks.append("uv")
    elif (root / "poetry.lock").exists():
        pm = "poetry"
        stacks.append("Poetry")
    elif (root / "Pipfile").exists():
        pm = "pipenv"

    py_text = ""
    if (root / "pyproject.toml").exists():
        py_text += _read_text_limited(root / "pyproject.toml") + "\n"
    if (root / "requirements.txt").exists():
        py_text += _read_text_limited(root / "requirements.txt") + "\n"

    lower = py_text.lower()
    for pkg, label in [
        ("fastapi", "FastAPI"),
        ("django", "Django"),
        ("flask", "Flask"),
        ("pytest", "pytest"),
        ("ruff", "Ruff"),
        ("mypy", "mypy"),
        ("sqlalchemy", "SQLAlchemy"),
        ("pydantic", "Pydantic"),
    ]:
        if pkg in lower and label not in stacks:
            stacks.append(label)

    if (root / "manage.py").exists():
        scripts.append({"cmd": "python3 manage.py runserver", "desc": "Django dev server"})
        scripts.append({"cmd": "python3 manage.py test", "desc": "Run Django tests"})

    if "pytest" in lower or (root / "tests").is_dir():
        runner = "uv run pytest" if pm == "uv" else ("poetry run pytest" if pm == "poetry" else "pytest")
        scripts.append({"cmd": runner, "desc": "Run Python test suite"})

    if "ruff" in lower:
        prefix = "uv run " if pm == "uv" else ("poetry run " if pm == "poetry" else "")
        scripts.append({"cmd": f"{prefix}ruff check .", "desc": "Lint Python code with Ruff"})

    return pm


def _detect_makefile(root: Path, scripts: list[dict[str, str]]) -> None:
    makefile = root / "Makefile"
    if not makefile.exists():
        return
    text = _read_text_limited(makefile, 32768)
    targets = re.findall(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]*)\s*:", text, flags=re.MULTILINE)
    seen = set()
    for t in targets:
        if t.startswith(".") or t in seen:
            continue
        seen.add(t)
        scripts.append({"cmd": f"make {t}", "desc": f"Makefile target '{t}'"})
        if len(seen) >= 10:
            break


def _detect_other_stacks(root: Path, stacks: list[str], scripts: list[dict[str, str]]) -> None:
    if (root / "Cargo.toml").exists():
        stacks.append("Rust")
        scripts.extend(
            [
                {"cmd": "cargo run", "desc": "Build and run Rust crate"},
                {"cmd": "cargo test", "desc": "Run Cargo tests"},
                {"cmd": "cargo clippy", "desc": "Run Rust clippy lints"},
            ]
        )

    if (root / "go.mod").exists():
        stacks.append("Go")
        scripts.extend(
            [
                {"cmd": "go test ./...", "desc": "Run all Go tests"},
                {"cmd": "go run .", "desc": "Run Go package"},
                {"cmd": "go build ./...", "desc": "Build Go packages"},
            ]
        )

    if (root / "Gemfile").exists():
        stacks.append("Ruby")
        if (root / "bin" / "rails").exists():
            stacks.append("Rails")
            scripts.append({"cmd": "bundle exec rails server", "desc": "Start Rails server"})

    if (root / "composer.json").exists():
        stacks.append("PHP")
        if (root / "artisan").exists():
            stacks.append("Laravel")
            scripts.append({"cmd": "php artisan serve", "desc": "Start Laravel server"})

    if (root / "pom.xml").exists():
        stacks.append("Java (Maven)")
        scripts.append({"cmd": "mvn test", "desc": "Run Maven tests"})

    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        stacks.append("Gradle")
        cmd_prefix = "./gradlew" if (root / "gradlew").exists() else "gradle"
        scripts.append({"cmd": f"{cmd_prefix} test", "desc": "Run Gradle tests"})

    if (root / "mix.exs").exists():
        stacks.append("Elixir")
        scripts.append({"cmd": "mix test", "desc": "Run Elixir tests"})

    if (root / "flake.nix").exists():
        stacks.append("Nix")

    if any(root.glob("*.plugin.zsh")):
        stacks.append("Zsh Plugin")

    if any(root.glob("*.tf")):
        stacks.append("Terraform")
        scripts.append({"cmd": "terraform plan", "desc": "Preview Terraform changes"})

    if (root / "Dockerfile").exists():
        stacks.append("Docker")

    for compose_name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (root / compose_name).exists():
            if "Docker Compose" not in stacks:
                stacks.append("Docker Compose")
            scripts.append({"cmd": "docker compose up -d", "desc": "Start compose services"})
            scripts.append({"cmd": "docker compose logs -f", "desc": "Follow compose logs"})
            break

    _detect_makefile(root, scripts)


def _scan_tree(root: Path, max_entries: int = 35) -> list[str]:
    entries: list[str] = []
    is_home = root.resolve() == Path.home().resolve()
    try:
        top_items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return entries

    for item in top_items:
        name = item.name
        if name in IGNORE_DIRS:
            continue
        if is_home and name.startswith("."):
            continue
        if item.is_dir():
            entries.append(f"{name}/")
            if not is_home and len(entries) < max_entries:
                try:
                    sub_items = sorted(item.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                except OSError:
                    sub_items = []
                for sub in sub_items[:6]:
                    if sub.name in IGNORE_DIRS or sub.name.startswith("."):
                        continue
                    suffix = "/" if sub.is_dir() else ""
                    entries.append(f"  {name}/{sub.name}{suffix}")
                    if len(entries) >= max_entries:
                        break
        else:
            entries.append(name)
        if len(entries) >= max_entries:
            break
    return entries


def compute_fingerprint(root: Path) -> str:
    """Compute a fast fingerprint of the directory's manifests, top entries, and git HEAD."""
    h = hashlib.sha256()
    h.update(str(root).encode("utf-8"))
    for fname in MANIFEST_FILES:
        p = root / fname
        if p.exists():
            try:
                st = p.stat()
                h.update(f"{fname}:{int(st.st_mtime)}:{st.st_size};".encode("utf-8"))
            except OSError:
                pass
    git_head = root / ".git" / "HEAD"
    if git_head.exists():
        h.update(_read_text_limited(git_head, 256).encode("utf-8"))
    try:
        names = sorted(p.name for p in root.iterdir())[:40]
        h.update(",".join(names).encode("utf-8"))
    except OSError:
        pass
    return h.hexdigest()[:16]


def analyze_project(directory: str | Path) -> dict[str, Any]:
    """Analyze a folder or project root and return compact structured metadata."""
    root = Path(directory).expanduser().resolve()
    if root.is_file():
        root = root.parent

    stacks: list[str] = []
    scripts: list[dict[str, str]] = []

    node_pm = _detect_node(root, stacks, scripts)
    py_pm = _detect_python(root, stacks, scripts)
    _detect_other_stacks(root, stacks, scripts)

    package_manager = node_pm or py_pm or ""
    if not stacks:
        stacks.append("General Folder")

    git_info: dict[str, Any] = {"is_repo": False}
    if (root / ".git").exists() or _run_git(root, ["rev-parse", "--is-inside-work-tree"]) == "true":
        branch = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD"
        status_raw = _run_git(root, ["status", "--short"])
        status_lines = [line for line in status_raw.splitlines() if line.strip()][:8]
        recent_commits = [
            line for line in _run_git(root, ["log", "-n", "3", "--oneline"]).splitlines() if line.strip()
        ]
        git_info = {
            "is_repo": True,
            "branch": branch,
            "dirty": len(status_lines) > 0,
            "status": status_lines,
            "recent_commits": recent_commits,
        }
        if status_lines:
            scripts.append({"cmd": "git status", "desc": f"{len(status_lines)}+ uncommitted changes"})
            scripts.append({"cmd": "git diff", "desc": "Inspect working tree changes"})

    # Deduplicate scripts preserving order
    dedup_scripts: list[dict[str, str]] = []
    seen_cmds: set[str] = set()
    for item in scripts:
        c = item["cmd"]
        if c not in seen_cmds:
            seen_cmds.add(c)
            dedup_scripts.append(item)

    tree = _scan_tree(root)
    return {
        "root": str(root),
        "name": root.name or str(root),
        "stacks": stacks,
        "package_manager": package_manager,
        "scripts": dedup_scripts[:20],
        "git": git_info,
        "tree": tree,
        "fingerprint": compute_fingerprint(root),
    }
