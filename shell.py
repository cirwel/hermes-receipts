"""Bounded command recognition for the terminal tool.

This is a heuristic over a narrow surface, not a shell parser or a security
boundary: it recognises a configured check command run on its own (optionally
after ``cd <dir> &&``), and ``git push`` / ``gh pr create`` in plain,
wrapper-prefixed or ``sh -c`` form. Commands it cannot parse but that mention a
guarded action are reported as opaque so the gate can refuse them clearly.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

MAX_COMMAND_CHARS = 32768
_SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&"}
_WRAPPERS = {"env", "command", "nohup", "time", "nice", "sudo", "exec"}
_SHELLS = {"sh", "bash", "zsh", "dash"}
_OPAQUE_HINT = re.compile(r"\bgit\b[^\n;|&]*\bpush\b|\bgh\b[^\n;|&]*\bpr\b[^\n;|&]*\bcreate\b")
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option"}


@dataclass
class Action:
    kind: str                     # "push" | "pr-create" | "opaque"
    cwd: str
    refs: list[str] = field(default_factory=list)   # revisions whose trees must have receipts
    unverifiable: str = ""        # non-empty: reason the target cannot be determined
    text: str = ""


def _split(command: str) -> list[list[str]] | None:
    """Split into simple commands on shell separators. None when unparseable."""
    if len(command) > MAX_COMMAND_CHARS:
        return None
    if "`" in command or "$(" in command or "<(" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS or word == "\n":
            segments.append([])
        elif word and all(c in ";&|()" for c in word):
            segments.append([])
        else:
            segments[-1].append(word)
    return [s for s in segments if s]


def _unwrap(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if "=" in w and not w.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            continue
        if os.path.basename(w) in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            continue
        break
    return words[i:]


def _resolve(path: str, base: str) -> str:
    return os.path.realpath(os.path.join(base, os.path.expanduser(path)))


def _static(value: str) -> bool:
    return bool(value) and not any(c in (value[2:] if value.startswith("~/") else value) for c in "$`*?{}~")


def _git_parts(words: list[str], cwd: str) -> tuple[str, list[str], str]:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        i += 1
        if opt in _GIT_OPTS_WITH_VALUE and i < len(words):
            if opt == "-C" or opt == "--work-tree":
                cwd = _resolve(words[i], cwd)
            i += 1
        elif opt.startswith("--work-tree="):
            cwd = _resolve(opt.partition("=")[2], cwd)
    sub = words[i] if i < len(words) else ""
    return sub, words[i + 1:], cwd


def _push_refs(args: list[str]) -> tuple[list[str], str, bool]:
    """Return (revisions to check, unverifiable reason, skip) for ``git push`` args."""
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positional += args[i:]
            break
        if a in ("--all", "--mirror", "--branches"):
            return [], f"`git push {a}` pushes many refs at once", False
        if a == "--tags":
            return [], "`git push --tags` pushes every tag", False
        if a in ("-d", "--delete", "-n", "--dry-run"):
            return [], "", True
        if a in _PUSH_OPTS_WITH_VALUE and i < len(args):
            i += 1
            continue
        if a in {"-u", "--set-upstream", "-f", "--force", "--force-with-lease", "--force-if-includes",
                 "--atomic", "--porcelain", "--no-verify", "-v", "--verbose", "-q", "--quiet"} \
                or a.startswith(("--force-with-lease=", "--push-option=")):
            continue
        if a.startswith("-"):
            return [], f"unsupported push option `{a}`", False
        positional.append(a)
    refspecs = positional[1:]
    if not refspecs:
        return [], "specify the remote and source explicitly, for example `git push origin HEAD`", False
    revs = []
    for spec in refspecs:
        src = spec.lstrip("+").partition(":")[0]
        if not src:           # ":branch" deletes a remote branch
            continue
        if not _static(src) or src == "tag" or src.startswith("-"):
            return [], "dynamic, wildcard or tag shorthand refspecs are not supported", False
        revs.append(src)
    return revs, "", not revs


def _pr_refs(args: list[str]) -> list[str]:
    for i, a in enumerate(args):
        if a in ("-H", "--head") and i + 1 < len(args):
            return [args[i + 1].rpartition(":")[2]]
        if a.startswith("--head="):
            return [a.partition("=")[2].rpartition(":")[2]]
    return ["HEAD"]


def guarded_actions(command: str, cwd: str, guard_pr: bool = True, _depth: int = 0) -> list[Action]:
    """Every ``git push`` / ``gh pr create`` the command would run, with its repository cwd."""
    segments = _split(command)
    if segments is None:
        return [Action("opaque", cwd, text=command)] if _OPAQUE_HINT.search(command) else []
    actions: list[Action] = []
    for raw in segments:
        words = _unwrap(raw)
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "cd":
            target = words[1] if len(words) > 1 else "~"
            if _static(target):
                cwd = _resolve(target, cwd)
            continue
        if name in _SHELLS and len(words) >= 3 and words[1] in ("-c", "-lc", "-ec") and _depth < 2:
            actions += guarded_actions(words[2], cwd, guard_pr, _depth + 1)
            continue
        if name == "git":
            sub, args, gcwd = _git_parts(words, cwd)
            if sub == "push":
                refs, why, skip = _push_refs(args)
                if not skip:
                    actions.append(Action("push", gcwd, refs=refs, unverifiable=why, text=" ".join(raw)))
                    if any(w.startswith(("-c", "--config-env", "--git-dir", "--work-tree")) for w in words[1:words.index("push")]):
                        actions[-1].unverifiable = "Git configuration overrides are not supported"
        elif name == "gh" and guard_pr and len(words) >= 3 and words[1] == "pr" and words[2] == "create":
            actions.append(Action("pr-create", cwd, refs=_pr_refs(words[3:]), text=" ".join(raw)))
            if any(w in {"-R", "--repo"} or w.startswith("--repo=") for w in words[3:]):
                actions[-1].unverifiable = "cross-repository PR creation is not supported"
    if actions and len(segments) > 1:
        safe_cd = len(segments) == 2 and len(segments[0]) == 2 and segments[0][0] == "cd" \
            and _static(segments[0][1]) and "&&" in command and not any(c in command for c in ";|\n")
        if not safe_cd:
            for action in actions:
                action.unverifiable = "run the push or PR creation separately from other commands"
    if actions and (re.search(r"\b(?:GIT_[A-Z_]+|PATH)=", command) or "$" in command):
        for action in actions:
            action.unverifiable = "dynamic paths or environment overrides are not supported"
    if not actions and _OPAQUE_HINT.search(command):
        if not (len(segments) == 1 and len(segments[0]) >= 2 and segments[0][:2] == ["git", "push"]
                and any(w in {"--delete", "-d", "--dry-run", "-n"} for w in segments[0])):
            return [Action("opaque", cwd, text=command)]
    return actions


def check_invocation(command: str, check: str, cwd: str) -> str | None:
    """If ``command`` runs exactly the configured ``check`` (optionally after ``cd <dir> &&``),
    return the directory it runs in; otherwise None. Pipelines, chains and redirections never
    count: their exit status is not the check's."""
    segments = _split(command)
    want = _split(check)
    if not segments or not want or len(want) != 1:
        return None
    if command.count("&&") > (1 if len(segments) == 2 else 0):
        return None
    if any(op in command for op in ("|", ";", "||", ">", "<", "&\n")) or command.rstrip().endswith("&"):
        return None
    if len(segments) == 2:
        first = segments[0]
        if len(first) != 2 or first[0] != "cd" or not _static(first[1]) or "&&" not in command:
            return None
        cwd = _resolve(first[1], cwd)
        segments = segments[1:]
    if len(segments) != 1:
        return None
    # shlex drops quoting: "$VAR" and '$VAR' must not certify each other.
    if len(_split(command) or []) == 2:
        actual = command.split("&&", 1)[1].strip()
    else:
        actual = command.strip()
    return cwd if segments[0] == want[0] and actual == check.strip() else None


def is_receipts_config_change(command: str) -> bool:
    """True when the agent tries to change receipts configuration through the CLI."""
    segments = _split(command)
    if segments is None:
        return bool(re.search(r"\bhermes\b[^\n;|&]*\breceipts\b[^\n;|&]*\b(check|clear|off)\b", command))
    for raw in segments:
        words = _unwrap(raw)
        if len(words) >= 3 and os.path.basename(words[0]) == "hermes" and words[1] == "receipts" \
                and words[2] in ("check", "clear"):
            return True
    return False
