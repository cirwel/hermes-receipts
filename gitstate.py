"""Read-only repository fingerprints.

A fingerprint is the git tree id the working tree would have if every tracked
change and every untracked, non-ignored file were committed. It is computed
without writing to the repository: blob ids come from ``git hash-object``
without ``-w``, and the changed directories are re-hashed here. When the
working tree is clean the fingerprint is simply ``HEAD^{tree}``, so a receipt
recorded on uncommitted changes matches the commit that later records exactly
those changes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from dataclasses import dataclass, field

EMPTY_TREE = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}


class GitUnavailable(Exception):
    """The fingerprint could not be computed (not a repo, timeout, git error)."""


@dataclass
class Fingerprint:
    root: str
    tree: str
    head: str | None
    head_tree: str
    # Paths that differ from HEAD: path -> (mode, object id), or None when deleted.
    overlay: dict[str, tuple[str, str] | None] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.overlay


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", LC_ALL="C")
    return env


def git(root: str, *args: str, deadline: float, input: bytes | None = None, check: bool = True) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitUnavailable("time budget exhausted")
    cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-C", root, *args]
    try:
        proc = subprocess.run(cmd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=_env(), timeout=remaining, stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailable("git timed out") from exc
    except OSError as exc:
        raise GitUnavailable(f"git not runnable: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitUnavailable(proc.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return proc.stdout


def repo_root(path: str, deadline: float) -> str | None:
    if not path or not os.path.isdir(path):
        return None
    try:
        out = git(path, "rev-parse", "--show-toplevel", deadline=deadline)
    except GitUnavailable:
        return None
    root = out.decode().strip()
    return os.path.realpath(root) if root else None


def object_format(root: str, deadline: float) -> str:
    try:
        fmt = git(root, "rev-parse", "--show-object-format", deadline=deadline).decode().strip()
    except GitUnavailable:
        fmt = "sha1"
    return fmt if fmt in EMPTY_TREE else "sha1"


def _hash(fmt: str, kind: bytes, body: bytes) -> str:
    h = hashlib.sha256() if fmt == "sha256" else hashlib.sha1()
    h.update(kind + b" " + str(len(body)).encode() + b"\0" + body)
    return h.hexdigest()


def commit_tree(root: str, rev: str, deadline: float) -> tuple[str, str] | None:
    """Resolve ``rev`` to (commit id, tree id), or None when it does not name a commit."""
    try:
        commit = git(root, "rev-parse", "--verify", "-q", "--end-of-options", rev + "^{commit}",
                     deadline=deadline).decode().strip()
        tree = git(root, "rev-parse", "--verify", "-q", "--end-of-options", commit + "^{tree}",
                   deadline=deadline).decode().strip()
    except GitUnavailable:
        return None
    return commit, tree


def _ls_tree(root: str, tree: str, deadline: float, paths: list[str] | None = None, trees: bool = True):
    """Yield (mode, type, oid, path) for every entry under ``tree``."""
    args = ["ls-tree", "-r", "-z", "--full-tree"]
    if trees:
        args.append("-t")
    args.append(tree)
    if paths:
        args += ["--", *paths]
    out = git(root, *args, deadline=deadline)
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        yield mode.lstrip("0"), kind, oid, os.fsdecode(path)


def _status_paths(root: str, deadline: float) -> list[str]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
              "--ignore-submodules=none", deadline=deadline)
    paths = []
    for record in out.split(b"\0"):
        if len(record) > 3:
            paths.append(os.fsdecode(record[3:]))
    return paths


def _worktree_entry(root: str, rel: str, fmt: str,
                    deadline: float) -> tuple[str, str] | None | str:
    """Return (mode, oid), None (deleted), or the string "blob" when git must hash it."""
    full = os.path.join(root, rel)
    try:
        st = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(st.st_mode):
        return ("120000", _hash(fmt, b"blob", os.fsencode(os.readlink(full))))
    if stat.S_ISDIR(st.st_mode):
        # A tracked file replaced by a directory is removed; its children
        # appear separately. Embedded repos need their own verification.
        if os.path.exists(os.path.join(full, ".git")):
            raise GitUnavailable("embedded repositories are not supported")
        return None
    if not stat.S_ISREG(st.st_mode):
        raise GitUnavailable("unsupported file type: " + rel)
    return "blob"


def _check_supported(root: str, deadline: float) -> None:
    # Do not execute a repository's external filters while fingerprinting.
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", deadline=deadline)
    attrs = git(root, "check-attr", "-z", "--stdin", "filter", input=paths, deadline=deadline).split(b"\0")
    drivers = {os.fsdecode(value) for value in attrs[2::3]
               if value and value not in {b"unspecified", b"unset", b"set"}}
    for driver in drivers:
        for operation in ("clean", "process"):
            if git(root, "config", "--get", f"filter.{driver}.{operation}",
                   deadline=deadline, check=False).strip():
                raise GitUnavailable("external Git clean/process filters are not supported")
    entries = git(root, "ls-files", "-v", "--stage", "-z", deadline=deadline)
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        flag, mode, _oid, stage_path = entry.split(b" ", 3)
        if flag == b"S" or flag.islower():
            raise GitUnavailable("sparse or assume-unchanged index entries are not supported")
        if mode == b"160000":
            raise GitUnavailable("repositories with submodules are not supported")
        if not stage_path.startswith(b"0\t"):
            raise GitUnavailable("resolve index conflicts before recording a receipt")


def _quote_path(path: str) -> bytes:
    # Git's --stdin-paths understands C quoting, including octal byte escapes.
    return b'"' + b"".join(bytes([c]) if 32 <= c < 127 and c not in (34, 92)
                           else ("\\%03o" % c).encode() for c in os.fsencode(path)) + b'"'


def fingerprint(root: str, deadline: float) -> Fingerprint:
    """Compute the would-be tree id of ``root``'s working tree. Raises GitUnavailable."""
    _check_supported(root, deadline)
    fmt = object_format(root, deadline)
    head_ct = commit_tree(root, "HEAD", deadline)
    head, head_tree = head_ct if head_ct else (None, EMPTY_TREE[fmt])
    changed = _status_paths(root, deadline)
    if not changed:
        return Fingerprint(root=root, tree=head_tree, head=head, head_tree=head_tree)

    try:
        filemode = git(root, "config", "--bool", "core.fileMode", deadline=deadline,
                       check=False).decode().strip() != "false"
    except GitUnavailable:
        filemode = True
    symlinks = git(root, "config", "--bool", "core.symlinks", deadline=deadline,
                   check=False).decode().strip() != "false"

    head_entries: dict[str, tuple[str, str, str]] = {}  # path -> (mode, type, oid)
    if head_ct:
        for mode, kind, oid, path in _ls_tree(root, head_tree, deadline):
            head_entries[path] = (mode, kind, oid)

    included = {os.fsdecode(p) for p in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                                           deadline=deadline).split(b"\0") if p}
    index_modes = {}
    for record in git(root, "ls-files", "--stage", "-z", deadline=deadline).split(b"\0"):
        if record:
            meta, _, path = record.partition(b"\t")
            index_modes[os.fsdecode(path)] = meta.split()[0].decode()

    overlay: dict[str, tuple[str, str] | None] = {}
    to_hash: list[str] = []
    for rel in changed:
        if rel not in included:
            overlay[rel] = None
            continue
        entry = _worktree_entry(root, rel, fmt, deadline)
        if entry == "blob":
            to_hash.append(rel)
        else:
            overlay[rel] = entry  # type: ignore[assignment]

    if to_hash:
        out = git(root, "hash-object", "--stdin-paths", deadline=deadline,
                  input=b"\n".join(_quote_path(p) for p in to_hash) + b"\n")
        oids = out.decode().split()
        if len(oids) != len(to_hash):
            raise GitUnavailable("hash-object returned an unexpected number of ids")
        for rel, oid in zip(to_hash, oids):
            executable = bool(os.lstat(os.path.join(root, rel)).st_mode & stat.S_IXUSR)
            if not symlinks and index_modes.get(rel) == "120000":
                mode = "120000"
            elif filemode:
                mode = "100755" if executable else "100644"
            else:
                prev = index_modes.get(rel)
                mode = prev if prev in ("100644", "100755") else "100644"
            overlay[rel] = (mode, oid)

    # Drop entries that turn out identical to HEAD (e.g. a touched-but-unchanged file).
    for rel in list(overlay):
        new = overlay[rel]
        old = head_entries.get(rel)
        if new is not None and old is not None and old[0] == new[0] and old[2] == new[1]:
            del overlay[rel]
        elif new is None and old is None:
            del overlay[rel]
    if not overlay:
        return Fingerprint(root=root, tree=head_tree, head=head, head_tree=head_tree)

    tree = _rebuild(head_entries, overlay, head_tree, fmt)
    return Fingerprint(root=root, tree=tree, head=head, head_tree=head_tree, overlay=overlay)


def _rebuild(head_entries: dict[str, tuple[str, str, str]], overlay: dict[str, tuple[str, str] | None],
             head_tree: str, fmt: str) -> str:
    """Re-hash only the directories on changed paths; reuse HEAD's ids for the rest."""
    children: dict[str, dict[str, tuple[str, str]]] = {}
    for path, (mode, kind, oid) in head_entries.items():
        parent, _, name = path.rpartition("/")
        children.setdefault(parent, {})[name] = (mode, oid)
    children.setdefault("", {})

    dirty: set[str] = {""}
    # Remove old paths before adding replacements (file <-> directory).
    for path, entry in sorted(overlay.items(), key=lambda item: item[1] is not None):
        parent, _, name = path.rpartition("/")
        if entry is None:
            children.get(parent, {}).pop(name, None)
            dirty.add(parent)
            parts = path.split("/")[:-1]
            dirty.update("/".join(parts[:i + 1]) for i in range(len(parts)))
            continue
        # Mark every ancestor dirty and make sure it exists as a directory entry.
        parts = path.split("/")[:-1]
        for i in range(len(parts)):
            d = "/".join(parts[: i + 1])
            dirty.add(d)
            up, _, dname = d.rpartition("/")
            children.setdefault(up, {})
            if dname not in children[up] or children[up][dname][0] != "40000":
                children[up][dname] = ("40000", "")
            children.setdefault(d, {})
        dirty.add(parent)
        if entry is None:
            children.get(parent, {}).pop(name, None)
        else:
            children.setdefault(parent, {})[name] = entry

    def tree_id(d: str) -> str | None:
        entries = children.get(d, {})
        items = []
        for name, (mode, oid) in entries.items():
            sub = f"{d}/{name}" if d else name
            if mode == "40000" and (sub in dirty or not oid):
                oid = tree_id(sub)
                if oid is None:
                    continue
            items.append((name, mode, oid))
        if not items:
            return None
        items.sort(key=lambda it: os.fsencode(it[0]) + (b"/" if it[1] == "40000" else b""))
        body = b"".join(mode.encode() + b" " + os.fsencode(name) + b"\0" + bytes.fromhex(oid)
                        for name, mode, oid in items)
        return _hash(fmt, b"tree", body)

    return tree_id("") or EMPTY_TREE[fmt]


def blobs_at(root: str, tree: str, paths: list[str], deadline: float) -> dict[str, tuple[str, str]]:
    """(mode, oid) of each of ``paths`` inside ``tree`` (missing paths are absent)."""
    if not paths:
        return {}
    return {path: (mode, oid) for mode, kind, oid, path in _ls_tree(root, tree, deadline, paths, trees=False)}


def changed_names(root: str, base_tree: str, target_tree: str, deadline: float) -> list[str]:
    out = git(root, "diff", "--name-only", "-z", "--no-renames", base_tree, target_tree, deadline=deadline)
    return [os.fsdecode(p) for p in out.split(b"\0") if p]
