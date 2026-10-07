# Measuring it yourself

Every number in the README comes from one of these scripts. They need a checkout and `jev` (local by default); none of them needs a Claude or Codex account except the native checks at the end.

| Script | What it measures | Needs |
| --- | --- | --- |
| `python3 -m tests.bench` | 30 multi-session scenarios replayed through the kernel's own hooks and MCP server | jev |
| `python3 -m tests.bench --filler 0 100 500 1000` | the same scenarios among more and more unrelated facts | jev |
| `python3 -m tests.bench --replay fixtures/bench/recorded.json` | the same, on answers recorded from the local judge: deterministic, runs in CI | nothing |
| `python3 -m tests.relevance_check` | selection on 60 labelled questions over 122 facts | jev (or `--scores fixtures/relevance_scores.json`) |
| `python3 -m tests.holdout_check` | the whole capture path on 38 held-out messages | jev |
| `python3 -m tests.growth_check` | an old constraint asked about in other words, as memory grows | jev |
| `python3 -m tests.compat_matrix` | end to end in the real clients, with their versions recorded | Claude Code (and a trusted Codex pilot) |

The latest results, and the failures behind them, are in [verification](verification.md#the-benchmark). In short, with the local judge and the hook's real budget: 193 of 211 checks with an empty memory, 186 with 200 unrelated facts, 179 with 1,000.

## The scenarios (`fixtures/bench`)

Thirty JSON files, half in English, half in Spanish, four mixed. Each one is two to four sessions of a few turns: what the user types, what a competent agent proposes to save (with the user's words as `quote`), the other memory tools it calls, its reply, and what should be true afterwards. They cover:

- facts stated in passing (deadlines, approvers, budgets, decisions, preferences);
- messages a sloppy agent would save but the kernel must not: questions, hypotheticals, sarcasm, requests for work, pasted mail and logs, someone else's private life, past states, uncertainty;
- corrections under a different key name, ambiguous changes ("the deadline moved") that must not replace anything;
- recommendations resting on one or several facts, flagged for review when one changes and not when something unrelated does;
- forgetting one property, forgetting a whole thing, undoing the last save;
- rules that must reach every later session;
- decision records accepted, superseded, forgotten, and routine commits that must not become facts;
- "don't remember this".

The agent's part is fixed, so the benchmark measures the kernel and its judge, not an agent's extraction. The cues the agent passes are in `fixtures/bench/cues.json`; Claude Sonnet wrote them from each message and fact alone, never seeing a later question. `fixtures/bench/filler.json` holds 1,000 unrelated facts with cues written the same way.

Each expectation is one check. The report gives the share passed per kind of check (delivered, not delivered, review flagged or not, current value, absent, held, standing) and per scenario tag, plus hook latencies. Prompt hooks run with the production budget of 8 seconds unless `--budget none`.

## The relevance set (`fixtures/relevance.json`)

Four workspaces (a SaaS engineer, a freelance mobile developer with three clients, a platform engineer, a person mixing work and personal context), 26 to 34 facts each, and 60 questions: 33 paraphrased (no word in common with the facts that matter), 15 direct, 12 generic (nothing stored should come). Nine questions need two or three facts together. Labels say which facts a careful colleague answering would take into account; a fact merely on the same topic is not one. Cues (`fixtures/relevance_cues.json`) were written blind to the questions.

`saas` and `freelance` are where the selection rule was chosen. `platform` and `personal` are held out and reported separately.

## Held-out capture (`fixtures/holdout.jsonl`)

38 messages written after the capture thresholds were set, never used to tune them: 18 that state a fact and 20 that do not (questions, a sister's diet, sarcasm, a pasted ticket, a request for work, a log line…). Each goes through the real prompt hook and `memory_capture`, as if the agent proposed exactly the row's fact.

## Recording and replaying the judge

`python3 -m tests.bench --record FILE` runs with the real judge and keeps every answer; `--replay FILE` answers from it. A replay is deterministic and needs no model, so CI runs the benchmark on every push and fails when the share of checks passed drops (`--min-rate`). A request the recording lacks (the kernel now asks something different) counts as the judge not answering and shows up as `replay_missing`. Recordings run without the time limit, so what gets judged does not depend on the machine.
