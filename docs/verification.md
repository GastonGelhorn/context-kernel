# Verification

Checked on 2026-10-05 on macOS arm64 with Python 3.14.3, SQLite 3.51.2 with FTS5, Ollama 0.34.4 and the already installed `qwen3.5:9b` model. I downloaded no model and used no paid model API.

## v0.4 results

Checked on 2026-10-05 with jev 1.9.3 on its local backend (tev1-32k through Ollama), with no paid API.

### Unit and subprocess suite

209 tests pass. Two judges drive them. `tests/fakes.py:FakeJudge` runs in memory, and a fake `jev` executable answers `rank`, `ask` and `--dry-run` over a real subprocess. `tests/test_v04.py` covers each phase:

- turn tokens
- refusing a server bound to another host process, and refusing unknown or expired tokens
- continuation and markup origins
- each row of the plan's message table:
  - a project constraint with no first-person subject
  - a relation the user names
  - another person's allergy, which is held
  - a decision typed after a pasted log, which is captured while the log's fact is held as `quoted_source`
  - an unaffirmed fact, which is rejected
  - "don't save it", which stores nothing
- instruction-shaped and oversized values
- a hosted judge, which stores nothing
- the trust lattice, and a proposal in place of overriding a confirmed value
- undo only when the user asks for it
- caps counted as omissions
- a forget that lands during a slow capture (the forget wins)
- forget, policy and confirmation only from the user's own words
- truthful receipts and accounting for missed captures
- acknowledgements that cost no judgment
- the 90-day digest and 180-day eligibility
- confirmation by restating
- the post's scenario with an empty memory. A premise stated in the same turn is linked by inference, and when it later changes, a new projection flags the recommendation, without its text
- the bundle for both clients
- additive migration from v1 and v2 databases

The Ollama/Qwen planner and its tests are gone.

### Official MCP SDK 2.3.0

The server negotiated 2025-06-18, listed the 16 tools with valid schemas, and returned scoped evidence that did not include the private canary. The check also observed an owner-approved correction and read the inventory. It confirmed that a server no hook ever bound refuses `memory_capture` and `memory_forget` when given a forged token.

### Local end-to-end smoke

Real processes and the real jev:

1. The prompt hook issued a token and asked for capture of "We have three months to deliver the checkout project. Should we rewrite its payment module?".
2. A separate `serve` process accepted that token through process-ancestry binding.
3. jev validated `checkout.deadline = three months` as `constraints`, and the fact was captured.
4. The Stop hook reported the save and linked the recommendation in the reply to that fact.

### Calibration with the kernel's own questions

Run with `memory calibrate KIND --score` on the bilingual fixtures in `fixtures/calibration.jsonl`.

| Judgment | Rows | Result |
| --- | --- | --- |
| The message asserts this fact (two-field state) | 30 | All 13 true rows score ≥ 0.762. False rows score ≤ 0.757, the highest being a question ("Who approves…?"). Default 0.75 keeps recall 1.0 with one false positive. [0.60, 0.75) is held as `uncertain`. |
| The message states something worth keeping (1 - P(none)) | 20 | At P(none) < 0.15: recall 1.0, precision 0.91. The one false hit, "Please forget the checkout deadline.", is excluded by the forget check before the gate. |
| The message asks to forget this fact | 6 | True rows 0.94 to 0.98, false rows ≤ 0.60. Default 0.70. |

When the same affirmed fixtures are rendered as single candidates for `jev tune`, they separate worse. The best threshold there is 0.58, with ECE 0.29 and eight rows within ±0.02 of it. So the kernel calibrates with its own two-field question. The export for `jev tune` is still available. These are small hand-authored sets. Read the numbers as a trend; they are not a benchmark.

### Gate behaviour measured on real prompts

The instruction score is not used as a veto. "We have three months… Should we rewrite?" scored 0.82 on "instruction" while stating a fact. A generic question put P(none) at 0.27, and fact-bearing messages put it at 0.04 or less. The gate is skipped for acknowledgements, question-only messages, generic questions and forget requests.

### Prompt-hook latency

Ten stored facts, warm Ollama, hook run as a subprocess with a minimal PATH.

| Prompt | Time |
| --- | --- |
| cold question | 4.06 s |
| fact plus question (gate and relevance) | 4.12 s |
| the same question again (all judgments cached) | 0.33 s |

### First native run: 3 of 9 checks

Claude Code 2.1.287, headless, five fresh sessions, no memory commands.

These passed:
- all five sessions were bound
- the agent captured from conversation through the bound MCP server
- "Please forget the checkout deadline." removed the fact after it was validated against the typed message
- the generic question carried no claims

These failed, for these reasons:
- **Key drift.** Session 1 stored `checkout_project.delivery_timeline = three months`. In session 2 the agent did not see that fact and stored `checkout.deadline = three weeks` as a second fact next to it. The change never reached the premise, so the recommendation was never flagged, and session 3 saw two timelines that disagreed.
- **Lexical recommendation gate.** It missed "No, … don't rewrite the payment module" in session 1 and fired on "I can't tell you whether to go ahead…" in session 3.
- **Over-generic question guard.** "What is the checkout deadline?" counted as generic because the stored entity is `checkout_project`.
- **Oversized relevance query.** The owner's own hook timed out on a 10 KB pasted prompt and fell back to rules, as designed.

Fixes, each measured against the real local jev before we picked a threshold:
- The capture hint lists related stored facts, matched lexically, with their ids.
- `memory_capture` accepts `replaces`.
- When jev, reading only the key names, judges a new pair to be the same attribute as an existing one, the kernel resolves it to the existing pair. 5 of 6 true pairs scored ≥ 0.70, every false pair scored ≤ 0.64, and this exact case scored 0.723.
- Recommendations are now judged with the question "does the reply recommend, advise, or decide a course of action". Advice scores 0.80 to 0.95, and reports, questions and refusals score at most 0.61. The bar is 0.70.
- The judge reads only the authored part of the prompt, up to 1,500 characters.
- A definition question that names stored key words is not generic.

We then replayed the scenario through the kernel with the real jev and the same drifting keys:

1. The drifting key resolved to the stored pair, so the change became a second version.
2. The session-1 recommendation was linked.
3. Session 3 returned `review_required` with the recommendation's id and delivered only the current "three weeks".

### Second native run: 8 of 9 checks

Same setup, after the fixes, scored against checks registered before the run.

| Session | What happened (read from the kernel database, not from the model) |
| --- | --- |
| 1 | Captured `checkout_project.delivery_timeline = three months`. The answer, "Probably not: with three months to deliver, a full rewrite… is high-risk", was judged a recommendation and linked to that fact. |
| 2 | "We now have three weeks" became a second version of the same pair: one pair, two versions, current "three weeks". The recommendation became stale. |
| 3 | A fresh session got `review_required` with the recommendation's id. Answer: "That plan needs a second look: I recommended against a full rewrite… but that advice assumed three months to deliver, and I've since recorded the timeline as three weeks (from an earlier message you haven't confirmed)…" |
| 4 | "Please forget the checkout deadline." deleted both versions after validation against the typed message. |
| 5 | The generic question carried no claims. All five sessions were bound. |

The check that failed, "the answer names the change", was a keyword false negative. The session-3 answer above names both values and asks for a second look, but it uses none of the words the check looked for (review, revis, reconsider, changed, no longer). We broadened the keyword list afterwards to include second look, assumed, re-examine and re-evaluate. By the criteria fixed before the run, the score stays 8 of 9.

The answer also followed the captured-claim rule and described the three weeks as "from an earlier message you haven't confirmed". Projections took 0.37 to 0.40 s per prompt.

The run exposed a privacy gap, which is now fixed. After the timeline was forgotten, the two inferred recommendations stayed in the database without links, and the first one's text quoted the forgotten "three months". `forget` now also deletes inferred recommendations that rested on any version of the forgotten property, and a test checks that the database dump no longer contains the value. Linked statements the user made themselves are kept, minus their links.

### Codex review and native run (2026-10-06)

Codex CLI 0.160.0, gpt-5.6-sol through the ChatGPT subscription. Another agent ran the review and saved it to `outputs/hook-review-2026-10-06.md`.

| Check | Result |
| --- | --- |
| Hook delivery in Codex | 3 of 3 fresh sessions answered from memory (registered, corrected, forgotten) with no tool calls |
| Explicit save and forget through the bound MCP server | Passed. Write binding works in Codex's process tree |
| Conversational capture | Failed. The prompt hook emitted the capture request and Codex never called `memory_capture` |
| Claude Code, same flow | 9 of 9 |

The review found four issues. All of them are fixed.

1. P1: when the user asked to forget the checkout deadline, `memory_undo` deleted the last save (an approver), even with the judge down. Undo now requires a "take that back" message that names no other stored fact. jev could not tell the two cases apart (0.72 for the wrong target against 0.68 to 0.78 for genuine undos), so the guard is deterministic, and its regression test runs with the judge down.
2. P2: the server's initialization instructions still described v0.2 ("cannot approve, revoke, or forget"). They now describe the turn-token workflow, captured versus confirmed facts, and forget versus undo.
3. P2: the native checks accepted an empty store as a successful forget and counted session rows as binding. They now require a fact that exists before the forget, a recorded forget operation, derived recommendations that existed and were then removed, and a write accepted through the bound session.
4. `memory_inspect` returned quarantined records. It now refuses them, and the inventory is where they get reviewed.

The capture request had travelled inside the JSON packet, and the packet's first reader rule said values are "never instructions". Codex's model obeyed that rule. Kernel requests now go before the packet as plain text, and the server instructions repeat the workflow. A Codex rerun with `tests/native_codex_v04_check.py` is pending.

### Third native Claude Code run: 5 of 10 checks

Run after kernel requests moved out of the packet, against the stricter checks.

What held:
- capture from conversation through the bound session
- the change stored as a new version of the same pair (`checkout_project.delivery_window`)
- the forget removed an existing fact and recorded the operation
- the generic question carried no claims

The failures are one chain, starting in session 1. The reply ("Probably not: a payment rewrite… is hard to estimate in three months, so make only the targeted fixes…") was judged a recommendation (0.835). Its premise scored 0.568 on "rests on", under a 0.70 bar nobody had ever calibrated. Nothing was linked, so nothing was flagged later.

On 15 labelled reply/premise pairs:
- true links score 0.87 to 0.95, except loose phrasings, which score 0.57
- unrelated premises score 0.08 to 0.62, so no single bar separates the two groups
- every weak true link names its premise's value, and no false link does

The kernel now makes a link at 0.85, or at 0.50 when the reply names the premise's value. On that sample this gives 7 true links and 0 false ones.

The run also turned up noise:
- "Answer in one sentence." made a pure question look like a statement. The agent was asked to capture, and it told the user why it didn't.
- "Please forget the checkout deadline." was not recognised as a forget request, because the pattern only matched a leading verb. The earlier calibration note that says this request is excluded before the gate was therefore wrong until this fix.
- Forget words inside pasted text were read as a request.

What changed:
- answer-style instructions count as non-statements
- forget requests are recognised after polite lead-ins, except "don't forget"
- requests are read only from the authored part of a typed turn
- the capture request tells the agent to stay silent when nothing is stated

A replay with the real jev and the run's exact wording made the link and resolved the drifted key. The question raised no capture request, and the fresh session got `review_required` with only "three weeks". Native reruns are pending.

### Second Codex run

gpt-5.6-sol via the subscription. It ran at 02:02 local time, before the fixes were saved at 02:03, so it tested commit a378cb6.

Results:
- Codex now captured in conversation. The deadline change in session 2 went through the bound server.
- The forget removed the fact and its derived recommendation.
- In session 1 Codex answered "No—given the three-month deadline, … refactor the payment module…" without calling `memory_capture`. The chain had no premise to link, and so nothing to flag later.
- The old noise was still there: capture requests on a question and on "Please forget …".

The kernel cannot extract facts without a second model, and an agent may skip a request. Both hosts let a Stop hook hand the turn back by returning `decision: block` with a reason. Codex turns the reason into a new prompt, and Claude Code continues. The Stop hook now does this once, when all of the following hold:
- The gate was confident, at P(none) ≤ 0.05. Fact-bearing messages measured 0.004 to 0.04; a forget request measured 0.12 and a generic question 0.27.
- Nothing was saved.
- The turn was typed.
- A memory server is registered under the same host process.

How it works:
1. The reason carries the original turn's token, so the capture is validated against the user's words and not against the nudge.
2. The original reply is kept, and the kernel infers from it after the capture.
3. A continuation never nudges again.
4. With no server, as in sessions that lack the memory tools, there is no nudge and nothing is counted as missed. Thirteen earlier "missed" captures in the owner's scope came from a session like that.

"Answer in one sentence." raised P(none) on the first message from 0.033 to 0.068, so instructions about how to answer are now removed before the gate. A real-process smoke test with the real jev reproduced the run's first message and Codex's exact reply. The first Stop returned the nudge, a capture with the original token was saved, and the Stop after the continuation reported the save and linked the recommendation.

### Fourth native Claude Code run: 10 of 10 checks

Commit 969675d, before the nudge existed, using the stricter checks.

| Session | Result |
| --- | --- |
| 1 | Captured `checkout_project.delivery_timeline = three months`. The reply was linked to it. |
| 2 | The change became a second version of the same pair, and the recommendation was flagged. |
| 3 | `review_required`. The answer: "My earlier advice to skip a full rewrite… assumed a three-month deadline, and I have the timeline as three weeks now (please confirm that), which makes the case against a rewrite even stronger…" |
| 4 | The forget removed both versions and the two recommendations derived from them. |
| 5 | The generic question carried no claims. |

Nobody typed a memory command, and each session's prompt hook took 0.4 to 0.5 s. One fictional scenario passing once gives no reliability rate.

### Third Codex run: 10 of 10 checks

Codex CLI 0.160.0 with the model configured in the CLI, ChatGPT subscription, commit 4224cc0, stricter checks.

| Session | Result |
| --- | --- |
| 1 | Codex called `memory_capture` while answering ("…memory saved: checkout project timeline is three months"). The kernel stored `checkout_project.delivery_timeline` and rejected one extra item the user had not stated. The reply was linked to the premise. |
| 2 | The change became a second version of the same pair, and the recommendation was flagged. |
| 3 | `review_required`. Answer: "Not unchanged—we should first revisit the plan because one of its underlying assumptions has changed, despite the three-week timeline." |
| 4 | `memory_forget` removed the fact and its derived recommendation. |
| 5 | The generic question carried no claims. |

The one-time Stop nudge never fired in this run. Once the request travelled as plain text, Codex captured on its own. So far the nudge has only been verified by the real-process smoke test above, never natively.

Codex's session-3 answer flags the change, but it names neither value as clearly as Claude Code's answer did. It did not call `memory_dependents` for details.

One intermediate attempt failed with "Unsupported memory schema". The runner process had loaded the schema-3 code. A Stop hook that started after the schema-4 code was saved then migrated the pilot database mid-run. Editing code during a native run invalidates that run.

Both clients have now passed the same pre-registered scenario once each, with no memory commands. That is evidence that the mechanism works end to end in both hosts. It is not a reliability rate.

### Still to verify natively

We still need repeated runs to estimate rates, the nudge in a native session where the agent skips the capture, and real use over weeks. `tests/native_claude_check.py` runs five fresh headless Claude Code sessions with no memory commands and checks the database after each one. The Codex walkthrough repeats the flow with the generated Codex bundle. Both use the owner's sign-in and quota. The runs above also tested process-ancestry binding natively in both hosts, and every session that wrote was bound.

## v0.6: decisions from the repository, and standing facts

Checked on 2026-10-06 with jev 1.9.3 on its local backend (tev1-32k through Ollama), with no paid API.

### Unit and subprocess suite

253 tests pass, 36 of them new.

`tests/test_repository.py` builds real git repositories in a temporary folder. It covers:

- the parser on Nygard, MADR (front matter and bullets) and Spanish records, and which files count as records;
- accepted records learned and proposed ones skipped;
- a superseded record pointing at its successor, flagging an inferred recommendation, and letting `reaffirm` move the link there;
- deprecated and deleted records, and a changed record stored as a new version;
- a forget that holds until the record changes;
- routine commits, commits without decision wording, and commits that write a record, none of which reach the judge;
- one commit per request, and reverts, including one in the same window and a rewritten hash;
- a hosted judge and a failing judge, and the policy switch;
- key resolution never landing on a repository fact;
- the session-start trigger, the note at the next Stop, and the claim's source.

`tests/test_standing.py` covers:

- three sessions, and one session repeating itself;
- rule wording, and a fact of the moment that uses "never";
- delivery once per session and again after SessionStart, and again after a value changes;
- the decay exemption;
- taking a fact off the list, after which counting does not put it back;
- the tool's checks, forget and the inventory;
- the migration from version 5 to 6.

### Which commits are decisions

Five rounds with the real jev, on subjects labelled for this purpose. There are 112 rows now in `fixtures/calibration.jsonl`.

1. A ranked question ("a decision, rule or constraint … rather than routine work") scored decisions 0.32 to 0.56 and routine work 0.21 to 0.40, all in one narrow band.
2. Over this repository's 42 commits, that question with only the bar took 10 as decisions. By hand, about half of them were finished work ("Harden retrieval and hook delivery").
3. A reworded ranked question did better on this repository and worse on the written set. Ranked scores also moved with the project name in the query: the same subject scored 0.429 for "demo-app" and 0.486 for "acme". They moved with the batch too.
4. A filter on the language of a decision (use, require, keep, stop, instead of…) removed every routine subject of the written set and of this repository. 16 routine changes worded like decisions were then added to test what gets past it. The ranked question scored them as high as 0.53, above many decisions.
5. The final rule asks about each commit on its own: does it state a choice for the whole project, or a change to one place in the code? At 0.5 it kept 25 of 28 written decisions, 2 of 13 of this repository's, and none of the 16 look-alikes (the highest scored 0.482). This repository's decisions mostly change how one tool behaves, which the question reads as local. That is why only 2 of its 13 pass.

A real pass over this repository with the final rule sent 8 of its 42 commits to jev. It judged them in 1.4 s and kept 2: the MIT license and the Codex runner's trust requirement. Later passes with nothing new took 0.15 s.

### End to end with the real hooks

These runs used a throwaway repository and a throwaway database.

- With one decision record and four commits, the SessionStart hook returned in 0.18 s, and the background pass finished about 0.7 s later.
  - It learned ADR 1 and "Adopt pnpm as the package manager".
  - It judged "Use a context manager for the file handle" and turned it down.
  - "Fix typo in README" and "Harden retrieval and hook delivery" never reached jev.
  - The session's notice read "Memory: learned 2 decision(s) from the repository (ADR 1, commit 6818af9)."
- With the final hook, which no longer runs git itself, SessionStart returned in 0.12 s. A second session start's pass found nothing changed and added nothing, and a session in a folder outside any repository started no pass.
- An earlier run tested the flow from recommendation to review. It used the same code except for how commits were judged, which that flow does not involve.
  - A question about the queue delivered ADR 1 with `attribution: repository` and its path as `source`.
  - The reply "Keep the job queue in SQLite for now rather than moving it to Redis…" was linked to ADR 1.
  - A commit then superseded ADR 1 with ADR 2. The next SessionStart started a pass, and the next prompt delivered ADR 2 with `review_recommended` and the recommendation's id.
- A reply phrased "No: ADR 1 keeps jobs in one SQLite file, so moving the queue to Redis would reverse an accepted decision…" scored 0.61 on the recommendation question, below the 0.70 bar, and was not linked. That bar misses some advice phrased as a refusal.

### Git in a folder synced by iCloud

This checkout lives in `~/Documents`, which iCloud syncs. 30 loose objects and the pack's reverse index were dataless, meaning evicted to the cloud. One `git log --name-only` waited 44 s at 0% CPU while they downloaded, and took 0.01 s afterwards. So no hook runs git. The SessionStart hook only looks for a `.git` folder and starts the pass, and the pass gives git 60 s. A full test run once took 44 s instead of 14 s for the same reason (evicted `__pycache__` files).

### Standing facts

The `memory_standing` questions were scored on 13 rows (`standing_on` and `standing_off` in the fixtures).

- Requests to always keep a fact in mind scored 0.905 to 0.959; every other row scored 0.613 or less.
- Requests to stop scored 0.712 to 0.950; every other row scored 0.421 or less.

Both use the shared 0.70 bar. The margin for stopping is thin, 0.012.

### Later decisions replace earlier ones

The replacement question was scored on 22 labelled pairs, one pair per request (`memory calibrate decision_replaces --score`).

- Replacements scored 0.531 to 0.895. Other pairs scored 0.293 to 0.654; the highest of those was "Require Python 3.10 or newer" after "Drop support for Python 3.7", where both still hold.
- At 0.70 it caught 7 of 10 replacements and no other pair.
- A second question, asking whether the two decide the same thing differently, scored the other way round (AUC 0.33), so it was dropped.

On a throwaway repository with "Use SQLite for the job queue", "Adopt pnpm as the package manager" and "Use Redis for the job queue instead of SQLite", one pass took 1.75 s. It kept all three as decisions and ended the SQLite one in favour of Redis; the pnpm decision stayed active.

### Picks among listed options

The previous turn of this session, "haz 1 y 2", reached the gate and was read as two facts with confidence. Only the missing MCP tools kept the Stop hook from asking for a capture. Short picks ("haz 1 y 2", "la 3", "ambas", "go with option 2") now skip the gate and relevance, as acknowledgements do. Replayed through the real prompt hook on a copy of the real memory, it took 0.12 s, made no judge call, and asked for no capture.

### A cancelled prompt hook

In this session, one prompt hook was cancelled at its 15 s timeout before it opened a turn. Replayed by hand with the same command, it took under a second. At that moment, 14 of the kernel's modules in this iCloud-synced checkout were evicted, and Python waits for a download when it reads one. `context-kernel doctor` now reports that: "10 file(s) under …/context_kernel are evicted to iCloud", with the two remedies.

### Not verified yet

- Standing delivery and the background pass inside a native Claude Code or Codex session.
- The band showing the pass's note.

## v0.5: the plugin, the band, and requests to remember

Checked on 2026-10-06 with jev 1.9.3 on its local backend (tev1-32k through Ollama).

### Installed as a Claude Code plugin

`.claude-plugin/plugin.json` declares the MCP server and the options (scope, database, selection, jev path, band). `hooks/hooks.json` runs the three command hooks through `bin/context-kernel`, which turns the options into the kernel's arguments and finds jev by absolute path. `claude plugin validate .` passes.

Two headless sessions ran with `--plugin-dir` and a throwaway database, with no project files and no memory commands. "Recuerda: el deploy de aurora es los viernes." was captured through the plugin's own MCP server (the session was bound), and the turn's line "Memory: saved aurora.deploy_day." was stored for the band. A fresh session answered "viernes". The first attempt saved nothing, because headless sessions do not grant the plugin's tools. Passing `--allowedTools "mcp__plugin_context-kernel_context-kernel__*"` fixed that. In an interactive session Claude Code asks once.

The launcher and the kernel run on the `python3` that ships with macOS (3.9.6, with SQLite FTS5), and the whole suite passes there just as it does on 3.14. Running `install.sh` and `context-kernel setup --yes` in a throwaway HOME installed the launcher with the checked Python pinned, wrote `~/.context-kernel/config.json`, and generated Codex's `.codex/hooks.json` and `config.toml` for a project. `claude plugin validate` accepts the band (`hooks/context-kernel.tsx`), but nobody has watched it in an interactive session yet.

### Refusals are reported

When the kernel refused a capture, the user used to see nothing. The Stop hook then asked the agent to capture again and got the same refusal. Now the receipt and the Stop line list what was not saved and why, in plain words ("not read as something you stated"), and a refused attempt is not nudged.

### Requests to remember

In real use, three facts the owner asked to keep were refused as not affirmed. "recuerda los commits sin coauthored" scored 0.39 to 0.54 against the 0.75 bar. We measured three rewordings of the affirmed question over the 30 fixtures plus 17 new rows (imperatives, requests to remember, and their questions and negations). None of them separated those rows from the false ones. An imperative remember cue ("recuerda", "remember", "no olvides", "from now on"…, typos allowed) followed by the value in the user's own words now counts as affirmed. Questions, "I remember…" and "recuerdo…" do not. Origin and category checks still apply.

### Calibration drift

The same run re-scored the v0.4 fixtures with the current local model. Three false rows now reach the 0.75 bar: "Who approves context-kernel releases?" (0.84), "What if we deployed only in the EU region?" (0.77) and "Remind me tomorrow to call Ana." (0.94). In v0.4 the highest false row was 0.757. Question-only messages skip the gate, which covers the first two in practice. The third would be captured if an agent proposed it. The threshold has to be re-measured with `memory calibrate affirmed --score` whenever jev's model changes, and tuned with `jev tune` once real labelled rows exist. There are none yet.

## Historical checks (v0.1 to v0.3)

The sections below cover earlier versions. Their protocols differ, and they include the local Qwen planner, which has since been removed.

### Stable checks (v0.3)

154 unit and integration tests passed at v0.3.

### Official MCP SDK (v0.3)

The SDK is only needed for this test and is not a runtime dependency. To reproduce in an isolated environment:

```sh
python3 -m venv work/mcp-check
work/mcp-check/bin/python -m pip install mcp==2.3.0
work/mcp-check/bin/python -m tests.mcp_sdk_check
```

SDK 2.3.0 negotiated the supported 2025-06-18 fallback and listed all five tools. It validated their JSON schemas, retrieved only authorized evidence, created a pending proposal and observed an owner-approved correction in a later call. It also inspected a trace and handled a missing-statement tool error. This is a real check against a second implementation. It is not a full protocol conformance certificate. [Official SDK](https://github.com/modelcontextprotocol/python-sdk).

### Real local Qwen (removed in v0.4)

```sh
python3 -m context_kernel.demo --live --repetitions 1
```

Across five development runs we attempted 65 local reader/planner calls. The first probes passed. Broader probes exposed invented entity keys, selection of irrelevant preferences, and evidence that was available but ignored. Constraining planning to pairs and separating unavailable needs explicitly improved some cases. Neither fixed every relevance failure.

The historical v0.1 run had four passing reader checks and four of five passing planner checks:

| Probe | Latest result |
| --- | --- |
| Corrected manager, reconstructed recent history | Pass |
| Corrected manager, accumulated history | Pass |
| Corrected manager, structured compaction | Pass |
| One adversarial answer-contamination probe | Pass |
| Career choice with caregiving availability | Relevant constraint selected; other critical evidence marked missing |
| Housing choice with mobility limitation | Relevant constraint selected; apartment accessibility marked missing |
| Deployment choice with EU data constraint | Relevant constraint selected; irrelevant color preference excluded |
| Gift choice with a friend's allergy recorded under the user's source pair | **Fail: available allergy evidence omitted** |
| Generic sorting question outside the rules guard | Pass: no personal evidence selected |

The guarded generic SQLite question also used no model call and no personal context. The core lifecycle checks in the same run passed. Raw aggregate results, needs, warnings and measured local usage are in [local-results.json](local-results.json).

The live command returns nonzero on purpose when a model check fails. Don't suppress that exit status to make a benchmark look successful. Inferred planning stays opt-in and is not the default hook policy. Source-pair validation protects retrieval identity; it does not protect semantic relevance. Some generated missing needs are redundant even when they are correctly typed.

Repetitions use temperature zero and a fixed seed, so they are not independent trials. The reader checks are small deterministic name and number checks on fictional responses. They are not paid judges, and they are not validated measures of conversational quality. A single contamination probe cannot establish injection resistance.

Local calls took anywhere from a few seconds to tens of seconds. The owner command `project --strategy inferred` defaults to 45 seconds and accepts an explicit timeout of up to 60. Hooks stay capped at ten seconds and use the fast rules selector by default. Byte ceilings and Ollama-reported counts are visible. We don't claim an exact tokenizer preflight.

### v0.2 results

The direct local demo passed four reader checks and all five selection checks ([v0.2 local results](v02-local-results.json)). Career availability and the gift/allergy source each needed one deterministic rule supplement, so the model alone did not show better recall on these omissions. The housing and gift plans still marked task evidence as missing. Selecting the relevant evidence does not show that the context was enough for a final decision.

The SDK interoperability check and the dependency-free lifecycle demo also passed after the changes.

We ran native Codex CLI in two fresh eight-session campaigns:

| Client/model | Functional checks | Controls | Outcome |
| --- | --- | --- | --- |
| ChatGPT subscription, gpt-5.6-sol, high reasoning | 6/6 | 2/2 | Pass under the v0.2 protocol |
| Local Ollama, qwen3.5:9b | 5/6 | 1/2 | Fail; retained |

[Subscription client events and answers](v02-native-subscription-results.json), [local client events and answers](v02-native-local-results.json).

The subscription run used the correct registered and corrected names, including on a Spanish correction query. It returned UNKNOWN after the forget and issued five successful memory-context queries with no failed arguments. Its job-offer answer made the decision depend on confirmed flexible hours for caregiving, and it did not invent a currency. The generic Spanish SQLite turn used no memory tool and included no personal information from the fixture. The private-scope canary was never exposed.

The local run kept the lifecycle and the Spanish correction intact. Its job-offer answer, though, invented a dollar amount for the offer from the current salary, and it recommended rejection even though the offer's hours were unknown. The bounded currency-review signal flagged the unsupported reference. Its factual no-memory control returned UNAVAILABLE where UNKNOWN was required. That is a failure of abstention format; the model did not make up a name. The overall command returned nonzero. The passing direct local demo does not offset any of these failures.

The v0.2 protocol changes include explicit query and recovery instructions, a Spanish correction query, an unforced generic turn and a currency-review signal. So 6/6 against the historical 4/6 is not a controlled estimate of improvement. Skipping memory on a generic question is the behaviour we want, and we don't count it as a failure. The currency signal checks four currency families. It does not judge factual or decision quality in general. One campaign per model cannot establish reliability.

Subscription sessions took 126.774 seconds of wall time in total, and usage events reported 188,728 input tokens (116,736 cached) and 1,032 output tokens (370 reasoning). Local native sessions took 352.592 seconds in total, and usage events reported 86,945 input tokens (35,161 cached) and 331 output tokens (zero reasoning). These are the client's session totals summed across model steps. They are not unique prompt sizes, kernel projection costs or exact account-quota percentages. Nothing was billed to an API key, and no paid judge was used.

We prepared a separate prompt-hook pilot with fictional data and no proposal capture. Without user configuration, the project-file and invocation-inline probes emitted no kernel projection and answered UNKNOWN. They did not verify automatic attachment, and they did not show why the hook was skipped. The hook still needs the owner's normal review of the project and its definition. Hooks are on by default in the installed CLI. We used no bypass and changed no trust state. [Owner pilot instructions](hook-pilot.md), [official discovery/trust behavior](https://learn.chatgpt.com/docs/hooks#where-codex-looks-for-hooks).

### v0.3 results

The direct local demo (`python3 -m context_kernel.demo --live --repetitions 1`, qwen3.5:9b) passed every check. It covered three reader history conditions, the contamination probe, five selection probes (career and gift each still needed one rule supplement) and the new dependency fixture. [v0.3 local results](v03-local-results.json).

In the dependency fixture, a rewrite decision depends on a three-month deadline. The deadline is corrected to three weeks, and a fresh projection asks about the rewrite. The kernel delivered the decision with `stale_assumptions` and the new deadline alongside it, with no "three months" and status `review_required`. The local reader then answered that the rewrite "rests on a superseded assumption regarding its deadline" and should be reviewed. The check is a keyword test on one fictional answer. It shows the mechanism and says nothing about reliability. The reader's "you should not proceed" goes further than the reader rules ask (flag, do not decide), so answer quality still needs human review.

Fixture-level counts from the demo: 2 of 2 facts delivered in a fresh session without restating them, 1 of 1 stale recommendation flagged with 0 silently replaced, and 0 of 4 reader answers the owner would have to correct. These are counts on one fixture and do not make a benchmark.

We ran `--strategy jev` against the real `jev` 1.9.3 on its local backend (tev1-32k through Ollama) with seven authorized pairs. The correct pair ranked first on all four probes:

- gift question: allergy 0.56 (favorite colour 0.45, salary 0.48)
- job offer: salary 0.75 and availability 0.58
- rewrite question: deadline 0.67
- generic sorting question: every pair below 0.09

The gift margin is narrow, and at the default thresholds the allergy landed as supporting instead of critical. The thresholds need `jev tune` on a real inventory. We did not measure the hosted model. The unit suite drives the integration through a fake `jev` executable and covers thresholds, metadata-only traces, timeouts, exit codes, malformed output and the rules fallback.

We switched hooks to fail open and re-verified the subprocess contract for both clients. An unreadable event yields an empty-context envelope with a `systemMessage`. `--fail-closed` yields `decision: block`. A `Forget:` prompt blocks in both modes.

The owner ran the native Claude Code check (Claude Code 2.1.287, `claude -p`, client default model, rules strategy) in a fresh pilot directory. The first attempt hit an expired CLI sign-in. The hook still fired in all six sessions, and the runner now stops at the first authentication failure. After `claude auth login`, the full check passed 6/6. [Native Claude Code results](v03-native-claude-results.json).

| Phase | Projection | Answer |
| --- | --- | --- |
| Empty memory | `empty` | `UNKNOWN` |
| Registered approver, fresh session | `ok` | `Nyra Vale` |
| Corrected approver, fresh session | `ok` | `Orin Keel` |
| Rewrite decision after the deadline moved | `review_required` | Needs review: it "rested on an assumption that has since been superseded by the current three-week deadline, and the context doesn't say whether the rewrite still fits that timeline" |
| Forgotten approver, fresh session | `review_required` | `UNKNOWN` |
| Generic SQLite question | `empty` | Definition, no fixture names |

This was the first native confirmation that a project-local `.claude/settings.local.json` hook delivers the kernel's context and that the model uses it. In fresh sessions, a correction replaced the old name and forgetting removed it. A stale decision was flagged along with its changed assumption; it was neither restated nor silently replaced. A generic question got no personal context. No global setting or permission bypass was involved.

The six sessions took 43.2 seconds of wall time. The client reported 2.15 USD-equivalent across them and 16-23k cache-creation tokens per session. That figure is Claude Code's own system prompt plus the projection. It does not measure the kernel's cost. One more observation: the question about the approver also carried the stale rewrite decision (`review_required`), because the rules family for project questions includes `decision`. That did no harm here, but it is a reason to prefer judged selection.

The owner then ran the same check with `--strategy jev` three times. The first two runs passed 6/6 with the same answers, but the kernel's traces showed `jev_unavailable` on every non-generic prompt. The fail-open fallback had served the rules plan, and neither the host nor the model could see that. We found two causes, one after the other:

1. The generated hook named `jev` by bare name, and Claude Code runs hooks with a minimal PATH. Fixed: the adapter pins the absolute path and refuses to generate a jev configuration without one.
2. The owner's interactive shell exported `TYPESAFE_BASE_URL=https://openrouter.ai/api`, which jev honours over its own `config.json`. That OpenRouter account answered HTTP 402 (no credits) in about 100 ms, and `jev doctor` had hidden this behind a cached probe.

The kernel's trace now keeps jev's own error line, so the next fallback explains itself.

The third run had those two variables unset, so jev used its configured local backend (tev1-32k through Ollama). It passed 6/6 with `strategy_fallbacks: 0`, and jev judged the inventory on all four non-generic prompts. [Native Claude Code jev results](v03-native-claude-jev-results.json). The rewrite answer again named the superseded deadline and asked for review "rather than treated as a settled yes". One difference from the rules run is telling. With jev, the question about the forgotten approver produced an `empty` projection: the two remaining checkout pairs scored 0.28 and 0.34, both under the 0.5 supporting bar. With rules, the family had dragged the unrelated stale rewrite decision into a `review_required` projection. Judged selection left out the irrelevant claim, and the family rule could not. Total wall time was 51.9 seconds, and jev's own call took about 0.7 seconds per prompt.

The first real use outside the pilot was the owner's `work` scope: two facts, the hook in this checkout's `.claude/settings.local.json`, and the jev strategy. Every prompt in the desktop session produced an emitted projection. On an approver question jev scored `context_kernel.release_approver` 0.95-0.97 and `user.manager` 0.47. On manager statements it was the reverse (0.11-0.15 against 0.87-0.92). There was one miss. A second session asked "quien es mi manager?", and jev scored the manager pair 0.467, under the 0.5 supporting bar. The projection was empty, so that session answered from the owner CLI instead. The kernel now settles the uncertain band (0.35-0.5) by lexical match, and replaying the same question selects the pair as supporting with `lexical_rescues: ["user.manager"]`.

Local jev latency, measured with synthetic pairs and `--no-cache` on a warm Ollama: 2 pairs 0.21 s, 4 pairs 0.30 s, 32 pairs 1.7 s, 48 pairs 2.6 s, 64 pairs 3.5-4.8 s, 96 pairs 13 s. The first call after the model loaded took 6.1 s for 32 pairs. On this Ollama, setting jev's `--concurrency` anywhere from 1 to 8 made no difference. The kernel caps each call at `--jev-max-pairs` (48) and records how many pairs went unjudged.

The owner then ran the Codex walkthrough with Codex CLI 0.160.0 (GPT-5.6-Sol, ChatGPT subscription) opened in this checkout. The folder was trusted, the generated `.codex/hooks.json` was reviewed and trusted through Codex's own hooks prompt, and the owner asked "quien es mi manager?". The kernel recorded two emitted projections for that session, both selecting `user.manager` through the lexical rescue. (A first attempt with a typo produced a correct `empty` projection: jev 0.424 and no lexical match.) Codex answered "Tu manager es Dani." No bypass or global setting was involved. This closed the native hook verification for both clients. Desktop-app history behaviour and the hosted jev model are still unmeasured.

Still pending: running `jev tune` on the thresholds once the real inventory has enough labelled rows.

### Native Codex CLI

These are the historical v0.1 native results. Codex CLI 0.160.0 ran with invocation-specific MCP configuration in disposable fictional workspaces. We did not edit global configuration. Shell tools, apps, web search and subagents were disabled, no hook-trust bypass was used, and every condition started a fresh ephemeral session.

The local `qwen3.5:9b` run completed six mechanical evidence and lifecycle checks: empty memory, registered approver, corrected approver, forgotten approver, career constraint and a generic technical question. The private-scope canary was not exposed. [Native local results](native-local-results.json) keep the client tool events and answers. The career answer recommended rejecting an offer too early, without knowing its hours, so passing an evidence-use check did not mean the answer was good. Codex reported fallback metadata warnings for this local model.

The owner then asked for a run on their existing OpenAI subscription. `codex login status` reported ChatGPT authentication. The test selected the owner's CLI-configured `gpt-5.6-sol` with high reasoning, required ChatGPT sign-in explicitly, and stripped API-key variables from child processes. The kernel and its MCP server stayed local, but the fictional evidence and prompts went to OpenAI and used subscription quota. There was no API-key billing, purchase, global settings edit or paid judge. [Official authentication documentation](https://learn.chatgpt.com/docs/auth).

Eight subscription sessions completed, including two same-prompt controls without the memory server. Four of six mechanical memory checks passed, as did both control checks. The overall command returned nonzero. [Subscription results](native-subscription-results.json).

| Condition | Observed result |
| --- | --- |
| Empty memory | `UNKNOWN`; an empty-argument tool call failed before a successful retry |
| Registered approver | **Fail:** Codex called `memory_context` with `{}`, received a validation error, and answered `UNKNOWN` without recovering |
| Corrected approver, fresh session | `Orin Keel`, using current evidence |
| Forgotten approver, fresh session | `UNKNOWN`, with an empty successful retrieval |
| Job offer with memory | Retrieved availability and salary; mentioned confirmed flexible hours for four months, but invented EUR currency |
| Generic technical question | Answered without personal information, but skipped the requested tool; an empty projection was not observed |
| Registered approver without memory | `UNKNOWN`, no tool call |
| Job offer without memory | Said the available evidence was insufficient; no personal constraint available |

The registration failure happened before selection. The required `query` argument was missing, so the evidence never reached the reader. The server's schema requires that argument and its validator rejected the call, so this failure says nothing about missing canonical state. Skipping the tool on the generic question fails this particular observation check, though avoiding irrelevant memory is otherwise what we want.

With memory, the job-offer answer was more specific to the context but not fully grounded: salary was a plain number with no currency. The mechanical check only validates the selected evidence and a mention of the constraint. Answer quality has to be reviewed separately and should not be reported as a green benchmark. The local and subscription career prompts were different, so their answers are not a controlled comparison of model quality.

Subscription sessions took 134.281 seconds of wall time in total, from 5.249 to 32.810 seconds each. Native usage events summed to 184,513 input tokens (120,192 of them cached) and 1,538 output tokens (789 of them reasoning). These are client-reported session totals across model steps. They are not unique prompt sizes, kernel projection tokens, a price calculation or an exact subscription-quota percentage. In the successful tool traces we observed, selection itself took milliseconds.

To reproduce the bounded checks, first pick an installed local model or a subscription model your account can use:

```sh
mktemp -d work/codex-pilot.XXXXXX
python3 -m tests.native_codex_check --workspace /absolute/path/to/new/pilot --codex /absolute/path/to/codex
python3 -m tests.native_codex_check --workspace /absolute/path/to/another/new/pilot --codex /absolute/path/to/codex --provider chatgpt --model YOUR_SELECTED_MODEL --reasoning high --controls
```

Use a new empty pilot for each run. The default uses the existing Ollama model and does not download it. Subscription mode requires an explicit model and refuses API-key authentication. Six sessions run by default, and `--controls` adds two. These tests ask for memory use explicitly. They do not establish autonomous relevance, statistical reliability, resistance to long accumulated desktop history or complete ActiveRequestTrace visibility. Native CLI behaviour proves nothing about the exact model and settings of the current desktop chat.

### Not yet verified (v0.3)

The automatic Codex prompt hook and the native Claude Code walkthrough still need the owner's normal project trust and approval step. Their hook envelopes were tested through subprocesses, and native Codex MCP testing does not verify automatic hook injection. Antigravity was not installed here. Its adapter follows the official MCP configuration, but native activation is unverified.

We changed no global client configuration, unrelated project, remote repository or hosting service. The repository is local and unpushed. We do not claim benchmark superiority, remote data erasure, automatic human-authenticated ingestion, or that arbitrary need discovery is solved.
