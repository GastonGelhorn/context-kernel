# Verification

Checked on 2026-10-05: macOS arm64, Python 3.14.3, SQLite 3.51.2 with FTS5, Ollama 0.34.4, and the existing `qwen3.5:9b` model. No model was downloaded and no paid model API was used.

## Stable checks

```sh
python3 -W error::ResourceWarning -m unittest discover -v
python3 -m context_kernel.demo
```

154 unit/integration tests passed. One parameterized regression includes 120 deterministic state/retrieval scenarios (15 variations of eight checks). These are not 120 independent language tasks or a claim of universal exhaustiveness. Six checks validate native evaluation authentication/command boundaries. The 36 new v0.2 checks cover bilingual relevance and proposal grammar, contextual versus generic questions, explicit/ambiguous entities, bounded containment, relation snapshot non-interference, quantities, bounded currency review, explicit unavailable/conflicted/clarification states, trace listing, empty-context minimum budgets, unused-index avoidance, project-work/career disambiguation, and pre-delivery retries.

Coverage includes attribution, temporal boundaries, future and immediate corrections, conflicting claims, excluded hypotheses, history, revocation, scoped non-interference, aliases and ambiguous references, managed forgetting, whole-claim byte budgets, deterministic plan replay, stale proposals, atomic delivery transitions, retry deduplication, pre-delivery changes, hook subprocess contracts, strict JSON, MCP transport recovery, and injected local model failures/limits.

Ollama fault tests use a local HTTP fixture, not a remote service. Truncation, reported context-boundary input, oversized responses, redirects, HTTP failures, malformed plans, and request ceilings fail explicitly. Resource-warning-as-error checks found and fixed connection/HTTP-error cleanup issues.

Editable package installation and the installed `memory --help` entry point also passed.

## Official MCP SDK

The test-only SDK is not a runtime dependency. To reproduce in an isolated environment:

```sh
python3 -m venv work/mcp-check
work/mcp-check/bin/python -m pip install mcp==2.3.0
work/mcp-check/bin/python -m tests.mcp_sdk_check
```

SDK 2.3.0 negotiated the supported 2025-06-18 fallback, listed all five tools, validated their JSON schemas, retrieved only authorized evidence, created a pending proposal, observed an owner-approved correction in a subsequent call, inspected a trace, and handled a missing-statement tool error. This is a real cross-implementation check, not a full protocol conformance certificate. [Official SDK](https://github.com/modelcontextprotocol/python-sdk).

## Real local Qwen

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

## v0.2 results

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

## v0.3 results

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

Still pending: the owner-trusted Codex hook walkthrough, and the same Claude Code check with `--strategy jev`.

## Native Codex CLI

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

## Not yet verified

The automatic Codex prompt-hook and native Claude Code walkthrough still require the owner's normal project trust/approval step. Their hook envelopes were tested through subprocesses; native Codex MCP testing does not verify automatic hook injection. Antigravity was not installed here. Its adapter follows official MCP configuration, but native activation is unverified.

No global client configuration, unrelated project, remote repository, or hosting service was changed. The repository is local and unpushed. There is no claim of benchmark superiority, remote data erasure, automatic human-authenticated ingestion, or solved arbitrary need discovery.
