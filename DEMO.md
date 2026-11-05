# 75-second demo outline

Run `demo.py` with the Hermes installation's Python. It uses real Hermes plugin
discovery, hook dispatch and local terminal execution. The push target is a local
bare Git repository; no model is involved in this deterministic demonstration.

| Time | Show | Say |
| --- | --- | --- |
| 0–10s | `/receipts check python3 check.py` | “A passing test result belongs to specific code. Receipts keeps that connection.” |
| 10–22s | First push blocked | “This commit has no passing receipt, so the Hermes terminal hook blocks its push.” |
| 22–35s | Check passes; push succeeds | “The actual command exits zero. Receipts records the content, and the push goes through.” |
| 35–50s | Changed file committed; next push blocked | “Change the code and that earlier pass no longer qualifies. The message names the changed file.” |
| 50–62s | Failed check; push remains blocked | “A failure stays a failure, whatever the agent says about it.” |
| 62–75s | Fix, check, commit, push | “Check the fix, commit the same content, and ship it. The receipt follows the content across the commit.” |

Suggested post text, for the operator to review and publish:

> Built receipts for the @NousResearch Hermes plugin hackathon: verification
> results tied to Git content. It blocks supported terminal pushes until the
> configured check passes for the tree being pushed. Change the code, check
> again. Demo uses real Hermes hooks and a local Git remote.

The video and hackathon entry form still need the operator's submission.
