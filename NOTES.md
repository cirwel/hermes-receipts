# Receipts build handoff — 2026-11-05

Implementation is committed on `codex/receipts-1105`.

Verified locally:
- 30 behavioral tests using real Git repositories, including SHA-1/SHA-256,
  staged changes, executable bits, symlinks, nested paths, quoting and Git filters.
- `hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci` pass.
- A fresh local install into an isolated Hermes home passes doctor.
- `demo.py` passes with the installed Hermes runtime: unchecked push blocked,
  passing check permits push, changed content blocked, failure stays blocked,
  checked fix survives committing and reaches a local bare remote.

The runtime plugin uses documented helpers. Catalog validation warns about
internal Hermes imports in the integration-demo harness only; README discloses them.
The default in-process plugin mode and local terminal backend are supported.

Delivery:
- Public source: https://github.com/cirwel/hermes-receipts
- Release PR and current CI: https://github.com/cirwel/hermes-receipts/pull/1
  Use its live checks for readiness. The first macOS run found a fixture race
  from detached Git maintenance; the fixture now disables that maintenance.
- The owner-account exception is authorized. Catalog submission is held while
  the operator decides whether to wait for a demo review.
- Record/publish the demo video and submit the entry form. DEMO.md has a 75-second outline.

The Google-family implementation review returned no review: its command tool was
auto-denied in headless mode and the runner ended at its 600-second cap. No
permissions were widened. A supplemental fresh-context OpenAI review found three defects (joined git -C,
symlink-placeholder modes, and leading cd check discovery). All three were fixed
with regression cases; the reviewer independently reran all 30 tests and marked
the complete follow-up delta CLEAN. This is same-family coverage, not an
independent-family review.
Earlier design consult timed out and supplied no usable review evidence. The implementation originated with Claude and was
continued and corrected by Codex.

Catalog entry and PR drafts are prepared under `.review-cache/submission/`.
The catalog pin must be regenerated after the final commit.
