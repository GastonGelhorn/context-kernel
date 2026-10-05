# Verification

Checked on 2026-10-05: macOS arm64, Python 3.14.3, SQLite 3.51.2 with FTS5, Ollama 0.34.4, and the existing `qwen3.5:9b` model. No model was downloaded and no paid model API was used.

## Stable checks

```sh
python3 -W error::ResourceWarning -m unittest discover -v
python3 -m context_kernel.demo
```

83 unit/integration tests passed. One parameterized regression includes 120 deterministic state/retrieval scenarios (15 variations of eight checks). These are not 120 independent language tasks or a claim of universal exhaustiveness.

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

## Not yet verified

The native Codex and Claude Code two-conversation walkthrough requires the owner's project trust/approval step. Their hook envelopes were tested through subprocesses, not through paid native model runs. Antigravity was not installed here. Its adapter follows official MCP configuration, but native activation is unverified.

No global client configuration, unrelated project, remote repository, or hosting service was changed. The repository is local and unpushed. There is no claim of benchmark superiority, remote data erasure, automatic human-authenticated ingestion, or solved arbitrary need discovery.
