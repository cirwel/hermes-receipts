"""Exercise Hermes discovery, hook dispatch, multiplex scopes, and local terminal.

Run with a PM-installed Hermes Python. No LLM, credentials, or publication.
This is an API integration test, not a complete gateway or agent turn.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parent,
                        help="Receipts source directory; defaults to this script directory")
    parser.add_argument("--expect", choices=("inactive", "block"), default="block",
                        help="Expected multiplex behavior; inactive reproduces the pre-fix release")
    options = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="receipts-multiplex-") as temporary:
        base = Path(temporary).resolve()
        home, repo = base / "home", base / "repo"
        plugin = home / "plugins/receipts"
        plugin.mkdir(parents=True)
        repo.mkdir()
        for filename in ("__init__.py", "shell.py", "gitstate.py", "store.py", "plugin.yaml"):
            shutil.copy2(options.source / filename, plugin / filename)
        (home / "config.yaml").write_text(
            "terminal:\n  env: local\nplugins:\n  enabled: [receipts]\n  isolation: in_process\n")
        os.environ.update(HERMES_HOME=str(home), TERMINAL_ENV="local", TERMINAL_CWD=str(repo),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                          GIT_AUTHOR_NAME="Receipts demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
                          GIT_COMMITTER_NAME="Receipts demo", GIT_COMMITTER_EMAIL="demo@example.invalid")

        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                            "-C", str(repo), *args], check=True)

        git("init", "-q", "-b", "main")
        (repo / "check.py").write_text('print("passed")\n')
        git("add", ".")
        git("commit", "-qm", "fixture")

        import hermes_cli
        sys.path.insert(0, str(Path(hermes_cli.__file__).resolve().parent.parent))
        from agent.runtime_cwd import set_session_cwd, reset_session_cwd
        from agent.secret_scope import (
            is_multiplex_active, reset_multiplex_context, reset_secret_scope,
            set_multiplex_active, set_multiplex_context, set_secret_scope,
        )
        from hermes_cli.plugins import get_plugin_command_handler, get_pre_tool_call_directive
        from tools.terminal_scope import (
            build_profile_terminal_scope, set_terminal_scope, reset_terminal_scope,
        )
        from tools.terminal_tool import terminal_tool, cleanup_all_environments

        cwd_token = set_session_cwd(str(repo))
        secret_token = terminal_token = None
        try:
            handler = get_plugin_command_handler("receipts")
            assert handler is not None, "Hermes did not discover receipts"
            print("CONFIG:", handler("check python3 check.py"))
            if options.expect == "block":
                for check in ("python3 check.py && echo success", "coproc false"):
                    rejected = handler("check " + check)
                    print("INVALID CHECK:", rejected)
                    assert "cannot earn receipts" in rejected, rejected
                    status = handler("status")
                    assert "`python3 check.py`" in status and check not in status, status

            args = {"command": "git push origin main", "workdir": str(repo)}
            before = get_pre_tool_call_directive(
                "terminal", args, session_id="integration", tool_call_id="before")
            print("SINGLE PROFILE:", before)
            assert before[0] == "block" and "no passing run" in before[1], before

            def check_multiplex(label):
                assert is_multiplex_active()
                print(f"{label} STATUS:", handler("status"))
                directive = get_pre_tool_call_directive(
                    "terminal", args, session_id="integration", tool_call_id=label)
                print(f"{label} PUSH DIRECTIVE:", directive)
                expected = None if options.expect == "inactive" else "block"
                assert directive[0] == expected, directive
                if expected == "block":
                    assert "inactive" in directive[1], directive
                ordinary_args = {"command": "printf ordinary", "workdir": str(repo)}
                ordinary = get_pre_tool_call_directive(
                    "terminal", ordinary_args, session_id="integration", tool_call_id=label + "-ordinary")
                print("ORDINARY COMMAND DIRECTIVE:", ordinary)
                assert ordinary == (None, None), ordinary
                result = terminal_tool(**ordinary_args, session_id="integration", timeout=30)
                data = json.loads(result)
                print("ORDINARY TERMINAL RESULT:", data)
                assert data.get("exit_code") == 0 and "ordinary" in data.get("output", ""), data

            secret_token = set_secret_scope({}, profile_home=str(home))
            terminal_token = set_terminal_scope(build_profile_terminal_scope(home))
            token = set_multiplex_context(True)
            try:
                check_multiplex("MULTIPLEX CONTEXT")
            finally:
                reset_multiplex_context(token)
            set_multiplex_active(True)
            try:
                check_multiplex("MULTIPLEX PROCESS")
            finally:
                set_multiplex_active(False)
        finally:
            cleanup_all_environments()
            if terminal_token is not None:
                reset_terminal_scope(terminal_token)
            if secret_token is not None:
                reset_secret_scope(secret_token)
            reset_session_cwd(cwd_token)
        print(f"PASS: real discovery and dispatch verified multiplex behavior={options.expect}; "
              "no pushes executed.")


if __name__ == "__main__":
    main()
