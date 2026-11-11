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
plugin hooks (manifest minimum: 0.22.0; runtime demo verified with 0.22.5 and
0.23.0). Use a standalone single-profile Hermes CLI session with the local
terminal and in-process plugins. See [compatibility](COMPATIBILITY.md) for the
multiplex limitation and exact test scope.

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
| `/receipts show [id]` | One receipt in full: check, content, model, issuer, session and time |
| `/receipts clear` | Remove this repository's receipt history |
| `/receipts check --off` | Turn the gate off for this repository |

Run the check in the repository root, as a foreground terminal command on its
own. Pipes, redirects, chained commands and background jobs do not earn receipts.
Shell control forms such as `coproc` and `!` are unsupported, including after
wrappers such as `time`. Commands such as `echo coproc` remain accepted.
Setup rejects command forms the recorder cannot recognize and keeps the previous
setting unchanged. For multiple steps, put them in a script that returns a nonzero
exit code when any required step fails and waits for all required work to finish,
then configure the script itself:

```text
/receipts check python3 check.py
```

Previously saved unsupported checks show repair guidance in `/receipts` and
the push-block message. Unconfigured repositories are unaffected in supported
single-profile mode.

## Who checked it

Each receipt also records who ran the check:

- **Model:** the model driving the Hermes session when the check started,
  taken from Hermes's `pre_llm_call` hook. The plugin reads only the session id
  and model name from that hook; it does not read or change prompts, and it
  returns nothing. If Hermes has not reported a model, the field is empty; the
  plugin never guesses one.
- **Issuer:** an id the plugin mints once per ledger and then reuses, so
  receipts from one Hermes install can be told apart from another's.
- **Session:** the Hermes session id.

`/receipts show` prints the receipt for the current content as text you can
paste into a pull request:

```text
Receipt #3 — passed
  check:    `python3 check.py` (exit 0)
  content:  tree 95862da741f61e3dea33204562e1421ef904c377 (HEAD ff7a3fbb0c)
  model:    demo-model (no LLM call)
  issuer:   ac2cf283-f7ec-43b9-bd90-6979162c533b
  session:  receipts-demo
  recorded: 2026-11-11T05:26:27Z
  output:   sha256 0153121493dc… (12 chars)
  Recorded locally by the receipts plugin from Hermes hook data. Not a signature.
```

These fields describe a local record. Anyone who can write the ledger file can
change them, so they are provenance for honest setups, not proof against a
forger. Ledgers from 0.1.x gain the new fields in place; older receipts show
them as not recorded.

## Without Hermes: a Git pre-push hook

`cli.py` runs the same receipts as a plain Git tool, with no Hermes involved.
The gate is a `pre-push` hook that Git runs itself, so it covers every tool
that pushes from the repository: a person, Claude Code, Codex, Hermes or a
script. It needs Git 2.31+ and Python 3.9+ and has no other dependencies.

```sh
ln -s /absolute/path/to/hermes-receipts/cli.py ~/.local/bin/receipts
cd your-repository
receipts check python3 -m unittest discover -s tests
receipts install-hook
receipts run          # runs the check here and records the result for this content
git push              # allowed only when the pushed commit's content has a pass
```

| Command | Result |
| --- | --- |
| `receipts check <command>` | Set the check (`--off` removes it; no arguments shows it) |
| `receipts run [--actor <text>]` | Run the check in this worktree and record a receipt |
| `receipts status` | The check, the receipt for the current content, and the hook |
| `receipts verify [rev ...]` | Exit 0 when each commit (default `HEAD`) has a passing receipt |
| `receipts log` | Recent receipts, with the actor when one was given |
| `receipts install-hook` / `uninstall-hook` | Add or remove the `pre-push` hook |

Differences from the plugin:

- The tool runs the check itself, so any one-line shell command is accepted.
- The check and its receipts belong to the repository, not to one directory:
  every worktree shares them, and a pass earned in one worktree counts for the
  same content in another.
- `--actor` (or `$RECEIPTS_ACTOR`) stores free text with the receipt, such as a
  model name and an agent id. It is a self-reported label, not proof.
- The ledger is `$RECEIPTS_DB`, by default `~/.local/state/receipts/receipts.db`,
  and is separate from the Hermes plugin's ledger.
- Only `git push` is gated; a pull request needs a pushed branch, so `gh pr
  create` is covered that way. Deleting a remote branch is not gated.
- Only the last commit of each pushed branch or tag is verified, as in the
  plugin; earlier commits in the same push are not checked individually.
- Repositories the fingerprint does not support (submodules, sparse or
  assume-unchanged entries, external clean filters, embedded repositories)
  cannot earn receipts. `receipts check` warns about this when you set a check.
- A check that writes untracked, non-ignored files changes the content it is
  checking and earns no receipt. Add those files to `.gitignore`.
- `git push --no-verify` skips the hook, as it skips every Git hook. This is a
  guard against forgetting, not against someone who intends to bypass it.
- `install-hook` will not overwrite an existing `pre-push` hook it did not
  write; it prints the line to add to yours. The hook records the tool's
  location and blocks with an explanation if the tool is later moved.

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
and the separate plugin-host mode are inactive.

**Multiplexed profiles are not supported.** When Hermes reports a multiplexed
context, Receipts does not record checks or inspect host repositories. While the
plugin is enabled, recognized publishing commands are blocked with an explicit
inactive-mode explanation, including in unconfigured repositories. Ordinary
terminal commands continue. Use a standalone single-profile CLI session to earn
and use receipts. Hermes 0.23's gateway defaults to multiplexing, though its
single-profile optimization can leave multiplex mode off; do not assume that
every Hermes session has the same behavior.

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
/path/to/hermes/venv/bin/python demo_multiplex.py
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
`demo_multiplex.py` checks the explicit unsupported-mode block through Hermes's
real discovery, hook dispatch and multiplex APIs. It executes only an ordinary
local terminal command and never executes a push or PR command. This is not a
full gateway or concurrent-profile compatibility test.

Apache-2.0. Built by Kenny Wang with AI-assisted implementation and review.
