# Plan and status

The project started from a public hypothesis about continuity across conversations and agents. The kernel should connect each recommendation to the assumptions behind it and flag what needs review when one of those assumptions changes, without asking the user to maintain another layer of documentation. v0.3 proved the mechanism with owner commands, and v0.4 removed the commands. v0.5 packages the kernel as a Claude Code plugin, with an installer and a setup wizard for the terminal and Codex. It runs on Python 3.9 or later.

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

Whether to merge the two into one plugin is decided later. One option is for jevmate to be the only thing people install, with the kernel as an experimental memory module that each scope turns on. Before deciding, I want to compare jevmate alone, the kernel alone and both together on the same continuity tasks, counting repeated explanations, stale recommendations, added latency per turn and manual maintenance. The two get merged only if the combination wins without adding wait or work. A shared repository is a maintenance question for later.

## Out of scope

Cloud sync, bulk transcript ingestion, embeddings, a second generative extractor, inferred facts about third parties, cross-user authorization, autonomous policy learning, forensic or remote erasure.
