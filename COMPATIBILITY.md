# Hermes 0.23 compatibility check

Verified on Linux on 2026-11-09 against the official Hermes `v0.23.0` tag,
commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, using its package-managed
Python 3.14 runtime. The Receipts baseline was
`0dd1a374cc70ce388040f99adc908c7054ed8fa9`.

## Result

- **Single-profile, local terminal, in-process plugins:** the actual runtime
  demo passes. Unchecked and changed commits are blocked, a failed check keeps
  the push blocked, and checked content reaches a disposable local bare remote.
- **Multiplex context and multiplex process:** the baseline returned no push
  directive while reporting itself inactive. The updated plugin explicitly
  blocks recognized publishing commands and explains the unsupported mode.
  Ordinary terminal commands still run through the actual local backend.
- **Check configuration:** unsupported chains, pipes, redirects, background
  commands and unrecognized shell syntax are rejected without replacing a
  previous valid check. Saved unsupported checks receive repair guidance.
- **Multiline checks:** the recorder now rejects embedded newlines. Previously,
  a configured `false` followed by a newline and `true` could be mistaken for
  one check and record the last command's successful exit status.
- **Coprocesses:** `coproc false` reports launch success before its asynchronous
  check fails. The recorder now rejects `coproc` and shell negation at command
  position, including timed and grouped variants. Real Bash regression cases
  verify that these exit-zero launches never earn passing receipts. Ordinary
  arguments such as `echo coproc` remain accepted.

Hermes's gateway defaults to multiplexing, but its single-profile optimization
can leave multiplex mode off. The plugin checks Hermes's actual mode rather
than inferring it from a product name or config default. Disabling multiplexing
through the retired `gateway.multiplex_profiles: false` setting is not a
recommended workaround.

This change does **not** add multiplex receipt support. While multiplexing is
active, the enabled plugin blocks recognized publishing commands even in
unconfigured repositories, without inspecting host Git repositories or reading
repository settings. Use a standalone single-profile Hermes CLI session to
record checks and use the gate. Non-local backends and separate plugin-host
mode remain inactive.

## Reproduce

Use the Python interpreter from a supported Hermes installation. The two
integration scripts create temporary Hermes homes and repositories; neither
calls an LLM or needs model credentials.

```sh
python3 -m unittest discover -s tests -v
/path/to/hermes/python demo.py
/path/to/hermes/python demo_multiplex.py
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

To compare the old behavior without changing the checkout, export the baseline
to a temporary directory and pass it to the updated compatibility script:

```sh
baseline=$(mktemp -d)
git archive 0dd1a374cc70ce388040f99adc908c7054ed8fa9 | tar -x -C "$baseline"
/path/to/hermes/python demo_multiplex.py --source "$baseline" --expect inactive
```

The updated suite has **38 passing tests**, up from the 30-test baseline. The
new regression cases fail against the baseline. Validation and doctor also
pass. Validation warns about private Hermes imports in the demo harnesses,
which may need updates when Hermes internals change. Its plugin-host isolation
classification is not evidence of plugin-host compatibility.

## Verification limits

The multiplex script uses Hermes's real plugin discovery, hook dispatcher,
context-local and process-global multiplex APIs, and local terminal backend.
It does not start a complete gateway, exercise a model-driven agent turn, or
prove concurrent multi-profile isolation. No remote push, PR creation,
marketplace update or hackathon submission is part of these tests. macOS and
other Python versions were not rerun for this change.

Receipts remains a bounded local workflow gate, not tamper-proof evidence or a
replacement for CI and branch protection.

References: [Hermes v0.23.0 source](https://github.com/NousResearch/hermes-agent/tree/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19),
[multiplex design and single-profile optimization](https://github.com/NousResearch/hermes-agent/blob/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19/website/docs/developer-guide/multiplexing-gateway.md).
