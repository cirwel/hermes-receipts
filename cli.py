#!/usr/bin/env python3
"""receipts as a plain Git tool: the same content-bound receipts, without Hermes.

    receipts check <command>     set the check for this repository (--off removes it)
    receipts run                 run the check here and record a receipt for this content
    receipts status              the check, and the receipt for the current content
    receipts verify [rev ...]    exit 0 when each commit (default HEAD) has a passing receipt
    receipts log                 recent receipts
    receipts install-hook        add a pre-push hook that runs the gate on every `git push`
    receipts uninstall-hook      remove that hook

The pre-push hook is run by Git itself, so it applies to every tool that pushes
from the repository: a person, Claude Code, Codex, Hermes or a script. The check
and its receipts are shared by all worktrees of a repository. The ledger is a
local SQLite file ($RECEIPTS_DB, default ~/.local/state/receipts/receipts.db),
separate from the Hermes plugin's ledger. `git push --no-verify` skips it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
BUDGET_S = 60.0
HOOK_MARKER = "# receipts pre-push hook"


def _load(name: str):
    # Load siblings by path: this file also runs as a script and through a symlink,
    # and the directory is a Hermes plugin package whose __init__ needs Hermes.
    mod_name = "receipts_cli_" + name
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


gitstate = _load("gitstate")
Store = _load("store").Store


def db_path() -> str:
    path = os.environ.get("RECEIPTS_DB")
    if not path:
        state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        path = os.path.join(state, "receipts", "receipts.db")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


def _deadline() -> float:
    return time.monotonic() + BUDGET_S


class Repo:
    """A worktree (``root``, where git runs) and the repository it belongs to (``key``)."""

    def __init__(self, root: str, key: str):
        self.root, self.key = root, key

    @classmethod
    def at(cls, cwd: str) -> "Repo | None":
        deadline = _deadline()
        root = gitstate.repo_root(cwd, deadline)
        if not root:
            return None
        common = gitstate.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir",
                              deadline=deadline).decode().strip()
        return cls(root, os.path.realpath(common))

    @classmethod
    def for_hook(cls) -> "Repo | None":
        """The repository Git is pushing from, taken from the hook's own environment.

        Git may run the hook with GIT_DIR set and a cwd that is not a worktree (a bare
        repository, or --git-dir); the cwd alone could name no repository or the wrong one.
        """
        def ask(*args: str) -> str:
            proc = subprocess.run(["git", "rev-parse", "--path-format=absolute", *args], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=BUDGET_S)
            return proc.stdout.decode().strip() if proc.returncode == 0 else ""
        common = ask("--git-common-dir")
        if not common:
            return None
        key = os.path.realpath(common)
        top = ask("--show-toplevel")
        return cls(os.path.realpath(top) if top else key, key)


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _diff_hint(store, repo: Repo, check: str, tree: str) -> str:
    last = store.latest_passing(repo.key, check)
    if last is None:
        return ""
    try:
        deadline = _deadline()
        overlay = {p: tuple(v) if v else None for p, v in json.loads(last["overlay_json"]).items()}
        names = set(gitstate.changed_names(repo.root, last["head_tree"], tree, deadline)) | set(overlay)
        target = gitstate.blobs_at(repo.root, tree, sorted(names), deadline)
        base = gitstate.blobs_at(repo.root, last["head_tree"], sorted(names), deadline)
        differ = sorted(p for p in names if (overlay[p] if p in overlay else base.get(p)) != target.get(p))
    except Exception:
        return ""
    if not differ:
        return ""
    shown = ", ".join(differ[:5]) + (f" (+{len(differ) - 5} more)" if len(differ) > 5 else "")
    return f" Changed since the last passing run: {shown}."


def gate(store, repo: Repo, revs: list[str], what: str) -> str | None:
    """Why ``revs`` may not be published, or None when every one has a passing receipt."""
    check = store.check_for(repo.key)
    if not check:
        return None                           # unconfigured repositories are never blocked
    for rev in revs:
        ct = gitstate.commit_tree(repo.root, rev, _deadline())
        if not ct:
            return f"[receipts] Blocked {what}: cannot resolve `{rev}` to a commit in {repo.root}."
        commit, tree = ct
        row = store.latest_for_tree(repo.key, tree, check)
        if row is not None and row["status"] == "passed":
            continue
        if row is not None:
            why = f"the last run of `{check}` on exactly this content failed (exit {row['exit_code']})."
        else:
            why = f"no passing run of `{check}` exists for this content." + _diff_hint(store, repo, check, tree)
        return (f"[receipts] Blocked {what}: commit {commit[:10]} — {why} Run `receipts run` in the "
                "worktree that has this commit checked out, fix any failure, commit, then retry.")
    return None


# ── commands ───────────────────────────────────────────────────────────────
def cmd_check(store, repo: Repo, args) -> int:
    if args.off:
        store.set_check(repo.key, None)
        print(f"receipts: removed the check for {repo.key}.")
        return 0
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        check = store.check_for(repo.key)
        print(f"receipts: check is `{check}`." if check else "receipts: no check configured.")
        return 0
    command = args.command[0] if len(args.command) == 1 else shlex.join(args.command)
    if not command.strip() or "\n" in command:
        _say("receipts: the check must be one non-empty line.")
        return 2
    store.set_check(repo.key, command)
    try:
        gitstate.fingerprint(repo.root, _deadline())
    except gitstate.GitUnavailable as exc:
        _say(f"receipts: warning — this repository cannot earn receipts yet ({exc}). Pushes will be blocked "
             "until that is resolved; `receipts check --off` removes the check.")
    print(f"receipts: check for {repo.key} is now `{command}`. Record a pass with `receipts run`.")
    return 0


def cmd_run(store, repo: Repo, args) -> int:
    check = store.check_for(repo.key)
    if not check:
        _say("receipts: no check configured. Set one with `receipts check <command>`.")
        return 2
    try:
        before = gitstate.fingerprint(repo.root, _deadline())
    except gitstate.GitUnavailable as exc:
        _say(f"receipts: cannot fingerprint this repository: {exc}.")
        return 2
    started = time.time()
    digest, size = hashlib.sha256(), 0
    # The check sees the caller's environment minus Git's own variables, which would
    # point it at the wrong index or directory when this runs under a hook or alias.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.Popen(check, shell=True, cwd=repo.root, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    out = sys.stdout.buffer
    try:
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            out.write(chunk)
            out.flush()
            digest.update(chunk)
            size += len(chunk)
        code = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        _say("receipts: interrupted; no receipt recorded.")
        return 130
    try:
        after = gitstate.fingerprint(repo.root, _deadline())
    except gitstate.GitUnavailable as exc:
        _say(f"receipts: no receipt recorded; cannot fingerprint after the run: {exc}.")
        return 1
    moved = sorted(p for p in set(before.overlay) | set(after.overlay)
                   if before.overlay.get(p, "") != after.overlay.get(p, "")) if after.tree != before.tree else []
    status = "changed" if after.tree != before.tree else ("passed" if code == 0 else "failed")
    store.add(root=repo.key, command=check, tree=before.tree, head_tree=before.head_tree, overlay=before.overlay,
              status=status, exit_code=code, duration_ms=int((time.time() - started) * 1000),
              output_sha256=digest.hexdigest(), output_chars=size,
              session_id=args.actor or os.environ.get("RECEIPTS_ACTOR") or "")
    if status == "changed":
        shown = ", ".join(moved[:5]) or "the commit under it"
        _say(f"receipts: NO RECEIPT — the content changed while the check ran ({shown}). "
             "Ignore files the check writes, then run it again.")
        return code or 1
    _say(f"receipts: {status.upper()} (exit {code}) recorded for tree {before.tree[:10]}"
         f"{' (uncommitted changes; commit exactly this content)' if not before.clean else ''}.")
    return code


def cmd_status(store, repo: Repo, args) -> int:
    check = store.check_for(repo.key)
    if not check:
        print(f"receipts: no check configured for {repo.key}. Set one with `receipts check <command>`.")
        return 0
    try:
        fp = gitstate.fingerprint(repo.root, _deadline())
    except gitstate.GitUnavailable as exc:
        _say(f"receipts: cannot fingerprint this repository: {exc}.")
        return 2
    row = store.latest_for_tree(repo.key, fp.tree, check)
    state = "PASSED" if row is not None and row["status"] == "passed" else ("FAILED" if row is not None else "NO RECEIPT")
    hook = _hook_path(repo)
    installed = os.path.isfile(hook) and HOOK_MARKER in _read(hook)
    print(f"receipts — {repo.root}\n  check: `{check}`\n  current content: tree {fp.tree[:10]}"
          f"{' (uncommitted changes)' if not fp.clean else ''}\n  receipt: {state}\n"
          f"  pre-push hook: {'installed' if installed else 'not installed (receipts install-hook)'}")
    return 0


def cmd_verify(store, repo: Repo, args) -> int:
    if not store.check_for(repo.key):
        _say("receipts: no check configured; nothing to verify.")
        return 2
    message = gate(store, repo, args.revs or ["HEAD"], "verify")
    if message:
        _say(message)
        return 1
    print("receipts: passing receipt found.")
    return 0


def cmd_log(store, repo: Repo, args) -> int:
    rows = store.recent(repo.key, args.limit)
    for r in rows:
        actor = f"  by {r['session_id']}" if r["session_id"] else ""
        print(f"{time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(r['created_at']))}  {r['status']:<8}"
              f" exit={r['exit_code']}  tree {r['tree'][:10]}  `{r['command']}`{actor}")
    if not rows:
        print("receipts: no receipts yet.")
    return 0


def cmd_hook(store, repo: Repo, args) -> int:
    if args.name != "pre-push":
        _say(f"receipts: unsupported hook `{args.name}`.")
        return 2
    revs: list[str] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_sha = parts[1]
        if set(local_sha) == {"0"}:           # deleting a remote ref publishes no content
            continue
        if local_sha not in revs:
            revs.append(local_sha)
    message = gate(store, repo, revs, "`git push`")
    if message:
        _say(message)
        return 1
    return 0


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _hook_path(repo: Repo) -> str:
    # gitstate.git overrides core.hooksPath for safety; ask git directly so the
    # repository's own setting decides where the hook goes.
    proc = subprocess.run(["git", "-C", repo.root, "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=gitstate._env(), timeout=BUDGET_S)
    hooks = proc.stdout.decode().strip()
    if proc.returncode != 0 or not hooks:
        raise gitstate.GitUnavailable("cannot locate the hooks directory")
    return os.path.join(hooks, "pre-push")


def cmd_install_hook(store, repo: Repo, args) -> int:
    path = _hook_path(repo)
    cli = os.path.join(HERE, "cli.py")
    line = f'exec python3 {shlex.quote(cli)} hook pre-push "$@"'
    if os.path.exists(path) and HOOK_MARKER not in _read(path):
        _say(f"receipts: {path} already exists and is not ours; not overwriting it. Add this line to it:\n  {line}")
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n"
                     f"{HOOK_MARKER} (installed by `receipts install-hook`; remove with `receipts uninstall-hook`)\n"
                     f"if [ ! -f {shlex.quote(cli)} ]; then\n"
                     f"  echo '[receipts] Blocked `git push`: the receipts tool is missing from its installed "
                     "location. Re-run `receipts install-hook` from its checkout, or remove this hook.' >&2\n"
                     "  exit 1\nfi\n"
                     f"{line}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"receipts: installed {path}.")
    if not store.check_for(repo.key):
        print("receipts: no check is configured yet, so pushes are not gated. Set one with `receipts check <command>`.")
    return 0


def cmd_uninstall_hook(store, repo: Repo, args) -> int:
    path = _hook_path(repo)
    if not os.path.exists(path):
        print("receipts: no pre-push hook installed.")
        return 0
    if HOOK_MARKER not in _read(path):
        _say(f"receipts: {path} is not ours; leaving it alone.")
        return 1
    os.unlink(path)
    print(f"receipts: removed {path}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="receipts", description="Content-bound verification receipts for Git.")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="set, show or remove the check for this repository")
    p.add_argument("--off", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("run", help="run the check and record a receipt for the current content")
    p.add_argument("--actor", help="who ran it (free text, e.g. a model and agent id); default $RECEIPTS_ACTOR")
    sub.add_parser("status", help="the check and the receipt for the current content")
    p = sub.add_parser("verify", help="exit 0 when each commit has a passing receipt")
    p.add_argument("revs", nargs="*")
    p = sub.add_parser("log", help="recent receipts")
    p.add_argument("-n", "--limit", type=int, default=10)
    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("name")
    p.add_argument("rest", nargs="*")
    sub.add_parser("install-hook", help="install the pre-push hook in this repository")
    sub.add_parser("uninstall-hook", help="remove the pre-push hook")
    args = parser.parse_args(argv)
    handler = {"check": cmd_check, "run": cmd_run, "status": cmd_status, "verify": cmd_verify, "log": cmd_log,
               "hook": cmd_hook, "install-hook": cmd_install_hook, "uninstall-hook": cmd_uninstall_hook}.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 2
    blocking = args.cmd == "hook"
    try:
        repo = Repo.for_hook() if blocking else Repo.at(os.getcwd())
        if repo is None:
            if blocking:
                _say("[receipts] Blocked `git push`: cannot identify the repository being pushed from.")
                return 1
            _say("receipts: the current directory is not inside a git repository.")
            return 2
        return handler(Store(path=db_path()), repo, args)
    except Exception as exc:                  # a gate that cannot decide must not look like approval
        _say(f"[receipts] {'Blocked `git push`: could' if blocking else 'Could'} not complete: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
