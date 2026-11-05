"""Run the receipts loop through an installed Hermes runtime, without an LLM.

Use the Python interpreter from your Hermes installation. All repositories,
plugin installation, configuration and receipts live in a temporary directory.
The only push target is a local bare repository.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="receipts-demo-") as temporary:
        base = Path(temporary).resolve()
        home, repo, remote = base / "home", base / "repo", base / "remote.git"
        repo.mkdir()
        plugin = home / "plugins/receipts"
        plugin.mkdir(parents=True)
        for filename in ("__init__.py", "gitstate.py", "shell.py", "store.py", "plugin.yaml"):
            shutil.copy2(Path(__file__).parent / filename, plugin / filename)
        (home / "config.yaml").write_text("terminal:\n  env: local\nplugins:\n  enabled: [receipts]\n  isolation: in_process\n")
        os.environ.update(HERMES_HOME=str(home), TERMINAL_ENV="local", TERMINAL_CWD=str(repo),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                          GIT_AUTHOR_NAME="Receipts demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
                          GIT_COMMITTER_NAME="Receipts demo", GIT_COMMITTER_EMAIL="demo@example.invalid")

        def git(*args):
            return subprocess.check_output(["git", "-c", "core.hooksPath=" + os.devnull,
                                            "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()

        git("init", "-q", "-b", "main")
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        (repo / "answer.txt").write_text("42\n")
        (repo / "check.py").write_text("from pathlib import Path\nassert Path('answer.txt').read_text().strip() == '42'\nprint('check passed')\n")
        git("add", "-A")
        git("commit", "-qm", "demo fixture")
        git("remote", "add", "origin", str(remote))

        # Real discovery, command registration, pre-hook dispatcher, terminal
        # backend, and post-hook dispatcher. No model credentials or calls.
        import hermes_cli
        # Hermes's CLI bootstrap adds its source root for top-level helpers.
        sys.path.insert(0, str(Path(hermes_cli.__file__).resolve().parent.parent))
        from hermes_cli.plugins import get_plugin_command_handler, get_pre_tool_call_directive
        from hermes_cli.lifecycle import invoke_hook
        from tools.terminal_tool import terminal_tool, cleanup_all_environments
        handler = get_plugin_command_handler("receipts")
        assert handler is not None, "Hermes did not discover receipts"
        print(handler("check python3 check.py"))
        counter = 0

        def execute(command, blocked=False, expected_exit=0):
            nonlocal counter
            counter += 1
            call_id = f"demo-{counter}"
            args = {"command": command, "workdir": str(repo)}
            directive, message = get_pre_tool_call_directive("terminal", args, tool_call_id=call_id,
                                                           session_id="receipts-demo")
            print(f"\n$ {command}")
            if directive == "block":
                print(message)
                assert blocked, "unexpected block"
                return
            assert not blocked, "expected the gate to block"
            result = terminal_tool(**args, session_id="receipts-demo", timeout=30)
            invoke_hook("post_tool_call", tool_name="terminal", args=args, tool_call_id=call_id,
                        session_id="receipts-demo", result=result)
            data = json.loads(result)
            print(data.get("output", "").strip())
            assert data.get("exit_code") == expected_exit, data

        try:
            execute("git push origin main", blocked=True)
            execute("python3 check.py")
            execute("git push origin main")
            (repo / "answer.txt").write_text("41\n")
            git("add", "-A")
            git("commit", "-qm", "change after verification")
            execute("git push origin main", blocked=True)
            execute("python3 check.py", expected_exit=1)
            execute("git push origin main", blocked=True)
            (repo / "answer.txt").write_text("42\n")
            execute("python3 check.py")
            git("add", "-A")
            git("commit", "-qm", "fix and verify before commit")
            execute("git push origin main")
            print("\n" + handler("log"))
            assert git("rev-parse", "HEAD") == git("ls-remote", "origin", "refs/heads/main").split()[0]
            print("\nPASS: untested and stale content blocked; checked content reached the local remote.")
        finally:
            cleanup_all_environments()


if __name__ == "__main__":
    main()
