"""receipts: a push gate backed by verification receipts bound to repository content.

pre_tool_call (terminal tool only):
  * `git push` / `gh pr create` in a repository with a configured check is blocked unless the
    tree of each pushed commit has a passing receipt for that check;
  * a terminal run of exactly the configured check gets a "before" fingerprint;
  * `hermes receipts check|clear` from the agent is refused (configuration is the user's).
post_tool_call: records the receipt (passed / failed / changed-during-run), with the model that was
  driving the session (observed in pre_llm_call) and this ledger's issuer id.
Returns only `block` or nothing; never `approve`. Inactive on non-local terminal backends.
Multiplexed profiles cannot earn receipts; recognized publishing commands are explicitly blocked.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

if __package__:
    from . import gitstate, shell
    from .store import Store, field

BUDGET_S = 8.0          # well under the 30 s fail-closed hook deadline
_MULTIPLEX_UNSUPPORTED = "multiplexed profiles are not supported in this release"
_pending: dict[str, tuple[str, str, object, float, str]] = {}   # call id -> (root, cmd, fp, t0, model)
MAX_TRACKED_SESSIONS = 256


def _check_error(command: str, root: str) -> str | None:
    # Use the recording recognizer, so setup cannot accept a check that the
    # hooks will never recognize. Do not attempt to execute or rewrite it.
    if shell.check_invocation(command, command, root) == root:
        return None
    return ("this check cannot earn receipts. Set one foreground command to run in the repository root "
            "with `/receipts check <command>`. Pipes, redirects, chains, background jobs (including coproc), "
            "shell negation and unsupported "
            "shell syntax are not accepted. Put multi-step checks in a script that returns a nonzero "
            "exit code on failure, then configure that script as the check.")


def _agent_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except ImportError:
        return os.path.abspath(os.environ.get("TERMINAL_CWD") or os.getcwd())


def _inactive_reason() -> str:
    try:
        from agent.secret_scope import is_multiplex_active
        if is_multiplex_active():
            return _MULTIPLEX_UNSUPPORTED
        from hermes_cli.config import load_config
        config = load_config() or {}
        if config.get("plugins", {}).get("isolation") == "host":
            return "the separate plugin host is not supported in this release"
        backend = config.get("terminal", {}).get("env") or os.environ.get("TERMINAL_ENV") or "local"
    except ImportError:
        backend = os.environ.get("TERMINAL_ENV") or "local"
    backend = backend.strip().lower()
    return "" if backend == "local" else f"terminal backend is `{backend}`; receipts supports `local` only"


def _inactive_message(reason: str) -> str:
    message = f"receipts is inactive: {reason}."
    if reason == _MULTIPLEX_UNSUPPORTED:
        message += (" Recognized publishing commands are blocked while this plugin is enabled, even in "
                    "unconfigured repositories. Use a standalone, single-profile Hermes CLI session with "
                    "the local terminal and in-process plugins; run /receipts there before retrying.")
    return message


class Receipts:
    def __init__(self, store: "Store | None" = None):
        self.store = store or Store()
        self._unrecorded: dict[str, str] = {}
        self._terminal_cwds: dict[str, str] = {}
        self._models: dict[str, str] = {}     # session id -> model driving it (from pre_llm_call)

    # ── provenance ─────────────────────────────────────────────────────────
    def pre_llm_call(self, session_id: str = "", model: str = "", **_):
        """Remember which model is driving a session. Observes only; never returns a directive."""
        try:
            if isinstance(model, str) and model.strip() and isinstance(session_id, str) and not _inactive_reason():
                self._models.pop(session_id or "", None)
                self._models[session_id or ""] = " ".join(model.split())[:120]
                while len(self._models) > MAX_TRACKED_SESSIONS:
                    self._models.pop(next(iter(self._models)))
        except Exception:
            pass
        return None

    # ── gate ───────────────────────────────────────────────────────────────
    def pre_tool_call(self, tool_name: str = "", args: dict | None = None, tool_call_id: str = "",
                      task_id: str = "", session_id: str = "", **_):
        if tool_name != "terminal":
            return None
        args = args if isinstance(args, dict) else {}
        command = args.get("command") or ""
        if not isinstance(command, str) or not command.strip():
            return None
        if inactive := _inactive_reason():
            # Do not inspect a repository or read profile-bound state in an
            # unsupported multiplex context. A silent no-op looks like approval.
            if inactive == _MULTIPLEX_UNSUPPORTED and shell.guarded_actions(command, os.curdir):
                return {"action": "block", "message": f"[receipts] Blocked: {_inactive_message(inactive)}"}
            return None
        deadline = time.monotonic() + BUDGET_S
        base = self._terminal_cwds.get(task_id or session_id) or _agent_cwd()
        if args.get("workdir"):
            base = os.path.join(base, os.path.expanduser(args["workdir"]))
        base = os.path.realpath(base)

        if shell.is_receipts_config_change(command):
            return {"action": "block", "message": "[receipts] Configuration changes must come from the user "
                    "(the /receipts command or their own terminal), not from the agent."}

        for action in shell.guarded_actions(command, base):
            try:
                msg = self._gate(action, deadline)
            except Exception as exc:
                msg = f"[receipts] Could not verify this command: {exc}. Retry it as a plain command."
            if msg:
                return {"action": "block", "message": msg}

        if not args.get("background"):
            self._maybe_start_receipt(command, base, f"{session_id}\0{tool_call_id}" if tool_call_id else "", deadline,
                                      self._models.get(session_id or "", "") if isinstance(session_id, str) else "")
        return None

    def _gate(self, action, deadline: float) -> str | None:
        root = gitstate.repo_root(action.cwd, deadline)
        if not root:
            return None
        check = self.store.check_for(root)
        if not check:
            return None                       # unconfigured repositories are never blocked
        if error := _check_error(check, root):
            return f"[receipts] Blocked: {error}"
        if action.kind == "push" and gitstate.git(root, "config", "--bool", "push.followTags",
                                                  deadline=deadline, check=False).strip() == b"true":
            return "[receipts] Blocked: push.followTags may publish additional commits; disable it or push manually."
        what = {"push": "git push", "pr-create": "gh pr create"}.get(action.kind, "this command")
        if action.kind == "opaque":
            return (f"[receipts] Blocked: this command looks like it pushes from {root}, but receipts cannot "
                    "parse it. Run `git push` (or `gh pr create`) as a plain command.")
        if action.unverifiable:
            return f"[receipts] Blocked: {action.unverifiable}; receipts can only verify specific commits. " \
                   "Push branches one at a time, or ask the user to run it."
        for rev in action.refs:
            ct = gitstate.commit_tree(root, rev, deadline)
            if not ct:
                return f"[receipts] Blocked `{what}`: cannot resolve `{rev}` to a commit in {root}."
            commit, tree = ct
            row = self.store.latest_for_tree(root, tree, check)
            if row is not None and row["status"] == "passed":
                continue
            if row is not None:
                why = f"the last run of `{check}` on exactly this content failed (exit {row['exit_code']})."
            else:
                why = f"no passing run of `{check}` exists for this content." + self._diff_hint(root, check, tree, deadline)
                if root in self._unrecorded:
                    return (f"[receipts] Blocked `{what}`: no passing receipt for {commit[:10]}. "
                            f"The last check could not be recorded: {self._unrecorded[root]}. "
                            "Use /receipts to inspect the repository before retrying.")
            return (f"[receipts] Blocked `{what}`: commit {commit[:10]} — {why} Run `{check}` in {root} as its "
                    "own command (not piped or chained), fix any failure, commit, then retry.")
        return None

    def _diff_hint(self, root: str, check: str, tree: str, deadline: float) -> str:
        last = self.store.latest_passing(root, check)
        if last is None:
            return ""
        try:
            overlay = {p: tuple(v) if v else None for p, v in json.loads(last["overlay_json"]).items()}
            names = set(gitstate.changed_names(root, last["head_tree"], tree, deadline)) | set(overlay)
            target = gitstate.blobs_at(root, tree, sorted(names), deadline)
            base = gitstate.blobs_at(root, last["head_tree"], sorted(names), deadline)
            differ = sorted(p for p in names if (overlay[p] if p in overlay else base.get(p)) != target.get(p))
        except Exception:
            return ""
        if not differ:
            return ""
        shown = ", ".join(differ[:5]) + (f" (+{len(differ) - 5} more)" if len(differ) > 5 else "")
        return f" Changed since the last passing run: {shown}."

    # ── receipts ───────────────────────────────────────────────────────────
    def _maybe_start_receipt(self, command: str, base: str, tool_call_id: str, deadline: float,
                             model: str = "") -> None:
        if not tool_call_id:
            return
        root = None
        try:
            root = gitstate.repo_root(shell.invocation_cwd(command, base), deadline)
            check = self.store.check_for(root) if root else None
            if not check:
                return
            run_dir = shell.check_invocation(command, check, base)
            if not run_dir or os.path.realpath(run_dir) != root:
                return
            # The model is taken when the check starts: that is the one that ran it.
            _pending[tool_call_id] = (root, check, gitstate.fingerprint(root, deadline), time.time(), model)
            self._unrecorded.pop(root, None)
        except Exception as exc:
            if root:
                self._unrecorded[root] = str(exc)
            return                            # never interfere with the check itself

    def post_tool_call(self, tool_name: str = "", tool_call_id: str = "", result=None, session_id: str = "",
                       duration_ms=None, args: dict | None = None, task_id: str = "", **_):
        if _inactive_reason():
            if tool_call_id:
                _pending.pop(f"{session_id}\0{tool_call_id}", None)
            return None
        try:
            data = json.loads(result) if isinstance(result, str) else (result or {})
        except (ValueError, TypeError):
            data = {}
        if tool_name == "terminal" and isinstance(data, dict) and isinstance(data.get("cwd"), str) \
                and not (args or {}).get("workdir"):
            self._terminal_cwds[task_id or session_id] = data["cwd"]
        pending = _pending.pop(f"{session_id}\0{tool_call_id}", None) if tool_call_id else None
        if pending is None:
            return None
        root, check, before, _t0, model = pending
        try:
            exit_code = data.get("exit_code") if isinstance(data, dict) else None
            output = str(data.get("output", "")) if isinstance(data, dict) else str(result)
            after = gitstate.fingerprint(root, time.monotonic() + BUDGET_S)
            if type(exit_code) is not int:
                status = "unknown"
            elif after.tree != before.tree:
                status = "changed"            # content moved during the run: proves nothing
            else:
                status = "passed" if exit_code == 0 else "failed"
            self.store.add(root=root, command=check, tree=before.tree, head_tree=before.head_tree,
                           overlay=before.overlay, status=status, exit_code=exit_code,
                           duration_ms=int(duration_ms) if isinstance(duration_ms, (int, float)) else None,
                           output_sha256=hashlib.sha256(output.encode()).hexdigest(), output_chars=len(output),
                           session_id=session_id or "", model=model, issuer=self.store.issuer_id())
        except Exception as exc:
            self._unrecorded[root] = str(exc)
        return None

    # ── /receipts ──────────────────────────────────────────────────────────
    def command(self, raw_args: str = "", **_) -> str:
        words = raw_args.split(None, 1)
        sub = words[0] if words else "status"
        rest = words[1].strip() if len(words) > 1 else ""
        if inactive := _inactive_reason():
            return _inactive_message(inactive)
        deadline = time.monotonic() + BUDGET_S
        root = gitstate.repo_root(_agent_cwd(), deadline)
        if not root:
            return "receipts: the current directory is not inside a git repository."
        if sub == "check":
            if not rest:
                return "usage: /receipts check <command>   (or: /receipts check --off)"
            if rest != "--off" and (error := _check_error(rest, root)):
                return f"receipts: {error} The configured check was not changed."
            self.store.set_check(root, None if rest == "--off" else rest)
            if rest == "--off":
                return f"receipts: removed the check for {root}."
            return f"receipts: check for {root} is now `{rest}`; `git push` and `gh pr create` need a passing run."
        if sub == "clear":
            return f"receipts: cleared {self.store.clear(root)} receipts for {root}."
        if sub == "log":
            rows = self.store.recent(root)
            return "\n".join(f"#{r['id']}  {time.strftime('%H:%M:%S', time.localtime(r['created_at']))}  "
                             f"{r['status']:<8} exit={r['exit_code']}  tree {r['tree'][:10]}  `{r['command']}`"
                             f"{'  model ' + field(r, 'model') if field(r, 'model') else ''}" for r in rows) \
                or "receipts: no receipts yet."
        if sub == "show":
            return self._show(root, rest, deadline)
        check = self.store.check_for(root)
        if not check:
            return f"receipts: no check configured for {root}. Set one with /receipts check <command>."
        if error := _check_error(check, root):
            return f"receipts: {error}"
        try:
            fp = gitstate.fingerprint(root, deadline)
        except gitstate.GitUnavailable as exc:
            return f"receipts: cannot fingerprint this repository: {exc}."
        row = self.store.latest_for_tree(root, fp.tree, check)
        state = "PASSED" if row is not None and row["status"] == "passed" else (
            "FAILED" if row is not None else "NO RECEIPT")
        warning = f"\n  last recording error: {self._unrecorded[root]}" if root in self._unrecorded else ""
        who = ""
        if row is not None:
            who = f"\n  checked by: model {field(row, 'model') or 'not recorded'} · issuer " \
                  f"{field(row, 'issuer')[:8] or 'not recorded'}" \
                  f" (receipt #{row['id']}; /receipts show)"
        return (f"receipts — {root}\n  check: `{check}`\n  current content: tree {fp.tree[:10]}"
                f"{' (uncommitted changes)' if not fp.clean else ''}\n  receipt: {state}{who}{warning}")

    def _show(self, root: str, which: str, deadline: float) -> str:
        """One receipt in full, as text that can be pasted into a pull request."""
        rows = self.store.recent(root, 500)
        if which:
            wanted = which.lstrip("#")
            rows = [r for r in rows if str(r["id"]) == wanted]
            if not rows:
                return f"receipts: no receipt #{wanted} for {root}."
        elif (check := self.store.check_for(root)):
            try:
                tree = gitstate.fingerprint(root, deadline).tree
            except gitstate.GitUnavailable as exc:
                return f"receipts: cannot fingerprint this repository: {exc}."
            current = self.store.latest_for_tree(root, tree, check)
            if current is None:
                return "receipts: no conclusive receipt for the current content. Use /receipts log to pick one."
            rows = [current]
        if not rows:
            return "receipts: no receipts yet."
        r = rows[0]
        head = gitstate.commit_tree(root, "HEAD", deadline)
        commit = f" (HEAD {head[0][:10]})" if head and head[1] == r["tree"] else ""
        took = f", {r['duration_ms'] / 1000:.1f} s" if r["duration_ms"] is not None else ""
        return (f"Receipt #{r['id']} — {r['status']}\n"
                f"  check:    `{r['command']}` (exit {r['exit_code']}{took})\n"
                f"  content:  tree {r['tree']}{commit}\n"
                f"  model:    {field(r, 'model') or 'not recorded'}\n"
                f"  issuer:   {field(r, 'issuer') or 'not recorded'}\n"
                f"  session:  {r['session_id'] or 'not recorded'}\n"
                f"  recorded: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(r['created_at']))}\n"
                f"  output:   sha256 {r['output_sha256']} ({r['output_chars']} chars)\n"
                "  Recorded locally by the receipts plugin from Hermes hook data. Not a signature.")


def register(ctx):
    service = Receipts()
    ctx.register_hook("pre_tool_call", service.pre_tool_call)
    ctx.register_hook("post_tool_call", service.post_tool_call)
    ctx.register_hook("pre_llm_call", service.pre_llm_call)
    ctx.register_command("receipts", service.command, description="Verification receipts and the push gate",
                         args_hint="[status|check <cmd>|log|show [id]|clear]")
