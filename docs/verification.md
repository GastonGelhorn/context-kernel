# Verification

Checked on 2026-10-05: macOS arm64, Python 3.14.3, SQLite 3.51.2 with FTS5, Ollama 0.34.4, and the existing `qwen3.5:9b` model. No model was downloaded and no paid model API was used.

## v0.4 results

Checked on 2026-10-05 with jev 1.9.3 on its local backend (tev1-32k through Ollama). No paid API was used for these checks.

**Unit and subprocess suite.** 206 tests pass. Two judges drive them. `tests/fakes.py:FakeJudge` runs in memory. A fake `jev` executable answers `rank`, `ask` and `--dry-run` over a real subprocess. `tests/test_v04.py` covers each phase:

- turn tokens;
- refusal of a server bound to another host process, and of unknown or expired tokens;
- continuation and markup origins;
- each row of the plan's message table:
  - a project constraint without a first-person subject;
  - a relation the user names;
  - another person's allergy, held;
  - a decision typed after a pasted log, captured, while the log's fact is held as `quoted_source`;
  - an unaffirmed fact, rejected;
  - "don't save it", which stores nothing;
- instruction-shaped and oversized values;
- a hosted judge, which stores nothing;
- the trust lattice and a proposal instead of overriding a confirmed value;
- undo only when asked;
- caps counted as omissions;
- a forget that lands during a slow capture, which wins;
- forget, policy and confirmation only with the user's own words;
- truthful receipts and missed-capture accounting;
- acknowledgements that cost no judgment;
- the 90-day digest and 180-day eligibility;
- confirmation by restating;
- the post's scenario with an empty memory. A premise stated in the same turn is linked by inference, and its later change flags the recommendation in a new projection, without its text;
- the bundle for both clients;
- additive migration from v1 and v2 databases.

The Ollama/Qwen planner and its tests were removed.

**Official MCP SDK 2.3.0.** Negotiated 2025-06-18, listed the 16 tools with valid schemas, and retrieved scoped evidence without the private canary. It observed an owner-approved correction and read the inventory. It confirmed that a server no hook ever bound refuses `memory_capture` and `memory_forget` with a forged token.

**Local end-to-end smoke (real processes, real jev).** The steps:

1. The prompt hook issued a token and asked for capture of "We have three months to deliver the checkout project. Should we rewrite its payment module?".
2. A separate `serve` process accepted that token through process-ancestry binding.
3. jev validated `checkout.deadline = three months` as `constraints`, and the fact was captured.
4. The Stop hook reported the save and linked the recommendation in the reply to that fact.

**Calibration with the kernel's own questions** (`memory calibrate KIND --score`, bilingual fixtures in `fixtures/calibration.jsonl`):

| Judgment | Rows | Result |
| --- | --- | --- |
| The message asserts this fact (two-field state) | 30 | All 13 true rows score ≥ 0.762. False rows score ≤ 0.757, the highest being a question ("Who approves…?"). Default 0.75 keeps recall 1.0 with one false positive. [0.60, 0.75) is held as `uncertain`. |
| The message states something worth keeping (1 - P(none)) | 20 | At P(none) < 0.15: recall 1.0, precision 0.91. The one false hit, "Please forget the checkout deadline.", is excluded by the forget check before the gate. |
| The message asks to forget this fact | 6 | True rows 0.94 to 0.98, false rows ≤ 0.60. Default 0.70. |

The same affirmed fixtures rendered as single candidates for `jev tune` separate worse: best threshold 0.58, ECE 0.29, eight rows within ±0.02 of the threshold. The kernel therefore calibrates with its own two-field question; the export for `jev tune` remains available. These are small authored sets, read as a trend, not a benchmark.

**Gate behaviour measured on real prompts.** The instruction score is not a veto: "We have three months… Should we rewrite?" scored 0.82 on "instruction" while stating a fact. A generic question put P(none) at 0.27; fact-bearing messages put it at 0.04 or less. The gate is skipped for acknowledgements, question-only messages, generic questions and forget requests.

**Prompt-hook latency** (ten stored facts, warm Ollama, subprocess with a minimal PATH):

| Prompt | Time |
| --- | --- |
| cold question | 4.06 s |
| fact plus question (gate and relevance) | 4.12 s |
| the same question again (all judgments cached) | 0.33 s |

**First native run (Claude Code 2.1.287, headless, five fresh sessions, no memory commands): 3 of 9 checks.**

Passed:
- all five sessions were bound;
- the agent captured from conversation through the bound MCP server;
- "Please forget the checkout deadline." removed it after validation against the typed message;
- the generic question carried no claims.

Failed, with causes:
- **Key drift.** Session 1 stored `checkout_project.delivery_timeline = three months`. Session 2's agent, which did not see that fact, stored `checkout.deadline = three weeks` as a second, parallel fact. The change never reached the premise, so the recommendation was never flagged, and session 3 saw two disagreeing timelines.
- **Lexical recommendation gate.** It missed "No, … don't rewrite the payment module" in session 1 and fired on "I can't tell you whether to go ahead…" in session 3.
- **Over-generic question guard.** "What is the checkout deadline?" counted as generic because the stored entity is `checkout_project`.
- **Oversized relevance query.** The owner's own hook timed out on a 10 KB pasted prompt and fell back to rules, as designed.

Fixes, each measured with the real local jev before choosing a threshold:
- the capture hint lists related stored facts (lexical, with ids);
- `memory_capture` accepts `replaces`;
- the kernel resolves a new pair to an existing one when jev, on key names only, judges them the same attribute: ≥ 0.70 on 5 of 6 true pairs, every false pair ≤ 0.64, and 0.723 for this exact case;
- recommendations are judged ("does the reply recommend, advise, or decide a course of action"): 0.80 to 0.95 for advice, at most 0.61 for reports, questions and refusals; bar 0.70;
- the judge reads only the authored part of the prompt, at most 1,500 characters;
- a definition question that names stored key words is not generic.

The scenario replayed through the kernel with the real jev and the same drifting keys:

1. the drifting key was resolved to the stored pair, so the change became a second version;
2. the session-1 recommendation was linked;
3. session 3 returned `review_required` with the recommendation's id, and only the current "three weeks" was delivered.

**Second native run (same setup, after the fixes): 8 of 9 pre-registered checks.**

| Session | What happened (read from the kernel database, not from the model) |
| --- | --- |
| 1 | Captured `checkout_project.delivery_timeline = three months`. The answer, "Probably not: with three months to deliver, a full rewrite… is high-risk", was judged a recommendation and linked to that fact. |
| 2 | "We now have three weeks" became a second version of the same pair: one pair, two versions, current "three weeks". The recommendation became stale. |
| 3 | A fresh session got `review_required` with the recommendation's id. Answer: "That plan needs a second look: I recommended against a full rewrite… but that advice assumed three months to deliver, and I've since recorded the timeline as three weeks (from an earlier message you haven't confirmed)…" |
| 4 | "Please forget the checkout deadline." deleted both versions after validation against the typed message. |
| 5 | The generic question carried no claims. All five sessions were bound. |

The one failing check, "the answer names the change", is a keyword false negative. The answer above names both values and asks for a second look, but contains none of the words the check looked for (review, revis, reconsider, changed, no longer). The keyword list was broadened afterwards (second look, assumed, re-examine, re-evaluate). Read the result as 8 of 9 by the criteria fixed before the run.

The model's answer also honoured the captured-claim rule: it called the three weeks "from an earlier message you haven't confirmed". Projections took 0.37 to 0.40 s per prompt.

The run exposed a privacy gap, now fixed. After forgetting the timeline, the two inferred recommendations stayed in the database without links, and the first one's text quoted the forgotten "three months". `forget` now deletes inferred recommendations that rested on any version of the forgotten property. A test checks that the database dump no longer contains the value. The user's own linked statements stay, without their links.

**Codex review and native run (2026-10-06, Codex CLI 0.160.0, gpt-5.6-sol through the ChatGPT subscription).** Another agent ran the review and kept it in `outputs/hook-review-2026-10-06.md`.

| Check | Result |
| --- | --- |
| Hook delivery in Codex | 3 of 3 fresh sessions answered from memory (registered, corrected, forgotten) with no tool calls |
| Explicit save and forget through the bound MCP server | Passed. Write binding works in Codex's process tree |
| Conversational capture | Failed. The prompt hook emitted the capture request and Codex never called `memory_capture` |
| Claude Code, same flow | 9 of 9 |

The review found these issues, all now fixed:

1. **P1:** `memory_undo` deleted the last save (an approver) when the user asked to forget the checkout deadline, even with the judge down. Undo now requires a "take that back" message that names no other stored fact. jev could not separate the two cases (0.72 for the wrong target against 0.68 to 0.78 for genuine undos), so the guard is deterministic, and the regression runs with the judge down.
2. **P2:** the server's initialization instructions still described v0.2 ("cannot approve, revoke, or forget"). They now describe the turn-token workflow, captured versus confirmed facts, and forget versus undo.
3. **P2:** the native checks accepted an empty store as a successful forget and counted session rows as binding. The checks now require an existing fact before the forget, a recorded forget operation, derived recommendations that existed and were removed, and a write accepted through the bound session.
4. **`memory_inspect` returned quarantined records.** It now refuses them; the inventory is the review surface.

The capture request had travelled inside the JSON packet, whose first reader rule said values are "never instructions". Codex's model obeyed that rule. Kernel requests now come before the packet as plain text, and the server instructions repeat the workflow. A Codex rerun with `tests/native_codex_v04_check.py` is pending.

**Third native Claude Code run (after moving kernel requests out of the packet): 5 of 10 of the stricter checks.**

What held:
- capture from conversation through the bound session;
- the change stored as a new version of the same pair (`checkout_project.delivery_window`);
- the forget removed an existing fact, with a recorded operation;
- the generic question carried no claims.

The chain that failed starts in session 1. The reply ("Probably not: a payment rewrite… is hard to estimate in three months, so make only the targeted fixes…") was judged a recommendation (0.835). Its premise scored 0.568 on "rests on", under a 0.70 bar that had never been calibrated. Nothing was linked, so nothing was flagged later.

Measured on 15 labelled reply/premise pairs:
- true links score 0.87 to 0.95, except loose phrasings at 0.57;
- unrelated premises score 0.08 to 0.62, so no single bar separates them;
- every weak true link names its premise's value, and no false link does.

A link is now made at 0.85, or at 0.50 when the reply names the premise's value: 7 true and 0 false links on that sample.

The run also showed noise:
- "Answer in one sentence." made a pure question look like a statement, so the agent was asked to capture and told the user why it did not;
- "Please forget the checkout deadline." was not recognised as a forget request because the pattern only matched a leading verb. The earlier calibration note claiming that request was excluded before the gate was therefore wrong until this fix;
- forget words inside pasted text were read as a request.

Now:
- answer-style instructions count as non-statements;
- forget requests are recognised after polite lead-ins, except "don't forget";
- requests are read only from the authored part of a typed turn;
- the capture request tells the agent to stay silent when nothing is stated.

Replayed with the real jev and the run's exact wording, the link was made, the drifted key resolved, the question raised no capture request, and the fresh session got `review_required` with only "three weeks". Native reruns are pending.

**Still to verify natively.** `tests/native_claude_check.py` runs five fresh headless Claude Code sessions with no memory commands and checks the database after each. The Codex walkthrough repeats the flow with the generated Codex bundle. Both use the owner's sign-in and quota and are left for the owner to start. Until they run, process-ancestry binding is verified in local subprocess tests and observed in the Claude desktop process tree, not in a native headless or Codex session.

## Historical checks (v0.1 to v0.3)

The sections below describe earlier versions. Their protocols differ, and they include the removed local Qwen planner.

### Stable checks (v0.3)

154 unit and integration tests passed at v0.3.

### Official MCP SDK (v0.3)

The test-only SDK is not a runtime dependency. To reproduce in an isolated environment:

```sh
python3 -m venv work/mcp-check
work/mcp-check/bin/python -m pip install mcp==2.3.0
work/mcp-check/bin/python -m tests.mcp_sdk_check
```

SDK 2.3.0 negotiated the supported 2025-06-18 fallback, listed all five tools, validated their JSON schemas, retrieved only authorized evidence, created a pending proposal, observed an owner-approved correction in a subsequent call, inspected a trace, and handled a missing-statement tool error. This is a real cross-implementation check, not a full protocol conformance certificate. [Official SDK](https://github.com/modelcontextprotocol/python-sdk).

### Real local Qwen (removed in v0.4)

```sh
python3 -m context_kernel.demo --live --repetitions 1
```

Across five development runs, 65 local reader/planner calls were attempted. Initial probes passed; broader probes exposed invented entity keys, irrelevant preference selection, and ignored available evidence. Pair-constrained planning and explicit separation of unavailable needs improved some cases. They did not fix every relevance failure.

The historical v0.1 run has four passing reader checks and four of five passing planner checks:

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

The guarded generic SQLite question additionally used no model call and no personal context. Core lifecycle checks in the same run passed. Raw aggregate results, needs, warnings, and measured local usage are in [local-results.json](local-results.json).

The live command deliberately returns nonzero when a model check fails. Do not suppress that exit status to present a successful benchmark. Inferred planning remains opt-in, not the default hook policy. Existing source-pair validation protects retrieval identity, not semantic relevance. Some generated missing needs are redundant even when correctly typed.

Repetitions use temperature zero and a fixed seed; they are not independent trials. Reader checks are small deterministic name/numeric checks on fictional responses, not paid judges or validated conversational-quality measures. A single contamination probe cannot establish injection resistance.

Measured local calls varied from a few seconds to tens of seconds. Owner `project --strategy inferred` defaults to 45 seconds and permits an explicit timeout up to 60. Hooks remain capped at ten seconds, and use the fast rules selector by default. Byte ceilings and Ollama-reported counts are visible; no exact tokenizer preflight is claimed.

### v0.2 results

The direct local demo passed four reader checks and all five selection checks. [v0.2 local results](v02-local-results.json). Career availability and the gift/allergy source each needed one deterministic rule supplement. The model alone did not demonstrate improved recall on these omissions. Housing and gift plans still marked missing task evidence; successful relevance selection does not establish sufficient context for a final decision.

The SDK interoperability check and dependency-free lifecycle demo also passed after the changes.

Native Codex CLI was exercised in two fresh eight-session campaigns:

| Client/model | Functional checks | Controls | Outcome |
| --- | --- | --- | --- |
| ChatGPT subscription, gpt-5.6-sol, high reasoning | 6/6 | 2/2 | Pass under the v0.2 protocol |
| Local Ollama, qwen3.5:9b | 5/6 | 1/2 | Fail; retained |

[Subscription client events and answers](v02-native-subscription-results.json), [local client events and answers](v02-native-local-results.json).

The subscription run used the correct registered and corrected names, including a Spanish correction query, returned UNKNOWN after forgetting, and issued five successful memory-context queries without failed arguments. The job-offer answer conditioned the decision on confirmed flexible hours for caregiving and did not invent a currency. The generic Spanish SQLite turn used no memory tool and included no fixture personal information. No private-scope canary was exposed.

The local run preserved the lifecycle and Spanish correction, but its job-offer answer invented a dollar amount for the offer from the current salary and recommended rejection despite unknown offer hours. The bounded currency-review signal flagged the unsupported reference. Its factual no-memory control returned UNAVAILABLE instead of the required UNKNOWN; that is an abstention-format failure, not a fabricated name. The overall command returned nonzero. These failures are not hidden by the passing direct local demo.

The v0.2 protocol changes include explicit query/recovery instructions, a Spanish correction query, an unforced generic turn, and a currency-review signal. Therefore 6/6 versus the historical 4/6 is not a controlled estimate of improvement. Avoiding memory on a generic question is desirable, not a failed product behavior. The currency signal checks four currency families and is not a general factual/decision-quality judge. One campaign per model cannot establish reliability.

Subscription session wall time totaled 126.774 seconds; usage events reported 188,728 input tokens (116,736 cached) and 1,032 output tokens (370 reasoning). Local native session wall time totaled 352.592 seconds; usage events reported 86,945 input tokens (35,161 cached) and 331 output tokens (zero reasoning). These are aggregate client session figures across model steps, not unique prompt sizes, kernel projection costs, or exact account quota percentages. No API-key model billing or paid judge was used.

The separate prompt-hook pilot was prepared with fictional data and no proposal capture. Project-file and invocation-inline probes without user configuration emitted no kernel projection and answered UNKNOWN; they did not verify automatic attachment or establish a specific skip reason. The hook still requires the owner's normal project/definition review. Hooks are enabled by default in the installed CLI; no bypass or trust-state mutation was used. [Owner pilot instructions](hook-pilot.md), [official discovery/trust behavior](https://learn.chatgpt.com/docs/hooks#where-codex-looks-for-hooks).

### v0.3 results

The direct local demo (`python3 -m context_kernel.demo --live --repetitions 1`, qwen3.5:9b) passed every check: three reader history conditions, the contamination probe, five selection probes (career and gift each still needed one rule supplement), and the new dependency fixture. [v0.3 local results](v03-local-results.json).

The dependency fixture: a rewrite decision depends on a three-month deadline; the deadline is corrected to three weeks; a fresh projection asks about the rewrite. The kernel delivered the decision with `stale_assumptions`, the new deadline alongside, no "three months", status `review_required`. The local reader then answered that the rewrite "rests on a superseded assumption regarding its deadline" and should be reviewed. The check is a keyword test on one fictional answer; it shows the mechanism, not reliability. The reader's "you should not proceed" goes further than the reader rules ask (flag, do not decide); answer quality still needs human review.

Fixture-level metrics reported by the demo: 2 of 2 facts delivered in a fresh session without restating; 1 of 1 stale recommendation flagged, 0 silently replaced; 0 of 4 reader answers the owner would have to correct. These are counts on one fixture, not a benchmark.

`--strategy jev` was exercised against the real `jev` 1.9.3 on its local backend (tev1-32k through Ollama) with seven authorized pairs. The correct pair ranked first on all four probes: allergy 0.56 for the gift question (favorite colour 0.45, salary 0.48), salary 0.75 and availability 0.58 for the job offer, deadline 0.67 for the rewrite question, and every pair below 0.09 for a generic sorting question. The gift margin is narrow and the allergy landed as supporting rather than critical at the default thresholds. Thresholds need `jev tune` on a real inventory; the hosted model was not measured. The unit suite drives the integration through a fake `jev` executable: thresholds, metadata-only traces, timeouts, exit codes, malformed output, and the rules fallback.

Hooks were switched to fail open. The subprocess contract was re-verified for both clients: an unreadable event yields an empty-context envelope with a `systemMessage`, `--fail-closed` yields `decision: block`, and a `Forget:` prompt blocks in both modes.

The owner ran the native Claude Code check (Claude Code 2.1.287, `claude -p`, client default model, rules strategy) in a fresh pilot directory. A first attempt hit an expired CLI sign-in: the hook still fired in all six sessions, and the runner now stops at the first authentication failure. After `claude auth login`, the full check passed 6/6. [Native Claude Code results](v03-native-claude-results.json).

| Phase | Projection | Answer |
| --- | --- | --- |
| Empty memory | `empty` | `UNKNOWN` |
| Registered approver, fresh session | `ok` | `Nyra Vale` |
| Corrected approver, fresh session | `ok` | `Orin Keel` |
| Rewrite decision after the deadline moved | `review_required` | Needs review: it "rested on an assumption that has since been superseded by the current three-week deadline, and the context doesn't say whether the rewrite still fits that timeline" |
| Forgotten approver, fresh session | `review_required` | `UNKNOWN` |
| Generic SQLite question | `empty` | Definition, no fixture names |

This is the first native confirmation that a project-local `.claude/settings.local.json` hook delivers the kernel's context and that the model uses it: a correction replaced the old name in a fresh session, forgetting removed it, a stale decision was flagged with its changed assumption rather than restated or silently replaced, and a generic question received no personal context. No global setting or permission bypass was involved. Six sessions took 43.2 seconds of wall time; the client reported 2.15 USD-equivalent across them and 16-23k cache-creation tokens per session, which is Claude Code's own system prompt plus the projection, not the kernel's cost. One observation: the question about the approver also carried the stale rewrite decision (`review_required`) because the rules family for project questions includes `decision`; harmless here, but a reason to prefer judged selection.

The owner then ran the same check with `--strategy jev` three times. The first two passed 6/6 with the same answers, but the kernel's traces showed `jev_unavailable` on every non-generic prompt: the fail-open fallback had served the rules plan, invisibly to the host and the model. Two causes, found in order: the generated hook named `jev` by bare name and Claude Code runs hooks with a minimal PATH (fixed: the adapter pins the absolute path and refuses to generate a jev configuration without one); then the owner's interactive shell exported `TYPESAFE_BASE_URL=https://openrouter.ai/api`, which jev honours over its own `config.json`, and that OpenRouter account answered HTTP 402 (no credits) in about 100 ms, which `jev doctor` had masked with a cached probe. The kernel's trace now keeps jev's own error line so the next fallback explains itself.

The third run, with those two variables unset so jev used its configured local backend (tev1-32k through Ollama), passed 6/6 with `strategy_fallbacks: 0`: jev judged the inventory on all four non-generic prompts. [Native Claude Code jev results](v03-native-claude-jev-results.json). The rewrite answer again named the superseded deadline and asked for review "rather than treated as a settled yes". One difference from the rules run is informative: the question about the forgotten approver produced an `empty` projection with jev (scores 0.28 and 0.34 for the two remaining checkout pairs, both under the 0.5 supporting bar), where the rules family had dragged the unrelated stale rewrite decision into a `review_required` projection. Judged selection avoided the irrelevant claim; the family rule could not. Total wall time 51.9 seconds; jev's own call took about 0.7 seconds per prompt.

First real use outside the pilot: the owner's `work` scope, two facts, hook in this checkout's `.claude/settings.local.json`, jev strategy. Every prompt in the desktop session produced an emitted projection. jev scored `context_kernel.release_approver` 0.95-0.97 and `user.manager` 0.47 on an approver question, the reverse (0.11-0.15 against 0.87-0.92) on manager statements. One miss: a second session asked "quien es mi manager?" and jev scored the manager pair 0.467, under the 0.5 supporting bar, so the projection was empty and that session answered from the owner CLI instead. The kernel now settles the uncertain band (0.35-0.5) by lexical match: the same question replayed selects the pair as supporting with `lexical_rescues: ["user.manager"]`.

jev local latency, measured with synthetic pairs and `--no-cache`, warm Ollama: 2 pairs 0.21 s, 4 pairs 0.30 s, 32 pairs 1.7 s, 48 pairs 2.6 s, 64 pairs 3.5-4.8 s, 96 pairs 13 s. The first call after the model loads took 6.1 s for 32 pairs. jev's `--concurrency` made no difference between 1 and 8 on this Ollama. The kernel caps each call at `--jev-max-pairs` (48) and records the unjudged count.

The owner then ran the Codex walkthrough: Codex CLI 0.160.0 (GPT-5.6-Sol, ChatGPT subscription) opened in this checkout, the folder was trusted, the generated `.codex/hooks.json` was reviewed and trusted through Codex's own hooks prompt, and "quien es mi manager?" was asked. The kernel recorded two emitted projections for that session (a typo'd first attempt produced a correct `empty` projection: jev 0.424 and no lexical match), both selecting `user.manager` through the lexical rescue, and Codex answered "Tu manager es Dani." No bypass or global setting was involved. This closes the native hook verification for both clients; desktop-app history behaviour and the hosted jev model remain unmeasured.

Still pending: `jev tune` of the thresholds once the real inventory has enough labelled rows.

### Native Codex CLI

The following paragraphs retain the historical v0.1 native results. Codex CLI 0.160.0 ran with invocation-specific MCP configuration in disposable fictional workspaces. Global configuration was not edited. Shell tools, apps, web search, and subagents were disabled. No hook-trust bypass was used. Every condition started a fresh ephemeral session.

The local `qwen3.5:9b` run completed six mechanical evidence/lifecycle checks: empty memory, registered approver, corrected approver, forgotten approver, career constraint, and generic technical question. The private-scope canary was not exposed. [Native local results](native-local-results.json) retain the client tool events and answers. The career answer prematurely recommended rejecting an offer without knowing its hours; passing an evidence-use check did not imply a good answer. Codex reported fallback metadata warnings for this local model.

The owner then requested evaluation through their existing OpenAI subscription. `codex login status` reported ChatGPT authentication. The test selected the owner's CLI-configured `gpt-5.6-sol` with high reasoning, explicitly required ChatGPT sign-in, and stripped API-key variables from child processes. The kernel and its MCP server stayed local, but fictional evidence/prompts went to OpenAI and subscription quota was consumed. No API-key billing, purchase, global settings edit, or paid judge was involved. [Official authentication documentation](https://learn.chatgpt.com/docs/auth).

Eight subscription sessions completed, including two same-prompt controls without the memory server. Four of six mechanical memory checks and both control checks passed; the overall command returned nonzero. [Subscription results](native-subscription-results.json).

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

The registration failure occurred before selection: the required `query` argument was absent, so evidence never reached the reader. The server's schema requires that argument and its validator rejected the call; this is not evidence of missing canonical state. The generic question's tool omission fails this particular observation check, but avoiding irrelevant memory is otherwise desirable.

The job-offer answer was more context-specific with memory, yet not fully grounded: salary was a plain number with no currency. The mechanical check only validates selected evidence and a constraint mention. Answer quality must be reviewed separately, not reported as a green benchmark. The local and subscription career prompts differed, so their answers are not a controlled model-quality comparison.

Subscription session wall time totaled 134.281 seconds, ranging from 5.249 to 32.810 seconds. Native usage events summed to 184,513 input tokens (including 120,192 cached tokens) and 1,538 output tokens (including 789 reasoning tokens). These are client-reported session totals across model steps, not unique prompt sizes, kernel projection tokens, a price calculation, or an exact subscription-quota percentage. Selection itself took milliseconds in the observed successful tool traces.

To reproduce the bounded checks after explicitly choosing an installed local model or an account-accessible subscription model:

```sh
mktemp -d work/codex-pilot.XXXXXX
python3 -m tests.native_codex_check --workspace /absolute/path/to/new/pilot --codex /absolute/path/to/codex
python3 -m tests.native_codex_check --workspace /absolute/path/to/another/new/pilot --codex /absolute/path/to/codex --provider chatgpt --model YOUR_SELECTED_MODEL --reasoning high --controls
```

Use a new empty pilot for each run. The default uses the existing Ollama model and does not download it. Subscription mode requires an explicit model and refuses API-key authentication. Six sessions run by default; `--controls` adds two. These tests explicitly request memory use and do not establish autonomous relevance, statistical reliability, resistance to long accumulated desktop history, or complete ActiveRequestTrace visibility. Native CLI behavior is not proof of the exact model/settings of the current desktop chat.

### Not yet verified (v0.3)

The automatic Codex prompt-hook and native Claude Code walkthrough still require the owner's normal project trust/approval step. Their hook envelopes were tested through subprocesses; native Codex MCP testing does not verify automatic hook injection. Antigravity was not installed here. Its adapter follows official MCP configuration, but native activation is unverified.

No global client configuration, unrelated project, remote repository, or hosting service was changed. The repository is local and unpushed. There is no claim of benchmark superiority, remote data erasure, automatic human-authenticated ingestion, or solved arbitrary need discovery.
