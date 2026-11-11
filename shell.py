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
# Options and variables that make git use a repository other than the one found from the cwd.
_RELOCATING_ENV = re.compile(r"\bGIT_(?:DIR|WORK_TREE|COMMON_DIR|NAMESPACE|INDEX_FILE|OBJECT_DIRECTORY|"
                             r"ALTERNATE_OBJECT_DIRECTORIES|CONFIG[A-Z_]*)=")
_UNVERIFIABLE_GIT_OPTS = ("-c", "--config-env", "--git-dir", "--work-tree", "--namespace", "--exec-path")


@dataclass
class Action:
    kind: str                     # "push" | "pr-create" | "opaque"
    cwd: str
    refs: list[str] = field(default_factory=list)   # revisions whose trees must have receipts
    unverifiable: str = ""        # non-empty: reason the target cannot be determined
    elsewhere: bool = False       # the repository is chosen by something other than ``cwd``
    text: str = ""


def _split(command: str) -> list[list[str]] | None:
    """Split into simple commands on shell separators. None when unparseable."""
    if len(command) > MAX_COMMAND_CHARS:
        return None
    if "`" in command or "$(" in command or "<(" in command:
        return None
    command = _fold(command)
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
        elif word and all(c in "()" for c in word):
            return None           # a subshell or group: a cd inside it does not move what follows
        elif word and all(c in ";&|()" for c in word):
            if "(" in word or ")" in word:
                return None
            segments.append([])
        else:
            segments[-1].append(word)
    return [s for s in segments if s]


def _fold(command: str) -> str:
    """Join lines the shell would join: a backslash before a newline continues the line."""
    return command.replace("\\\n", "")


_RELOCATES = re.compile(r"\b(?:cd|pushd|popd|eval|source)\b|(?:^|\s)-C|--git-dir|--work-tree|(?:^|[\s;&|(])\.\s")


def _opaque(command: str, cwd: str) -> list["Action"]:
    """An unparsed command that mentions a push. If it also contains anything that can change
    directory or repository, where it publishes from is unknown."""
    lost = bool(_RELOCATES.search(command) or _RELOCATING_ENV.search(command))
    return [Action("opaque", cwd, text=command, elsewhere=lost)]


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


def _git_parts(words: list[str], cwd: str) -> tuple[str, list[str], str, list[str], bool]:
    """(subcommand, its arguments, cwd, git's own options, repository-chosen-elsewhere)."""
    i = 1
    elsewhere = False
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        i += 1
        if opt in _GIT_OPTS_WITH_VALUE and i < len(words):
            if opt == "-C":
                elsewhere = elsewhere or not _static(words[i])
                cwd = _resolve(words[i], cwd)
            elif opt == "--git-dir":
                elsewhere = True
            i += 1
        elif opt.startswith("--git-dir="):
            elsewhere = True
        elif opt.startswith("-C") and len(opt) > 2:
            elsewhere = elsewhere or not _static(opt[2:])
            cwd = _resolve(opt[2:], cwd)
        # --work-tree does not change which repository git finds, so it never moves cwd.
    sub = words[i] if i < len(words) else ""
    return sub, words[i + 1:], cwd, words[1:i], elsewhere


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
        src, _, dst = spec.lstrip("+").partition(":")
        if not src and not dst:   # ":" alone pushes every matching branch
            return [], "a bare `:` refspec pushes every matching branch", False
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
        if a.startswith("-H") and len(a) > 2:
            return [a[2:].lstrip("=").rpartition(":")[2]]
    return ["HEAD"]


def guarded_actions(command: str, cwd: str, guard_pr: bool = True, _depth: int = 0) -> list[Action]:
    """Every ``git push`` / ``gh pr create`` the command would run, with its repository cwd."""
    command = _fold(command)
    segments = _split(command)
    if segments is None:
        return _opaque(command, cwd) if _OPAQUE_HINT.search(command) else []
    actions: list[Action] = []
    lost = bool(_RELOCATING_ENV.search(command))   # the repository is no longer known from cwd
    harmless = 0                                   # segments that are a cd, or a push that publishes nothing
    for raw in segments:
        words = _unwrap(raw)
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "cd":
            target = words[1] if len(words) > 1 else "~"
            if len(words) <= 2 and _static(target) and not target.startswith("-"):
                cwd = _resolve(target, cwd)
            else:
                lost = True               # options, `cd -`, variables: the destination is unknown
            harmless += 1
            continue
        if name in _SHELLS and len(words) >= 3 and words[1] in ("-c", "-lc", "-ec") and _depth < 2:
            nested = guarded_actions(words[2], cwd, guard_pr, _depth + 1)
            for action in nested:
                action.elsewhere = action.elsewhere or lost
            actions += nested
            continue
        if name == "git":
            sub, args, gcwd, opts, moved = _git_parts(words, cwd)
            if sub == "push":
                refs, why, skip = _push_refs(args)
                if skip:
                    harmless += 1
                else:
                    actions.append(Action("push", gcwd, refs=refs, unverifiable=why, text=" ".join(raw),
                                          elsewhere=lost or moved))
                    if any(w.startswith(_UNVERIFIABLE_GIT_OPTS) for w in opts):
                        actions[-1].unverifiable = "Git configuration and repository overrides are not supported"
        elif name == "gh" and guard_pr and len(words) >= 3 and words[1] == "pr" and words[2] == "create":
            actions.append(Action("pr-create", cwd, refs=_pr_refs(words[3:]), text=" ".join(raw), elsewhere=lost))
            if any(w in {"-R", "--repo"} or w.startswith(("--repo=", "-R")) for w in words[3:]):
                actions[-1].unverifiable = "cross-repository PR creation is not supported"
    if actions and len(segments) > 1:
        safe_cd = len(segments) == 2 and len(segments[0]) == 2 and segments[0][0] == "cd" \
            and _static(segments[0][1]) and not segments[0][1].startswith("-") and "&&" in command and not any(c in command for c in ";|\n")
        if not safe_cd:
            for action in actions:
                action.unverifiable = "run the push or PR creation separately from other commands"
    if actions and (re.search(r"\b(?:GIT_[A-Z_]+|PATH)=", command) or "$" in command):
        for action in actions:
            action.unverifiable = "dynamic paths or environment overrides are not supported"
    if not actions and _OPAQUE_HINT.search(command):
        # Deletions and dry runs publish nothing. Anything else that mentions a push but was
        # not recognised stays opaque, including when it merely follows a harmless push.
        if harmless != len(segments):
            return _opaque(command, cwd)
    return actions


def invocation_cwd(command: str, cwd: str) -> str:
    """Resolve a static leading cd so callers can find the target's settings."""
    segments = _split(command)
    if segments and len(segments) == 2 and len(segments[0]) == 2 \
            and segments[0][0] == "cd" and _static(segments[0][1]) and not segments[0][1].startswith("-") \
            and "&&" in command:
        return _resolve(segments[0][1], cwd)
    return cwd


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
    if any(op in command for op in ("|", ";", "||", ">", "<", "\n")) or command.rstrip().endswith("&"):
        return None
    if len(segments) == 2:
        first = segments[0]
        if len(first) != 2 or first[0] != "cd" or not _static(first[1]) or first[1].startswith("-") \
                or "&&" not in command:
            return None
        cwd = _resolve(first[1], cwd)
        segments = segments[1:]
    if len(segments) != 1:
        return None
    words = _unwrap(segments[0])
    if words and words[0] in {"coproc", "!"}:
        # coproc reports launch success, not the asynchronous check's status;
        # shell negation can hide it again after wrappers such as `time`.
        # Inspect command position only: `echo coproc` is an ordinary command.
        return None
    # shlex drops quoting: "$VAR" and '$VAR' must not certify each other.
    if len(_split(command) or []) == 2:
        actual = command.split("&&", 1)[1].strip()
    else:
        actual = command.strip()
    return cwd if segments[0] == want[0] and actual == check.strip() else None


def is_receipts_config_change(command: str) -> bool:
    """True when the agent tries to change receipts configuration through the CLI."""
    command = _fold(command)
    segments = _split(command)
    if segments is None:
        return bool(re.search(r"\bhermes\b[^\n;|&]*\breceipts\b[^\n;|&]*\b(check|clear|off)\b", command))
    for raw in segments:
        words = _unwrap(raw)
        if len(words) >= 3 and os.path.basename(words[0]) == "hermes" and words[1] == "receipts" \
                and words[2] in ("check", "clear"):
            return True
    return False
