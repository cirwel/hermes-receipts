# receipts

**Keep verification attached to the code you ship.**

Receipts is a Hermes plugin that blocks a supported `git push` or `gh pr create`
until your configured check has passed for the Git tree being published.
Edit a file and the earlier result no longer qualifies for the changed content.
Commit the exact content you checked and the receipt still qualifies.

The gate runs in Hermes's terminal hooks. It uses the command's exit code and
repository fingerprints before and after the run; the agent saying “tests passed”
doesn't create a receipt. No model call or additional model tool is needed.

## Try it

Requires Git, Python 3.10+, and Hermes with `pre_tool_call` and `post_tool_call`
plugin hooks (manifest minimum: 0.22.0; runtime demo verified with 0.22.5).

Install from this checkout:

```sh
hermes plugins install file:///absolute/path/to/hermes-receipts --enable
hermes plugins enable receipts
```

The local checkout must be committed before installing. Start a new Hermes
session in your repository. Set the check yourself:

```text
/receipts check python3 -m unittest discover -s tests
```

Then ask Hermes to run your check and push:

```text
git push origin HEAD                            → blocked: no passing receipt
python3 -m unittest discover -s tests            → pass recorded for this content
git push origin HEAD                            → allowed by receipts
```

If you checked uncommitted changes, commit those changes before pushing.
Receipts checks the commit being pushed, so an unrelated uncommitted edit
does not invalidate a receipt for an unchanged commit.

After another edit is committed, the old result doesn't qualify for that tree.
The block message names files changed since the last passing check. Run the check
again and retry. A failed run for the same content supersedes an earlier pass.

## Commands

| Command | Result |
| --- | --- |
| `/receipts` | Configured check and receipt for current working content |
| `/receipts check <command>` | Set the exact check command for this repository |
| `/receipts log` | Recent passes, failures, unknown results and changes during a run |
| `/receipts clear` | Remove this repository's receipt history |
| `/receipts check --off` | Turn the gate off for this repository |

Run the check in the repository root, as a foreground terminal command on its
own. Pipes, redirects, chained commands and background jobs do not earn receipts.
Unconfigured repositories are unaffected.

## What is verified

Each receipt binds the configured command and its exit status to a Git tree ID.
The fingerprint includes tracked files and non-ignored untracked files, including
deletions, executable modes and symlinks. It matches the tree produced by
`git add -A` and `git write-tree` for supported repositories, without writing to
their index or object database. Git's built-in text conversions are respected.

Supported pushes name their source explicitly, for example `git push origin HEAD`
or `git push origin feature:feature`. Multiple explicit sources are checked
individually. Plain `gh pr create` checks HEAD; `--head branch` checks that local
branch. Common wrappers and a static `cd <repo> &&` prefix are recognized.
Implicit push destinations/ref selections, bulk pushes, follow-tags, Git config
overrides, and command chains that might change HEAD before pushing are refused.
The message explains how to use a supported command.

## Scope and storage

This release covers the **local terminal backend** and Hermes's default
`plugins.isolation: in_process` mode. Remote/container terminal backends,
multiplexed profiles, and the separate plugin-host mode are inactive.

Receipts is a workflow gate, not a security boundary. Shell recognition is
deliberately bounded; aliases, custom publishing scripts, other tools and a user's
own terminal are outside the gate. It does not prove that the chosen check is
sufficient, or fingerprint ignored files, dependencies, environment variables,
external services or changes that occur and revert between observations.

Repositories with submodules, embedded repositories, sparse/assume-unchanged
entries, unresolved index conflicts, or configured external clean/process filters
cannot earn new receipts. Fingerprinting has an eight-second budget. Checks still
run when a fingerprint cannot be made; `/receipts` reports the limitation.

The plugin runs local Git subprocesses. Fingerprinting disables Git hooks,
fsmonitor and lazy fetch, and refuses external clean/process filters before
running Git status. It stores settings and up to 500 receipts per repository in
`plugin-data/receipts/receipts.db` under the active Hermes home. Records contain
repository paths, command text, tree/file hashes, exit status, timestamps and
session identifiers. Command output is represented by a SHA-256 digest and
character count, not saved as text. Receipts makes no network requests.

## Validation and reproducible demo

```sh
python3 -m unittest discover -s tests -v
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Run `demo.py` with the Python interpreter from your Hermes installation:

```sh
/path/to/hermes/venv/bin/python demo.py
```

The demo loads the plugin through Hermes discovery and exercises its hook
dispatchers and real local terminal backend. It creates an isolated Hermes home,
a test repository and a **local bare push target**, then demonstrates:

1. An unchecked push is blocked.
2. A passing check allows the push.
3. A committed edit makes that earlier receipt inapplicable.
4. A failed check keeps the push blocked.
5. A fix checked before committing can be committed and pushed.

The demo harness imports Hermes's internal dispatchers to test actual integration;
these test-only imports appear as warnings in catalog validation. The plugin's
runtime uses documented helpers and hook registration APIs.

It uses no LLM and leaves your installed plugins and repository configuration
unchanged. The automated suite compares fingerprints against Git's actual tree
IDs and checks that fingerprinting leaves the real index and object store intact.

Apache-2.0. Built by Kenny Wang with AI-assisted implementation and review.
