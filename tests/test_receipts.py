"""Behavior checks using real repositories, indexes and terminal exit codes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("receipts", SOURCE / "__init__.py",
                                            submodule_search_locations=[str(SOURCE)])
receipts = importlib.util.module_from_spec(spec)
sys.modules["receipts"] = receipts
spec.loader.exec_module(receipts)
from receipts import gitstate, shell
from receipts.store import Store

COPROCESS_CHECKS = (
    "coproc false", "time coproc false", "time -p coproc false",
    "(coproc false)", "coproc bash -c false", "! ! coproc false",
    "time ! ! coproc false", "time -- coproc false",
    "time -p -- coproc false", "( time coproc false )",
)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "-q", "-b", "main")
        self.write("src/one.txt", "one\n")
        self.write("top.txt", "top\n")
        self.commit()
        self.store = Store(Path(self.tmp.name) / "receipts.db")
        self.service = receipts.Receipts(self.store)
        self.addCleanup(receipts._pending.clear)
        environment = patch.dict(os.environ, {"TERMINAL_ENV": "local"})
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, *args, env=None):
        # A fixture commit must not leave detached maintenance racing the
        # read-only fingerprint assertions on newer Git versions.
        return subprocess.check_output(["git", "-c", "core.hooksPath=" + os.devnull,
                                        "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                                        "-C", str(self.root), *args], env=env or self.env,
                                       stderr=subprocess.PIPE).decode().strip()

    def write(self, name, data):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")

    def fp(self):
        return gitstate.fingerprint(str(self.root), time.monotonic() + 10)

    def assert_tree(self):
        index = self.root / ".git/index"
        before = index.read_bytes()
        objects = sorted(str(p.relative_to(self.root)) for p in (self.root / ".git/objects").rglob("*"))
        fp = self.fp()
        self.assertEqual(index.read_bytes(), before, "fingerprint changed real index")
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in (self.root / ".git/objects").rglob("*")), objects)
        env = dict(self.env, GIT_INDEX_FILE=str(Path(self.tmp.name) / "comparison-index"))
        Path(env["GIT_INDEX_FILE"]).write_bytes(before)
        self.git("add", "-A", env=env)
        self.assertEqual(fp.tree, self.git("write-tree", env=env))
        self.assertEqual(index.read_bytes(), before)
        return fp

    def pre(self, command, call_id="call", **extra):
        return self.service.pre_tool_call("terminal", {"command": command, "workdir": str(self.root), **extra}, call_id)

    def run_check(self, code=0, command="python3 -m unittest"):
        self.store.set_check(str(self.root), command)
        self.assertIsNone(self.pre(command))
        self.service.post_tool_call("terminal", "call", json.dumps({"exit_code": code, "output": "test result"}))

    # ── provenance ─────────────────────────────────────────────────────────
    def checked(self, session="s1", call="c1", code=0, between=None):
        self.store.set_check(str(self.root), "python3 -m unittest")
        args = {"command": "python3 -m unittest", "workdir": str(self.root)}
        self.assertIsNone(self.service.pre_tool_call("terminal", args, call, session_id=session))
        if between:
            between()
        self.service.post_tool_call("terminal", call, json.dumps({"exit_code": code, "output": "ok"}),
                                    session_id=session)
        return self.store.recent(str(self.root))[0]

    def test_receipt_records_the_model_driving_the_session_and_the_issuer(self):
        self.assertIsNone(self.service.pre_llm_call(session_id="s1", model="hermes-test-model"))
        self.assertIsNone(self.service.pre_llm_call(session_id="other", model="someone-else"))
        row = self.checked()
        self.assertEqual((row["status"], row["model"], row["session_id"]), ("passed", "hermes-test-model", "s1"))
        self.assertEqual(row["issuer"], self.store.issuer_id())
        self.assertEqual(Store(self.store.path).issuer_id(), row["issuer"])      # minted once, then stable
        self.assertEqual(len(row["issuer"]), 36)

    def test_model_is_the_one_that_started_the_check(self):
        self.service.pre_llm_call(session_id="s1", model="model-a")
        row = self.checked(between=lambda: self.service.pre_llm_call(session_id="s1", model="model-b"))
        self.assertEqual(row["model"], "model-a")

    def test_unknown_model_is_recorded_as_empty_never_guessed(self):
        for bad in (None, 7, "", "   "):
            self.assertIsNone(self.service.pre_llm_call(session_id="s1", model=bad))
        self.assertEqual(self.checked()["model"], "")
        with patch.object(receipts, "_inactive_reason", return_value=receipts._MULTIPLEX_UNSUPPORTED):
            self.service.pre_llm_call(session_id="s2", model="multiplexed-model")
        self.assertEqual(self.service._models, {})

    def test_tracked_sessions_are_bounded(self):
        for i in range(receipts.MAX_TRACKED_SESSIONS + 20):
            self.service.pre_llm_call(session_id=f"s{i}", model="m")
        self.assertEqual(len(self.service._models), receipts.MAX_TRACKED_SESSIONS)
        self.assertIn(f"s{receipts.MAX_TRACKED_SESSIONS + 19}", self.service._models)

    def test_show_and_status_name_who_checked(self):
        self.service.pre_llm_call(session_id="s1", model="hermes-test-model")
        row = self.checked()
        with patch.object(receipts, "_agent_cwd", return_value=str(self.root)):
            shown = self.service.command("show")
            self.assertIn(f"Receipt #{row['id']} — passed", shown)
            self.assertIn("model:    hermes-test-model", shown)
            self.assertIn("issuer:   " + row["issuer"], shown)
            self.assertIn("tree " + self.git("rev-parse", "HEAD^{tree}"), shown)
            self.assertIn("Not a signature", shown)
            self.assertEqual(self.service.command(f"show #{row['id']}"), shown)
            self.assertIn("no receipt #999", self.service.command("show 999"))
            self.assertIn("checked by: model hermes-test-model · issuer " + row["issuer"][:8], self.service.command())
            self.assertIn("model hermes-test-model", self.service.command("log"))

    def test_ledger_from_0_1_gains_the_new_columns_in_place(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
            root TEXT NOT NULL, command TEXT NOT NULL, tree TEXT NOT NULL, head_tree TEXT NOT NULL,
            overlay_json TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER,
            output_sha256 TEXT NOT NULL, output_chars INTEGER NOT NULL, session_id TEXT NOT NULL)""")
        conn.execute("INSERT INTO receipts (created_at, root, command, tree, head_tree, overlay_json, status,"
                     " exit_code, duration_ms, output_sha256, output_chars, session_id)"
                     " VALUES (1, 'r', 'c', 't', 't', '{}', 'passed', 0, 1, 'h', 1, '')")
        conn.commit()
        conn.close()
        store = Store(path)
        self.assertEqual(store.latest_for_tree("r", "t", "c")["model"], "")
        store.add(root="r", command="c", tree="t2", head_tree="t2", overlay={}, status="passed", exit_code=0,
                  duration_ms=1, output_sha256="h", output_chars=1, session_id="s", model="m", issuer="i")
        self.assertEqual(store.latest_for_tree("r", "t2", "c")["model"], "m")

    def test_commit_resolution(self):
        self.assertEqual(gitstate.commit_tree(str(self.root), "HEAD", time.monotonic() + 5),
                         (self.git("rev-parse", "HEAD"), self.git("rev-parse", "HEAD^{tree}")))

    def test_clean_and_modified_nested(self):
        self.assertTrue(self.assert_tree().clean)
        self.write("src/one.txt", "changed\n")
        self.assertFalse(self.assert_tree().clean)

    def test_untracked_nested_ignored_and_deleted(self):
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/no.txt", "ignored")
        self.write("src/deep/new.txt", "new")
        (self.root / "top.txt").unlink()
        self.assert_tree()

    def test_exec_and_symlink(self):
        (self.root / "top.txt").chmod(0o755)
        (self.root / "link").symlink_to("src/one.txt")
        self.assert_tree()

    def test_symlink_placeholder_uses_git_mode_and_text_conversion(self):
        link = self.root / "link.txt"
        link.symlink_to("src/one.txt")
        self.commit()
        self.git("config", "core.symlinks", "false")
        link.unlink()
        link.write_bytes(b"new-target\r\n")
        self.write(".gitattributes", "*.txt text eol=lf\n")
        self.assert_tree()

    def test_cd_from_outside_repository_records_receipt(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre(f"cd {self.root} && python3 -m unittest", workdir=str(self.root.parent))
        self.service.post_tool_call("terminal", "call", {"exit_code": 0})
        self.assertEqual(self.store.recent(str(self.root))[0]["status"], "passed")

    def test_joined_git_directory_cannot_skip_gate(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.assertEqual(self.pre(f"git -C{self.root} push origin main", workdir=str(self.root.parent))["action"], "block")

    def test_names_spaces_newlines_quotes(self):
        for name in ('space name', 'line\nname', '"quoted"', 'é.txt'):
            self.write(name, name)
        self.assert_tree()

    def test_file_directory_replacement_both_directions(self):
        (self.root / "top.txt").unlink()
        self.write("top.txt/child", "child")
        (self.root / "src/one.txt").unlink()
        (self.root / "src").rmdir()
        self.write("src", "now a file")
        self.assert_tree()

    def test_staged_then_modified_and_reverted(self):
        self.write("top.txt", "staged")
        self.git("add", "top.txt")
        self.write("top.txt", "top\n")
        self.assert_tree()

    def test_crlf_conversion(self):
        self.write(".gitattributes", "*.txt text eol=lf\n")
        (self.root / "top.txt").write_bytes(b"new\r\nline\r\n")
        self.assert_tree()

    def test_sha256_and_unborn_repository(self):
        self.root = Path(self.tmp.name).resolve() / "sha256"
        self.root.mkdir()
        self.git("init", "-q", "--object-format=sha256", "-b", "main")
        self.write("first.txt", "first")
        before = self.fp()
        self.commit()
        self.assertEqual(before.tree, self.git("rev-parse", "HEAD^{tree}"))
        self.write("dir/next.txt", "next")
        self.assert_tree()

    def test_external_filter_refused_without_execution(self):
        marker = Path(self.tmp.name) / "filter-ran"
        self.git("config", "filter.evil.clean", "touch " + str(marker))
        self.write(".gitattributes", "*.txt filter=evil\n")
        with self.assertRaisesRegex(gitstate.GitUnavailable, "external Git"):
            self.fp()
        self.assertFalse(marker.exists())
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre("python3 -m unittest")
        self.service.post_tool_call("terminal", "call", {"exit_code": 0})
        self.assertIn("could not be recorded", self.pre("git push origin main")["message"])

    def test_unused_global_style_filter_does_not_disable_normal_repo(self):
        self.git("config", "filter.unused.clean", "false")
        self.write("top.txt", "ordinary content")
        self.assert_tree()

    def test_hidden_index_entries_refused(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            self.git("update-index", flag, "top.txt")
            with self.assertRaisesRegex(gitstate.GitUnavailable, "index entries"):
                self.fp()
            self.git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "top.txt")

    def test_staged_removal_then_ignore(self):
        self.git("rm", "--cached", "top.txt")
        self.write(".gitignore", "top.txt\n")
        self.assert_tree()

    def test_index_exec_mode_with_filemode_disabled(self):
        self.git("config", "core.fileMode", "false")
        self.git("update-index", "--chmod=+x", "top.txt")
        self.assert_tree()

    def test_subdirectory_check_does_not_certify_root(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre("cd src && python3 -m unittest")
        self.service.post_tool_call("terminal", "call", {"exit_code": 0})
        self.assertEqual(self.store.recent(str(self.root)), [])

    def test_unchecked_named_branch_blocks_even_when_head_passed(self):
        self.run_check()
        self.git("branch", "checked")
        self.write("top.txt", "unchecked")
        self.commit()
        self.git("branch", "unchecked")
        self.git("checkout", "-q", "checked")
        self.assertEqual(self.pre("git push origin unchecked")["action"], "block")
        self.assertIsNone(self.pre("git push origin checked"))

    def test_push_follow_tags_refused(self):
        self.run_check()
        self.git("config", "push.followTags", "true")
        self.assertIn("followTags", self.pre("git push origin main")["message"])

    def test_gate_follows_checked_tree_across_commit(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.assertEqual(self.pre("git push origin main")["action"], "block")
        self.write("top.txt", "checked content")
        self.run_check()
        self.commit()
        self.assertIsNone(self.pre("git push origin main"))
        self.write("top.txt", "later edit")
        self.commit()
        blocked = self.pre("git push origin main")
        self.assertIn("top.txt", blocked["message"])
        self.assertEqual(blocked["action"], "block")

    def test_failed_run_supersedes_pass(self):
        self.run_check()
        self.assertIsNone(self.pre("git push origin main"))
        self.run_check(1)
        self.assertEqual(self.pre("git push origin main")["action"], "block")

    def test_changed_during_run_is_not_pass(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre("python3 -m unittest")
        self.write("top.txt", "during")
        self.service.post_tool_call("terminal", "call", {"exit_code": 0})
        self.assertEqual(self.store.recent(str(self.root))[0]["status"], "changed")
        self.commit()
        self.assertEqual(self.pre("git push origin main")["action"], "block")

    def test_no_receipt_for_background_or_missing_exit(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre("python3 -m unittest", background=True)
        self.service.post_tool_call("terminal", "call", {"exit_code": 0})
        self.assertEqual(self.store.recent(str(self.root)), [])
        self.pre("python3 -m unittest")
        self.service.post_tool_call("terminal", "call", {"output": "success-looking text"})
        self.assertEqual(self.store.recent(str(self.root))[0]["status"], "unknown")

    def test_unconfigured_and_remote_backend_inactive(self):
        self.assertIsNone(self.pre("git push origin main"))
        self.store.set_check(str(self.root), "false")
        with patch.dict(os.environ, {"TERMINAL_ENV": "docker"}):
            self.assertIsNone(self.pre("git push origin main"))

    def test_unsupported_check_configuration_preserves_previous_setting(self):
        unsupported = (
            "python3 -m unittest && echo done", "python3 -m unittest; true",
            "python3 -m unittest || true", "python3 -m unittest | tee log",
            "python3 -m unittest > log", "python3 -m unittest &",
            "python3 -m unittest\necho done", "cd . && python3 -m unittest",
            "python3 -m unittest &&", "echo $(date)", "echo `date`", "echo '",
            "x" * (shell.MAX_COMMAND_CHARS + 1),
        ) + COPROCESS_CHECKS
        with patch.object(receipts, "_agent_cwd", return_value=str(self.root)):
            for previous in (None, "python3 -m unittest"):
                for command in unsupported:
                    with self.subTest(previous=previous, command=command[:80]):
                        self.store.set_check(str(self.root), previous)
                        message = self.service.command("check " + command)
                        self.assertIn("cannot earn receipts", message)
                        self.assertIn("script", message)
                        self.assertEqual(self.store.check_for(str(self.root)), previous)

    def test_supported_check_configuration_records_and_can_be_disabled(self):
        with patch.object(receipts, "_agent_cwd", return_value=str(self.root)):
            message = self.service.command("check python3 -m unittest")
            self.assertIn("is now", message)
            self.assertIsNone(self.pre("python3 -m unittest"))
            self.service.post_tool_call("terminal", "call", {"exit_code": 0})
            self.assertEqual(self.store.recent(str(self.root))[0]["status"], "passed")
            self.assertIsNone(self.pre("git push origin main"))
            self.assertIn("removed", self.service.command("check --off"))
            self.assertIsNone(self.store.check_for(str(self.root)))

    @unittest.skipUnless(shutil.which("bash"), "requires Bash coprocess support")
    def test_coprocess_exit_zero_never_earns_a_passing_receipt(self):
        # Older settings can contain these commands. Bash returns zero for
        # starting a coprocess even though the actual check fails asynchronously.
        if subprocess.run(["bash", "-c", "help coproc"], capture_output=True, timeout=5).returncode:
            self.skipTest("requires Bash with coprocess support (4.0+)")
        for command in COPROCESS_CHECKS:
            with self.subTest(command=command):
                self.store.set_check(str(self.root), command)
                self.assertIsNone(self.pre(command))
                result = subprocess.run(["bash", "-c", command], cwd=self.root,
                                        env=self.env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.service.post_tool_call("terminal", "call", {"exit_code": result.returncode})
                self.assertEqual(self.store.recent(str(self.root)), [])
                self.assertEqual(self.pre("git push origin main")["action"], "block")

    def test_legacy_unsupported_check_explains_repair_in_status_and_gate(self):
        self.store.set_check(str(self.root), "python3 -m unittest && echo done")
        with patch.object(receipts, "_agent_cwd", return_value=str(self.root)):
            self.assertIn("cannot earn receipts", self.service.command())
            self.assertIn("/receipts check", self.service.command())
        blocked = self.pre("git push origin main")
        self.assertEqual(blocked["action"], "block")
        self.assertIn("cannot earn receipts", blocked["message"])
        self.assertIn("/receipts check", blocked["message"])

    def test_multiplex_publish_block_is_explicit_without_reading_repo_state(self):
        with patch.object(receipts, "_inactive_reason", return_value="multiplexed profiles are not supported in this release"), \
                patch.object(receipts, "_agent_cwd", side_effect=AssertionError("read cwd")), \
                patch.object(self.store, "check_for", side_effect=AssertionError("read settings")):
            for command in ("git push origin main", "gh pr create", "bash -c 'git push origin main'"):
                with self.subTest(command=command):
                    blocked = self.pre(command)
                    self.assertEqual(blocked["action"], "block")
                    self.assertIn("multiplexed profiles", blocked["message"])
                    self.assertIn("single-profile Hermes CLI", blocked["message"])
            self.assertIsNone(self.pre("python3 -m unittest"))
            self.assertIn("publishing commands are blocked", self.service.command())

    def test_becoming_inactive_discards_pending_check_without_recording(self):
        self.store.set_check(str(self.root), "python3 -m unittest")
        self.pre("python3 -m unittest")
        with patch.object(receipts, "_inactive_reason", return_value="multiplexed profiles are not supported in this release"):
            self.service.post_tool_call("terminal", "call", {"exit_code": 0, "cwd": str(self.root)})
        self.assertEqual(self.store.recent(str(self.root)), [])
        self.assertEqual(self.service._terminal_cwds, {})
        self.assertEqual(receipts._pending, {})


class ShellCase(unittest.TestCase):
    def test_plain_named_push(self):
        a, = shell.guarded_actions("git -C /repo push origin branch:target", "/base")
        self.assertEqual((a.cwd, a.refs), ("/repo", ["branch"]))

    def test_check_no_masked_exit(self):
        for command in ("pytest | tee log", "pytest; true", "pytest || true", "pytest &", "pytest > log"):
            self.assertIsNone(shell.check_invocation(command, "pytest", "/repo"))
        self.assertEqual(shell.check_invocation("cd /repo && pytest", "pytest", "/base"), "/repo")
        self.assertIsNone(shell.check_invocation('echo "$CHECK"', "echo '$CHECK'", "/repo"))

    def test_multiline_configured_check_never_records_masked_success(self):
        check = "false\ntrue"
        self.assertIsNone(shell.check_invocation(check, check, "/repo"))

    def test_control_constructs_are_not_foreground_check_commands(self):
        for check in COPROCESS_CHECKS + ("! false", "time ! false"):
            with self.subTest(check=check):
                self.assertIsNone(shell.check_invocation(check, check, "/repo"))
                self.assertIsNone(shell.check_invocation("cd /repo && " + check, check, "/base"))
        for check in ("echo coproc", 'python3 -c "print(\'coproc\')"', "time python3 -m unittest"):
            with self.subTest(check=check):
                self.assertEqual(shell.check_invocation(check, check, "/repo"), "/repo")

    def test_dynamic_home_not_static(self):
        self.assertFalse(shell._static("~/$TARGET"))

    def test_ambiguous_pushes_are_refused(self):
        for command in ("git push", "git push origin", "git push --all", "git push --follow-tags origin main",
                        "git push origin 'refs/heads/*:refs/heads/*'", "git commit -am changed && git push origin main",
                        "git -c push.followTags=true push origin main", "GIT_DIR=/other git push origin main",
                        "gh pr create --repo other/repo"):
            with self.subTest(command=command):
                actions = shell.guarded_actions(command, "/repo")
                self.assertTrue(actions)
                self.assertTrue(any(a.unverifiable or a.kind == "opaque" for a in actions))

    def test_wrapped_and_directory_pushes(self):
        for command in ("command git push origin main", "bash -c 'git push origin main'", "cd /repo && git push origin main"):
            with self.subTest(command=command):
                a, = shell.guarded_actions(command, "/repo")
                self.assertEqual(a.refs, ["main"])
                self.assertFalse(a.unverifiable)


if __name__ == "__main__":
    unittest.main()
