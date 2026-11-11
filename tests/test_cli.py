"""The standalone Git tool: real repositories, a real bare remote, a real pre-push hook."""

import os
import subprocess
import sys
import tempfile
import unittest

CLI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.repo, self.remote = os.path.join(base, "repo"), os.path.join(base, "remote.git")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(RECEIPTS_DB=os.path.join(base, "state", "receipts.db"), HOME=base,
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.env.pop("RECEIPTS_ACTOR", None)
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True, env=self.env)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True, env=self.env)
        self.git("remote", "add", "origin", self.remote)
        self.write("check.sh", "#!/bin/sh\ngrep -q ok answer.txt\n")
        self.write("answer.txt", "ok\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")

    def write(self, name, text, repo=None):
        with open(os.path.join(repo or self.repo, name), "w") as handle:
            handle.write(text)

    def git(self, *args, repo=None, ok=True):
        proc = subprocess.run(["git", "-C", repo or self.repo, *args], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout)
        return proc

    def cli(self, *args, repo=None, stdin=None):
        return subprocess.run([sys.executable, CLI, *args], cwd=repo or self.repo, env=self.env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def configure(self):
        self.assertEqual(self.cli("check", "sh", "check.sh").returncode, 0)
        self.assertEqual(self.cli("install-hook").returncode, 0)

    def push(self, *args, repo=None):
        return self.git("push", "-q", "origin", *(args or ("HEAD:refs/heads/main",)), repo=repo, ok=False)

    def test_unconfigured_repository_is_not_gated(self):
        self.assertEqual(self.cli("install-hook").returncode, 0)
        self.assertEqual(self.push().returncode, 0)

    def test_push_needs_a_pass_on_the_pushed_content(self):
        self.configure()
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("[receipts] Blocked", blocked.stdout)
        self.assertIn("no passing run", blocked.stdout)

        run = self.cli("run", "--actor", "model-x agent-1")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("PASSED", run.stdout)
        self.assertEqual(self.push().returncode, 0)
        self.assertIn("by model-x agent-1", self.cli("log").stdout)

        self.write("answer.txt", "ok, edited\n")
        self.git("commit", "-q", "-am", "edit")
        stale = self.push()
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Changed since the last passing run: answer.txt", stale.stdout)

    def test_failed_check_keeps_the_gate_shut(self):
        self.configure()
        self.write("answer.txt", "bad\n")
        self.git("commit", "-q", "-am", "break")
        run = self.cli("run")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("FAILED", run.stdout)
        blocked = self.push()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("failed (exit 1)", blocked.stdout)
        self.assertEqual(self.cli("verify").returncode, 1)

    def test_receipt_on_uncommitted_content_follows_the_commit(self):
        self.configure()
        self.write("answer.txt", "ok again\n")
        self.write("new.txt", "untracked\n")
        run = self.cli("run")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("uncommitted", run.stdout)
        self.assertNotEqual(self.push().returncode, 0)      # HEAD is still the unchecked commit
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "exactly what was checked")
        self.assertEqual(self.push().returncode, 0)

    def test_check_that_changes_content_earns_nothing(self):
        self.write("check.sh", "#!/bin/sh\necho x >> generated.txt\n")
        self.git("commit", "-q", "-am", "check writes a file")
        self.configure()
        run = self.cli("run")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("NO RECEIPT", run.stdout)
        self.assertIn("generated.txt", run.stdout)
        self.assertEqual(self.cli("verify").returncode, 1)

    def test_worktrees_share_the_check_and_receipts(self):
        self.configure()
        other = os.path.join(os.path.dirname(self.repo), "other")
        self.git("worktree", "add", "-q", "-b", "topic", other)
        self.assertNotEqual(self.push("HEAD:refs/heads/topic", repo=other).returncode, 0)
        self.assertEqual(self.cli("run", repo=other).returncode, 0)
        self.assertEqual(self.push("HEAD:refs/heads/topic", repo=other).returncode, 0)
        self.assertEqual(self.cli("verify").returncode, 0)   # same content, same receipt, in the first worktree

    def test_deleting_a_remote_branch_is_not_gated(self):
        self.configure()
        self.assertEqual(self.cli("run").returncode, 0)
        self.assertEqual(self.push().returncode, 0)
        self.write("answer.txt", "ok, unchecked\n")
        self.git("commit", "-q", "-am", "unchecked")
        self.assertEqual(self.push(":refs/heads/main").returncode, 0)

    def test_install_does_not_overwrite_a_foreign_hook(self):
        hook = os.path.join(self.repo, ".git", "hooks", "pre-push")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        self.write(os.path.join(".git", "hooks", "pre-push"), "#!/bin/sh\nexit 0\n")
        result = self.cli("install-hook")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not overwriting", result.stdout)
        with open(hook) as handle:
            self.assertEqual(handle.read(), "#!/bin/sh\nexit 0\n")
        self.assertEqual(self.cli("uninstall-hook").returncode, 1)

    def test_uninstall_and_check_off(self):
        self.configure()
        self.assertNotEqual(self.push().returncode, 0)
        self.assertEqual(self.cli("check", "--off").returncode, 0)
        self.assertEqual(self.push().returncode, 0)
        self.assertEqual(self.cli("uninstall-hook").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "hooks", "pre-push")))


if __name__ == "__main__":
    unittest.main()
