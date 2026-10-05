# v0.4 plan and status

This is the canonical plan for the repository. The goal comes from the public hypothesis that started it: continuity across conversations and agents. Connect recommendations to the assumptions behind them, flag what needs review when an assumption changes, and do it without making the user maintain another layer of documentation. v0.3 proved the mechanism with owner commands. v0.4 removes the commands.

## Principles

- No second LLM. The host agent extracts; jev judges; the kernel decides.
- Judgments are signals for a policy. Correctness, authorization, and session origin are separate checks.
- Autonomy within categories the user allowed for a scope, not approval per memory. Reversible is not a licence to store anything.
- Writes and deletions are bound outside the model: a hook-issued token, a session bound to the server's host process, and a typed turn whose text asks for it.
- Selection fails open, writes fail closed, and memory text never goes to a hosted judge unless the scope allows it.
- Thresholds are measured, not guessed.

## Delivered

| Phase | What the user stops doing by hand | Status |
| --- | --- | --- |
| 0 Plumbing | Nothing yet. Fewer judgments: cache, acknowledgement skip, plan reuse on retry | Done; schema v3, judge client, turns, binding, Ollama planner removed |
| 1 Capture | `remember`, `correct`, approvals | Done; segmentation, categories, lattice, caps, quarantine, tombstones, do-not-remember, undo, receipts |
| 2 Chat tools | `list`, `inspect`, `forget`, `revoke`, `dependents`, `reaffirm`, policy | Done; validated against the recorded turn |
| 3 Inferred dependencies | `depend` | Done; premises delivered or captured in the same turn; review notices without text |
| 4 Calibration and decay | Periodic cleanup, guessing thresholds | Done; `calibrate --score`, confirmation by use, 180-day eligibility, 90-day digest |
| 5 One experience, two components | Installing two things | Next: offer the kernel from jevmate as an experimental memory module, after a three-way comparison |

## Acceptance

Unit and subprocess tests cover each phase with an in-memory judge and a fake `jev` executable. A local end-to-end smoke ran real processes: hook, bound MCP server, real jev capture, Stop inference. The native acceptance has five fresh `claude -p` sessions with no memory commands (`tests/native_claude_check.py`). It checks:

- the constraint is captured from conversation;
- the recommendation is linked by inference;
- the change is captured as a new version;
- a fresh session flags the recommendation for review and the answer names the change;
- asking to forget removes the fact;
- a generic question carries no claims.

The Codex walkthrough repeats the flow with the generated Codex bundle. Results are recorded in [verification](verification.md), including failures.

The "must-have" gate before widening from the `work` scope to personal scopes:

- capture precision (undo and quarantine rates);
- coverage (omitted and missed counts against gate estimates);
- later usefulness (repeated explanations avoided, stale recommendations flagged, answers the owner corrected);
- all three measured on real use, and the undo rate under about one in ten captures.

## Phase 5: one experience, two components

Context Kernel stays an independent component. It depends on a judgment client, not on the jevmate product. jevmate can be the thing people install, and offer the kernel as an experimental memory module with per-scope activation. Before that, compare jevmate alone, the kernel alone, and both on the same continuity tasks: repetitions, stale recommendations, added latency per turn, and manual maintenance. Consolidate only if both together win without adding wait or work. A shared repository is a maintenance decision for later.

## Outside v0.4

Cloud sync, bulk transcript ingestion, embeddings, a second generative extractor, inferred facts about third parties, cross-user authorization, autonomous policy learning, forensic or remote erasure.
