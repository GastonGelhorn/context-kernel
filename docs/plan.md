# Plan and status

The project started from a public hypothesis about continuity across conversations and agents. The kernel should connect each recommendation to the assumptions behind it and flag what needs review when one of those assumptions changes, without asking the user to maintain another layer of documentation. v0.3 proved the mechanism with owner commands, and v0.4 removed the commands. v0.5 packages the kernel as a Claude Code plugin, with an installer and a setup wizard for the terminal and Codex. v0.6 reads the decisions a repository already records, and hands the facts a user keeps repeating to every session. v0.7 compared jevmate alone, the kernel alone and both. v0.8 works on trust and scale: fewer false memories, selection that holds up as memory grows and fits the hook's time, a judge checked per model, and measurements anyone can rerun. It runs on Python 3.9 or later.

## Principles

- There is no second LLM. The host agent's own model extracts, jev judges, and the kernel makes the decision.
- jev's judgments are inputs to a policy. Whether a fact is correct, whether an action is authorized and where a session came from are each checked separately.
- The user allows categories per scope, and inside those the kernel captures without asking about each memory. That a capture can be undone is no reason to store whatever comes along.
- Writes and deletions are bound by things the model cannot produce: a token issued by the hook, a session bound to the server's host process, and a typed turn whose text asks for the action.
- When the judge is unavailable, selection falls back and the prompt still goes through, while writes stop. Memory text goes to a hosted judge only when the scope allows it.
- Every threshold comes from a measurement.

## Delivered

| Phase | What the user stops doing by hand | Status |
| --- | --- | --- |
| 0 Plumbing | Nothing yet. Fewer judgments: cache, acknowledgement skip, plan reuse on retry | Done; schema v3, judge client, turns, binding, Ollama planner removed |
| 1 Capture | `remember`, `correct`, approvals | Done; segmentation, categories, lattice, caps, quarantine, tombstones, do-not-remember, undo, receipts |
| 2 Chat tools | `list`, `inspect`, `forget`, `revoke`, `dependents`, `reaffirm`, policy | Done; validated against the recorded turn |
| 3 Inferred dependencies | `depend` | Done; premises delivered or captured in the same turn; review notices without text |
| 4 Calibration and decay | Periodic cleanup, guessing thresholds | Done; `calibrate --score`, confirmation by use, 180-day eligibility, 90-day digest |
| 5 One experience, two components | Installing two things | In progress: v0.5 lists the kernel as its own plugin in jevmate's marketplace; merging the two plugins waits for a three-way comparison |
| 6 What is already written down | Restating decision records and commit decisions; repeating the same rule in every session | Done in v0.6; decision records parsed, commits filtered and judged one by one, standing facts after three sessions or a rule. v0.8 reads commits only when turned on |
| 7 Trustworthy at scale | Checking what memory saved; waiting for the judge; re-measuring after a model change | v0.8: cited quotes, holds for jokes and requests for work, safe mode with a canary per model, cues and a time-fitted selection, warm-up and keep-alive, precision measured from real use, a public benchmark and a compatibility matrix |
| 8 Facts that end | Forgetting a plan once it is done; cleaning out "this week" after the week | v0.9: `memory_close` (done, cancelled, ended; kept in history, not regret) checked against the user's words, `reopen`, a request when a message says something is over, and facts that end with the relative period they name |

## Acceptance

Unit and subprocess tests cover each phase, using an in-memory judge and a fake `jev` executable. A local end-to-end smoke test ran the real processes: the hook, the bound MCP server, real jev capture and Stop inference. The native acceptance test (`tests/native_claude_check.py`) runs five fresh `claude -p` sessions in which nobody types a memory command. It checks that:

- the constraint is captured from conversation;
- the recommendation is linked to it by inference;
- the change is captured as a new version;
- a fresh session flags the recommendation for review, and the answer names the change;
- asking to forget removes the fact;
- a generic question carries no claims.

The Codex walkthrough repeats the flow with the generated Codex bundle. Results are recorded in [verification](verification.md), failures included.

Capture widens from the `work` scope to personal scopes only after a must-have gate. Three things have to be measured on real use, and the undo rate has to stay under about one in ten captures:

- capture precision, from the undo and quarantine rates;
- coverage, from omitted and missed counts against the gate's estimates;
- later usefulness: repeated explanations avoided, stale recommendations flagged, answers the owner corrected.

## Phase 5: one experience, two components

Context Kernel stays a separate component. It depends on a judgment client, `jev`, and on nothing else from jevmate.

The first step shipped in v0.5. The kernel is its own Claude Code plugin, listed in jevmate's marketplace next to jevmate, so both install from the same place:

```text
/plugin marketplace add GastonGelhorn/jevmate
/plugin install context-kernel@gastongelhorn
```

Whether to merge the two into one plugin was left until jevmate alone, the kernel alone and both together had been compared on the same continuity tasks. The rule was to merge them only if the combination won without adding wait or work.

That comparison ran in v0.7 (`tests/compare_check.py`, [verification](verification.md#v07-jevmate-alone-the-kernel-alone-both-and-neither)). Over three repetitions:

- With both, the user explained nothing twice (0 of 9) and no stale recommendation went unflagged (0 of 6).
- With the kernel alone, the figures were 3 of 9 and 2 of 6.
- With jevmate alone, 9 of 9 and 3 of 6, the same as with nothing.
- The combination costs about 5 s more per session and 28% more tokens than no memory.

So the combination wins on continuity, but not without adding wait. The decision:

- **Keep them as two plugins.** jevmate alone does nothing for continuity, and the kernel already depends only on `jev`, not on the plugin. Merging would load jevmate's hooks into sessions that want memory, and memory's latency into sessions that only want jevmate.
- **Recommend installing them together for memory.** The kernel without jev selection is clearly worse. The plugin already defaults to jev selection; rules are only the fallback when jev is missing or slow, and the docs say so.
- **Work on the cost before revisiting the merge.** The cost is the 3 s prompt hook (cold judgments) and the extra tool round trip per save. Revisit when a cold prompt costs under 1 s.

A shared repository is a maintenance question for later.

v0.8 confirmed the split after an external review, and changed how the two are presented. Context Kernel is the product: what users see is memory that notices when advice goes stale. jev is the engine underneath, and the jevmate plugin is a separate tool for decisions inside a session (sift, tests and diff, triage), whose surface is frozen until its three main uses have external users. What shipped instead of a merge is one install: the kernel is in jevmate's marketplace, and the session-start warm-up loads jev's model so the first prompt does not pay for it.

## Before widening capture

The gate in [Acceptance](#acceptance) asks for capture precision on real use. `memory metrics` now reports it: of the captures made in the last 30 days, the share the user took back soon after (undone, forgotten or revoked within a week, replaced within an hour). Capture widens beyond `work` only when that stays above 0.9 over a month of daily use by more than one person.

## Out of scope

Cloud sync, bulk transcript ingestion (the repository pass reads decision records and commit subjects, nothing else), embeddings, a second generative extractor, inferred facts about third parties, cross-user authorization, autonomous policy learning, forensic or remote erasure.
