"""Comprehensive unit & integration tests for the cue Python engine."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.cli import main as cli_main, uninstall_cue_plugin
from engine.config import DEFAULT_FREE_MODELS, load_config, load_state, migrate_legacy_tawh_home, resolve_default_cue_home, save_config
from engine.context_manager import ContextManager, is_sensitive_command, make_project_id, resolve_project_root
from engine.error_fixer import diagnose_command_failure
from engine.openrouter_client import AllModelsExhaustedError, AuthError, OpenRouterClient, _build_ssl_context, is_free_model
from engine.project_analyzer import analyze_project
from engine.suggester import build_ranked_suggestions, write_zsh_suggestion_cache
from engine.tui import Theme, draw_card, pad_visible, visible_len


class TestContextManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cue_home = Path(self.tmp.name) / ".cue"
        self.projects_root = Path(self.tmp.name) / "workspace"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        save_config({"max_project_bytes": 12 * 1024, "max_total_bytes": 32 * 1024}, home=self.cue_home)
        self.cm = ContextManager(home=self.cue_home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stores_everything_inside_cue_home(self) -> None:
        proj = self.projects_root / "app1"
        proj.mkdir()
        self.cm.record_command(proj, "npm run dev", exit_code=0)
        pid = make_project_id(proj)
        pfile = self.cue_home / "projects" / f"{pid}.json"
        self.assertTrue(pfile.exists())
        self.assertTrue((self.cue_home / "projects" / "index.json").exists())

    def test_migrates_legacy_tawh_home_to_cue(self) -> None:
        legacy_dir = Path(self.tmp.name) / ".legacy_tawh"
        new_cue = Path(self.tmp.name) / ".new_cue"
        (legacy_dir / "projects").mkdir(parents=True)
        (legacy_dir / "config.json").write_text(json.dumps({"openrouter_api_key": "sk-or-v1-migrated", "onboarded": True}), encoding="utf-8")
        (legacy_dir / "projects" / "demo-123.json").write_text('{"name":"demo"}', encoding="utf-8")

        migrated = migrate_legacy_tawh_home(new_cue, legacy_root=legacy_dir)
        self.assertTrue(migrated)
        self.assertTrue((new_cue / "config.json").exists())
        self.assertTrue((new_cue / "projects" / "demo-123.json").exists())
        cfg = load_config(home=new_cue)
        self.assertEqual(cfg["openrouter_api_key"], "sk-or-v1-migrated")
        self.assertTrue(cfg["onboarded"])

    def test_sensitive_commands_not_recorded(self) -> None:
        proj = self.projects_root / "sec"
        proj.mkdir()
        self.assertTrue(is_sensitive_command("export OPENROUTER_API_KEY=sk-or-v1-abcdef12345"))
        res = self.cm.record_command(proj, "export API_KEY=supersecret123", exit_code=0)
        self.assertIsNone(res)

    def test_project_file_size_cap_enforced(self) -> None:
        proj = self.projects_root / "heavy"
        proj.mkdir()
        ctx = self.cm.load_project(proj)
        # Stuff huge data into turns & recent commands
        for i in range(60):
            self.cm.append_ai_turn(ctx, f"user prompt {i} " + ("x" * 400), f"assistant reply {i} " + ("y" * 600), DEFAULT_FREE_MODELS[0])
        for i in range(150):
            ctx["recent_commands"].append({"cmd": f"pytest tests/test_{i}.py --verbose " + ("a" * 120), "exit_code": 0, "ts": int(time.time()), "count": 1})
        pfile = self.cm.save_project(ctx)
        self.assertLessEqual(pfile.stat().st_size, 12 * 1024)

    def test_total_storage_lru_eviction(self) -> None:
        # Create several projects exceeding max_total_bytes (32 KB)
        created_ids = []
        for i in range(6):
            p = self.projects_root / f"proj_{i}"
            p.mkdir()
            ctx = self.cm.load_project(p)
            ctx["analysis"] = {"stacks": ["Python"], "tree": [f"file_{j}.py" for j in range(30)]}
            for j in range(45):
                ctx["recent_commands"].append({"cmd": f"python3 run_{j}.py " + ("z" * 100), "exit_code": 0, "ts": 1000 + i, "count": 1})
            self.cm.save_project(ctx)
            # Stagger last_accessed timestamps on disk
            pfile = self.cm.project_file(ctx["project_id"])
            data = json.loads(pfile.read_text(encoding="utf-8"))
            data["last_accessed"] = 1000 + i * 100
            pfile.write_text(json.dumps(data), encoding="utf-8")
            created_ids.append(ctx["project_id"])

        self.cm.enforce_total_size(protected_id=created_ids[-1])
        total_proj_bytes = sum(
            p.stat().st_size for p in (self.cue_home / "projects").glob("*.json") if p.name != "index.json"
        )
        self.assertLessEqual(total_proj_bytes, 32 * 1024)
        # Latest protected project must still exist
        self.assertTrue(self.cm.project_file(created_ids[-1]).exists())

    def test_clear_legacy_missing_and_older_than(self) -> None:
        old_dir = self.projects_root / "legacy_app"
        old_dir.mkdir()
        gone_dir = self.projects_root / "deleted_app"
        gone_dir.mkdir()
        keep_dir = self.projects_root / "active_app"
        keep_dir.mkdir()

        self.cm.record_command(old_dir, "make build")
        self.cm.record_command(gone_dir, "cargo test")
        self.cm.record_command(keep_dir, "pnpm dev")

        # Make old_dir 45 days old
        old_ctx = self.cm.load_project(old_dir)
        old_file = self.cm.project_file(old_ctx["project_id"])
        data = json.loads(old_file.read_text(encoding="utf-8"))
        data["last_accessed"] = int(time.time()) - 45 * 86400
        old_file.write_text(json.dumps(data), encoding="utf-8")

        # Delete gone_dir from disk
        gone_dir.rmdir()

        deleted_missing = self.cm.clear_legacy(missing_only=True)
        self.assertEqual(len(deleted_missing), 1)
        self.assertEqual(deleted_missing[0]["name"], "deleted_app")

        deleted_old = self.cm.clear_legacy(older_than_days=30)
        self.assertEqual(len(deleted_old), 1)
        self.assertEqual(deleted_old[0]["name"], "legacy_app")

        remaining = self.cm.list_projects()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "active_app")


class TestProjectAnalyzer(unittest.TestCase):
    def test_detects_nextjs_pnpm_and_makefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web"
            root.mkdir()
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9.0\n", encoding="utf-8")
            (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "web",
                        "dependencies": {"next": "15.0.0", "react": "19.0.0"},
                        "scripts": {"dev": "next dev", "build": "next build"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "Makefile").write_text("lint:\n\tpnpm lint\n", encoding="utf-8")

            res = analyze_project(root)
            self.assertEqual(res["package_manager"], "pnpm")
            self.assertIn("Node.js", res["stacks"])
            self.assertIn("TypeScript", res["stacks"])
            self.assertIn("Next.js", res["stacks"])
            cmds = [s["cmd"] for s in res["scripts"]]
            self.assertIn("pnpm dev", cmds)
            self.assertIn("make lint", cmds)

    def test_detects_python_uv_pytest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "api"
            root.mkdir()
            (root / "uv.lock").write_text("", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[project]\nname="api"\ndependencies=["fastapi","pytest"]\n',
                encoding="utf-8",
            )
            res = analyze_project(root)
            self.assertEqual(res["package_manager"], "uv")
            self.assertIn("Python", res["stacks"])
            self.assertIn("FastAPI", res["stacks"])
            self.assertIn("uv run pytest", [s["cmd"] for s in res["scripts"]])


class TestOpenRouterClient(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cue_home = Path(self.tmp.name) / ".cue"
        save_config({"openrouter_api_key": "sk-or-v1-testkey123", "preferred_model": "qwen/qwen3-coder:free"}, home=self.cue_home)
        self.client = OpenRouterClient(home=self.cue_home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_strict_free_models_only(self) -> None:
        self.assertTrue(is_free_model("qwen/qwen3-coder:free"))
        self.assertTrue(is_free_model("openrouter/free"))
        self.assertFalse(is_free_model("openai/gpt-4o"))
        self.assertFalse(is_free_model("anthropic/claude-sonnet-4"))
        self.assertFalse(is_free_model("some/model:free", {"pricing": {"prompt": "0.001", "completion": "0"}}))

    def test_switches_model_on_rate_limit_and_preserves_context(self) -> None:
        import urllib.error
        import io

        cm = ContextManager(home=self.cue_home)
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        ctx = cm.load_project(proj)
        ctx["analysis"] = {"stacks": ["Python", "pytest"], "package_manager": "uv", "scripts": [], "git": {}, "tree": []}
        ctx["recent_commands"] = [{"cmd": "uv run pytest", "exit_code": 1, "ts": int(time.time()), "count": 1}]
        cm.append_ai_turn(ctx, "previous user action", "suggested fixing test_auth", "qwen/qwen3-coder:free")

        called_models: list[str] = []
        captured_messages: list[list[dict[str, str]]] = []

        def fake_post(api_key: str, model_id: str, messages: list[dict[str, str]], timeout: float = 18.0):
            called_models.append(model_id)
            captured_messages.append(messages)
            if len(called_models) == 1:
                raise urllib.error.HTTPError(
                    url="https://openrouter.ai/api/v1/chat/completions",
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"message":"Rate limit exceeded for daily free quota"}}'),
                )
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "suggestions": [
                                        {"cmd": "uv run pytest -x --lf", "reason": "Re-run last failed test"},
                                        {"cmd": "git diff", "reason": "Inspect test changes"},
                                    ],
                                    "summary": "Debugging failing pytest run in Python project",
                                }
                            )
                        }
                    }
                ]
            }

        with mock.patch.object(self.client, "fetch_free_models", return_value=[{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS]):
            with mock.patch.object(self.client, "_post_chat_completion", side_effect=fake_post):
                res = self.client.generate_suggestions(ctx, partial_input="uv", cwd=str(proj), mode="complete")

        self.assertEqual(len(called_models), 2)
        self.assertNotEqual(called_models[0], called_models[1])
        self.assertTrue(is_free_model(called_models[1]))
        self.assertTrue(res["switched"])
        self.assertEqual(res["suggestions"][0]["cmd"], "uv run pytest -x --lf")

        # Verify context was carried over to the fallback model
        fallback_msgs_text = json.dumps(captured_messages[1])
        self.assertIn("suggested fixing test_auth", fallback_msgs_text)
        self.assertIn("uv run pytest", fallback_msgs_text)

        state = load_state(self.cue_home)
        self.assertEqual(state["active_model"], called_models[1])
        self.assertGreaterEqual(state["total_fallbacks"], 1)

    def test_auth_error_stops_immediately(self) -> None:
        import urllib.error
        import io

        cm = ContextManager(home=self.cue_home)
        proj = Path(self.tmp.name) / "proj2"
        proj.mkdir()
        ctx = cm.load_project(proj)

        def fake_post(api_key: str, model_id: str, messages: list[dict[str, str]], timeout: float = 18.0):
            raise urllib.error.HTTPError(
                url="https://openrouter.ai/api/v1/chat/completions",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b"Invalid key"),
            )

        with mock.patch.object(self.client, "fetch_free_models", return_value=[{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS]):
            with mock.patch.object(self.client, "_post_chat_completion", side_effect=fake_post):
                with self.assertRaises(AuthError):
                    self.client.generate_suggestions(ctx)

    def test_ssl_context_loads_ca_certs(self) -> None:
        ctx = _build_ssl_context()
        stats = ctx.cert_store_stats()
        self.assertGreater(stats.get("x509_ca", 0), 0)


class TestSuggester(unittest.TestCase):
    def test_ranking_and_tsv_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue_home = Path(tmp) / ".cue"
            ctx = {
                "project_id": "demo-1234",
                "recent_commands": [
                    {"cmd": "git status", "exit_code": 0, "ts": int(time.time()) - 60, "count": 3},
                    {"cmd": "pnpm dev", "exit_code": 0, "ts": int(time.time()) - 10, "count": 5},
                ],
                "cached_suggestions": [
                    {"cmd": "pnpm test", "reason": "Run test suite"},
                    {"cmd": "git commit -m 'wip'", "reason": "Commit staged files"},
                ],
                "analysis": {"scripts": [{"cmd": "pnpm build", "desc": "Build app"}]},
            }
            ranked = build_ranked_suggestions(ctx, partial_input="pnpm")
            cmds = [r["cmd"] for r in ranked]
            self.assertIn("pnpm dev", cmds)
            self.assertIn("pnpm test", cmds)
            self.assertIn("pnpm build", cmds)
            self.assertNotIn("git status", cmds)

            tsv_path = write_zsh_suggestion_cache(ctx, home=cue_home)
            self.assertTrue(tsv_path.exists())
            content = tsv_path.read_text(encoding="utf-8")
            self.assertIn("pnpm dev", content)

    def test_error_fixes_rank_first(self) -> None:
        ctx = {
            "project_id": "err-1",
            "last_error": {
                "cmd": "python3 -m pytest tests/test_cue_engine.py -v",
                "exit_code": 1,
                "ts": int(time.time()),
                "fixes": [
                    {"cmd": "python3 -m pip install pytest", "reason": "Install missing 'pytest' package"},
                    {"cmd": "python3 -m unittest discover -s tests -v", "reason": "Run tests with stdlib unittest"},
                ],
            },
            "recent_commands": [{"cmd": "git status", "exit_code": 0, "ts": int(time.time()), "count": 1}],
        }
        ranked = build_ranked_suggestions(ctx)
        self.assertEqual(ranked[0]["source"], "fix")
        self.assertEqual(ranked[0]["cmd"], "python3 -m pip install pytest")

    def test_recent_project_commands_rank_above_ai(self) -> None:
        ctx = {
            "project_id": "prio-1",
            "recent_commands": [
                {"cmd": "git status", "exit_code": 0, "ts": int(time.time()) - 20, "count": 1},
            ],
            "cached_suggestions": [
                {"cmd": "pnpm dev", "reason": "Top AI suggestion"},
                {"cmd": "pnpm test", "reason": "Second AI suggestion"},
            ],
            "analysis": {"scripts": [{"cmd": "pnpm build", "desc": "Build script"}]},
        }
        ranked = build_ranked_suggestions(ctx)
        sources = [r["source"] for r in ranked]
        self.assertEqual(sources[0], "proj")
        self.assertEqual(ranked[0]["cmd"], "git status")
        self.assertEqual(sources[1], "ai")
        self.assertEqual(sources[-1], "script")

    def test_suggestion_section_caps_recent3_ai12_then_others(self) -> None:
        now = int(time.time())
        ctx = {
            "project_id": "caps-1",
            "recent_commands": [{"cmd": f"rec_{i}", "exit_code": 0, "ts": now + i, "count": 1} for i in range(6)],
            "cached_suggestions": [{"cmd": f"ai_{i}", "reason": "AI"} for i in range(15)],
            "analysis": {"scripts": [{"cmd": "make test", "desc": "Script"}]},
        }
        ranked = build_ranked_suggestions(ctx, global_history=["hist_1", "hist_2"], limit=25)
        sources = [r["source"] for r in ranked]
        self.assertEqual(sources[:3], ["proj", "proj", "proj"])
        self.assertEqual(sources[3:15], ["ai"] * 12)
        self.assertEqual(sources[15], "script")
        self.assertEqual(sources[16:], ["hist", "hist"])

    def test_retry_ranks_first_after_fix_succeeds(self) -> None:
        ctx = {
            "project_id": "retry-1",
            "pending_retry": {"cmd": "python3 -m pytest tests/test_cue_engine.py -v", "ts": int(time.time())},
            "recent_commands": [
                {"cmd": "python3 -m pip install pytest", "exit_code": 0, "ts": int(time.time()), "count": 1},
            ],
            "cached_suggestions": [{"cmd": "git status", "reason": "AI suggestion"}],
        }
        ranked = build_ranked_suggestions(ctx)
        self.assertEqual(ranked[0]["source"], "retry")
        self.assertEqual(ranked[0]["cmd"], "python3 -m pytest tests/test_cue_engine.py -v")


class TestErrorFixerAndLatency(unittest.TestCase):
    def test_diagnose_missing_pytest_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            diag = diagnose_command_failure(
                "python3 -m nonexistent_pkg_xyz tests/test_cue_engine.py -v",
                exit_code=1,
                directory=root,
            )
            cmds = [f["cmd"] for f in diag["fixes"]]
            self.assertIn("python3 -m pip install nonexistent_pkg_xyz", cmds)
            self.assertIn("nonexistent_pkg_xyz", diag["diagnosis"])

    def test_latency_ranking_prefers_fast_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue_home = Path(tmp) / ".cue"
            save_config({"openrouter_api_key": "sk-or-v1-test", "preferred_model": "qwen/qwen3-coder:free"}, home=cue_home)
            client = OpenRouterClient(home=cue_home)
            client.record_model_latency("qwen/qwen3-coder:free", 4800.0)
            client.record_model_latency("google/gemma-3-12b-it:free", 320.0)
            with mock.patch.object(
                client,
                "fetch_free_models",
                return_value=[{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS],
            ):
                candidates = client.get_candidate_models()
            self.assertEqual(candidates[0], "google/gemma-3-12b-it:free")


class TestSettingsPromptModeAndUninstall(unittest.TestCase):
    def test_prompt_mode_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(home=Path(tmp) / ".cue")
            self.assertFalse(cfg["prompt_mode"])

    def test_resolve_cue_home_uses_cuee_when_foreign_cue_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Clean home -> ~/.cue
            self.assertEqual(resolve_default_cue_home(base), base / ".cue")

            # Foreign ~/.cue exists without .cue_home or config.json -> ~/.cuee
            (base / ".cue").mkdir()
            (base / ".cue" / "other_tool.txt").write_text("unrelated", encoding="utf-8")
            self.assertEqual(resolve_default_cue_home(base), base / ".cuee")

            # Managed ~/.cuee continues using ~/.cuee
            (base / ".cuee").mkdir()
            (base / ".cuee" / ".cue_home").write_text("cue\n", encoding="utf-8")
            self.assertEqual(resolve_default_cue_home(base), base / ".cuee")

            # Managed ~/.cue uses ~/.cue
            (base / ".cue" / ".cue_home").write_text("cue\n", encoding="utf-8")
            self.assertEqual(resolve_default_cue_home(base), base / ".cue")

    def test_settings_toggles_prompt_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue_home = Path(tmp) / ".cue"
            with mock.patch.dict(os.environ, {"CUE_HOME": str(cue_home)}):
                rc = cli_main(["settings", "--prompt-mode", "on"])
                self.assertEqual(rc, 0)
                self.assertTrue(load_config(home=cue_home)["prompt_mode"])
                rc2 = cli_main(["settings", "--prompt-mode", "off"])
                self.assertEqual(rc2, 0)
                self.assertFalse(load_config(home=cue_home)["prompt_mode"])

    def test_prompt_mode_ask_translates_natural_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue_home = Path(tmp) / ".cue"
            save_config({"openrouter_api_key": "sk-or-v1-test", "prompt_mode": True}, home=cue_home)
            client = OpenRouterClient(home=cue_home)
            captured: list[list[dict[str, str]]] = []

            def fake_post(api_key: str, model_id: str, messages: list[dict[str, str]], timeout: float = 9.0):
                captured.append(messages)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "suggestions": [{"cmd": "lsof -i :3000", "reason": "Find process on port 3000"}],
                                        "summary": "Check port 3000",
                                    }
                                )
                            }
                        }
                    ]
                }

            cm = ContextManager(home=cue_home)
            ctx = cm.load_project(Path(tmp))
            with mock.patch.object(client, "fetch_free_models", return_value=[{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS]):
                with mock.patch.object(client, "_post_chat_completion", side_effect=fake_post):
                    res = client.generate_suggestions(ctx, partial_input="check which process uses port 3000", mode="prompt")
            self.assertEqual(res["suggestions"][0]["cmd"], "lsof -i :3000")
            self.assertEqual(res["summary"], "Check port 3000")
            self.assertIn('"mode":"prompt"', captured[0][-1]["content"])
            self.assertIn('"cue_version":', captured[0][-1]["content"])

    def test_cli_ask_json_and_tsv_include_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue_home = Path(tmp) / ".cue"
            proj = Path(tmp) / "proj"
            proj.mkdir()
            save_config({"openrouter_api_key": "sk-or-v1-test", "prompt_mode": True}, home=cue_home)

            def fake_post(api_key: str, model_id: str, messages: list[dict[str, str]], timeout: float = 9.0):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "suggestions": [{"cmd": "cue --version", "reason": "Show installed cue version"}],
                                        "summary": "cue plugin version is 0.0.1",
                                    }
                                )
                            }
                        }
                    ]
                }

            with mock.patch.dict(os.environ, {"CUE_HOME": str(cue_home)}):
                with mock.patch("engine.cli.request_daemon", return_value=None):
                    with mock.patch.object(
                        OpenRouterClient,
                        "fetch_free_models",
                        return_value=[{"id": m, "name": m, "context_length": 32768} for m in DEFAULT_FREE_MODELS],
                    ):
                        with mock.patch.object(OpenRouterClient, "_post_chat_completion", side_effect=fake_post):
                            buf_json = io.StringIO()
                            with redirect_stdout(buf_json):
                                rc_json = cli_main(["ask", "--dir", str(proj), "--json", "what", "is", "the", "version?"])
                            self.assertEqual(rc_json, 0)
                            payload = json.loads(buf_json.getvalue())
                            self.assertEqual(payload["answer"], "cue plugin version is 0.0.1")
                            self.assertEqual(payload["suggestions"][0]["cmd"], "cue --version")

                            buf_tsv = io.StringIO()
                            with redirect_stdout(buf_tsv):
                                rc_tsv = cli_main(["ask", "--dir", str(proj), "--tsv", "what", "is", "the", "version?"])
                            self.assertEqual(rc_tsv, 0)
                            lines = buf_tsv.getvalue().splitlines()
                            self.assertTrue(lines[0].startswith("answer\tcue plugin version is 0.0.1\t"))
                            self.assertEqual(lines[1], "ai\tcue --version\tShow installed cue version")

    def test_uninstall_removes_rc_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            zshrc = fake_home / ".zshrc"
            zshrc.write_text('export PATH="$PATH"\n# cue - Smart AI Zsh autocomplete\nsource "/tmp/cue.plugin.zsh"\n', encoding="utf-8")
            cue_dir = fake_home / ".cue"
            cue_dir.mkdir()
            (cue_dir / ".cue_home").write_text("cue\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"ZDOTDIR": str(fake_home), "CUE_HOME": str(cue_dir)}):
                res = uninstall_cue_plugin(purge_data=True, home_dir=fake_home)
            self.assertIn(str(zshrc), res["removed_rc"])
            self.assertNotIn("cue.plugin.zsh", zshrc.read_text(encoding="utf-8"))
            self.assertFalse(cue_dir.exists())

    def test_modern_card_box_has_exact_aligned_borders(self) -> None:
        theme = Theme(enabled=True)
        card = draw_card(
            theme,
            theme.cyan("✦ cue") + " · Settings",
            theme.dim("v0.0.1"),
            [f"  {theme.bold('Prompt Mode')}  {theme.green('● Enabled')}", "---", "  Item 2"],
            footer="↑/↓ move · Esc exit",
            width=72,
        )
        widths = {visible_len(line) for line in card}
        self.assertEqual(len(widths), 1)
        self.assertTrue(all("│" in line or "╭" in line or "╰" in line or "├" in line for line in card))
        self.assertEqual(visible_len(pad_visible(theme.bold("abc"), 10)), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
