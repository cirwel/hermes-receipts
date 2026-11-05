"""Behavior checks using real repositories, indexes and terminal exit codes."""
import importlib.util
import json
import os
from pathlib import Path
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
        self.enterContext(patch.dict(os.environ, {"TERMINAL_ENV": "local"}))

    def git(self, *args, env=None):
        return subprocess.check_output(["git", "-c", "core.hooksPath=" + os.devnull,
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


class ShellCase(unittest.TestCase):
    def test_plain_named_push(self):
        a, = shell.guarded_actions("git -C /repo push origin branch:target", "/base")
        self.assertEqual((a.cwd, a.refs), ("/repo", ["branch"]))

    def test_check_no_masked_exit(self):
        for command in ("pytest | tee log", "pytest; true", "pytest || true", "pytest &", "pytest > log"):
            self.assertIsNone(shell.check_invocation(command, "pytest", "/repo"))
        self.assertEqual(shell.check_invocation("cd /repo && pytest", "pytest", "/base"), "/repo")
        self.assertIsNone(shell.check_invocation('echo "$CHECK"', "echo '$CHECK'", "/repo"))

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
