# Verification

Checked on 2026-10-05: macOS arm64, Python 3.14.3, SQLite 3.51.2 with FTS5, Ollama 0.34.4, and the existing `qwen3.5:9b` model. No model was downloaded and no paid model API was used.

## Stable checks

```sh
python3 -W error::ResourceWarning -m unittest discover -v
python3 -m context_kernel.demo
```

89 unit/integration tests passed. One parameterized regression includes 120 deterministic state/retrieval scenarios (15 variations of eight checks). These are not 120 independent language tasks or a claim of universal exhaustiveness. Six additional checks validate the native evaluation's authentication/command boundaries and distinguish a failed tool call from successful retrieval without launching a model.

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

The latest recorded run has four passing reader checks and four of five passing planner checks:

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

## Native Codex CLI

Codex CLI 0.160.0 ran with invocation-specific MCP configuration in disposable fictional workspaces. Global configuration was not edited. Shell tools, apps, web search, and subagents were disabled. No hook-trust bypass was used. Every condition started a fresh ephemeral session.

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
