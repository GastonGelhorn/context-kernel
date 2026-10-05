# Context Kernel

Local memory that can be corrected, inspected, and withdrawn. Python, SQLite, and FTS5; no runtime dependencies and no paid API calls.

The kernel keeps attributed state outside the model, selects bounded evidence for a question, and explains the selection. A correction creates a new version rather than another competing summary. New sessions and different agents can use the same owner-selected database and scope.

A decision can be linked to the assumptions it rests on. When an assumption is corrected, the decision is not replaced: it is flagged, delivered together with the assumption's current version, and left for the owner to correct, revoke, or reaffirm.

This is v0.3, not a claim that arbitrary personal context or prompt injection is solved. The default selector uses bilingual English/Spanish rules, scoped lexical search, explicit entities, and bounded containment ancestors. Calibrated selection through the `jev` command line and local Qwen planning are optional. The kernel does not replace an agent's existing conversation history.

## Try it

Use Python 3.11 or newer with SQLite FTS5. From this checkout:

```sh
python3 -m context_kernel.demo
python3 -W error::ResourceWarning -m unittest discover -v
```

The demo uses disposable fictional data. It exercises registration, correction, current-state retrieval, selection explanations, and forgetting. It never calls a model unless `--live` is supplied.

For an optional `memory` command, install in your own virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/memory --help
```

No model, package, or external service is downloaded automatically by the kernel. Installing the optional command may download its build tooling.

## Keep real state

```sh
python3 -m context_kernel init
python3 -m context_kernel remember user manager '"Alex"' --evidence 'My manager is Alex.'
```

Use the returned statement ID:

```sh
python3 -m context_kernel correct STATEMENT_ID '"Blair"' --evidence 'My manager is now Blair.'
python3 -m context_kernel --pretty list --history
python3 -m context_kernel --pretty project 'Who is my manager?' --strategy fts
python3 -m context_kernel --pretty why PROJECTION_ID
python3 -m context_kernel forget CURRENT_STATEMENT_ID
```

Values are JSON, so strings need JSON quotes. Output is JSON by default; `--pretty` makes owner-command output readable. `--db` and `--scope` go before the subcommand. The default database is `.context-kernel/memory.sqlite` relative to the current directory. Use an absolute database path when sharing it across clients.

Quantities can carry explicit units, currency, and periods:

```sh
python3 -m context_kernel remember user salary 42000 --currency USD --period year --evidence 'My salary is USD 42000 per year.'
python3 -m context_kernel --pretty project '¿Debo aceptar una oferta de trabajo que paga más?'
```

Missing metadata stays null. Old plain numeric values remain compatible and project unknown unit/currency/period fields; language or location does not determine currency. Quantity flags also work with corrections. Currency labels are checked for shape, not certified against a financial standards registry.

`forget` removes **every version of the selected entity/property**, associated orphan evidence, and same-scope derived logs and proposals. It is destructive within that explicitly selected property. It does not erase host transcripts, backups, or forensic disk copies. `revoke` instead retains evidence but makes that statement ineligible for subsequent retrieval.

## Decisions and their assumptions

```sh
python3 -m context_kernel remember checkout deadline '"three months"' --kind project --evidence 'We have three months.'
python3 -m context_kernel remember checkout decision '"Rewrite the payment module before launch"' --kind project --evidence 'We agreed to rewrite.'
python3 -m context_kernel depend DECISION_ID DEADLINE_ID
python3 -m context_kernel correct DEADLINE_ID '"three weeks"' --evidence 'The deadline moved to three weeks.'
python3 -m context_kernel --pretty stale
python3 -m context_kernel --pretty project 'Should we go ahead with the rewrite for checkout?'
```

The projection has status `review_required`; the decision carries `stale_assumptions` naming the superseded deadline and its successor, the new deadline is delivered with it, and the old value is not. The reader rules ask the model to flag the dependency rather than restate or silently replace the decision. Then the owner decides: `correct` or `revoke` the decision, or `reaffirm DECISION_ID` to move its link to the current deadline. `dependents ID` lists what rests on a statement.

Dependencies are declared by the owner and point at an exact version. The kernel never infers them, never rewrites a decision, and never settles which side of a changed assumption is right. Forgetting an assumption removes its links.

## Delivery continuity

```sh
python3 -m context_kernel transition laptop ordered --evidence 'I ordered laptop.'
python3 -m context_kernel transition laptop arrived --evidence 'laptop arrived.'
python3 -m context_kernel --pretty project 'Do I own my laptop?'
python3 -m context_kernel transition laptop returned --evidence 'I returned laptop.'
```

Ordering, receiving, and returning update delivery, ownership, and the pending loop in one transaction. An exact alias can be added with `alias laptop 'the computer'` and resolved with `resolve 'the computer'`. Pronouns are supported only by the bounded delivery grammar, not by a general discourse model.

## Connect an agent

| Client | Integration | Verified here |
| --- | --- | --- |
| Codex | Project prompt hook; optional stdio MCP | Hook verified natively after the owner's trust step; native CLI MCP with Ollama and ChatGPT subscription, failures recorded |
| Claude Code | Project prompt hook; optional stdio MCP | Hook verified natively (headless runner and live desktop sessions); `tests/native_claude_check.py` reruns it on request |
| Antigravity | On-demand stdio MCP | Documented config shape; native client not installed |
| MCP clients | Read/propose tools with fixed scope | Official Python SDK 2.3.0 interoperability |

Generate configuration without modifying any client settings:

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter codex --workspace /absolute/path/project
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter claude --workspace /absolute/path/project
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter antigravity --workspace /absolute/path/project
```

The output names the destination and includes its configuration. Add `--raw` to print just the JSON/TOML content. Review and merge it into the indicated project file; do not overwrite unrelated settings. Approve the hook or server through the client's normal trust flow. Paths are generated for this checkout and Python executable; regenerate them if either moves.

See [client setup](docs/adapters.md) before activation and [native results](docs/verification.md#native-codex-cli) for the scoped Codex checks. Automatic hook activation and desktop request inspection remain unverified; MCP retrieval is not automatic per-turn injection.

Hooks fail open: when the kernel cannot serve memory (local model down, budget overflow, unreadable event), the prompt proceeds without context and the host receives a `systemMessage` saying why. `--fail-closed` blocks the prompt instead. A privacy command typed into the prompt (`Forget: ...`, `Revoke: ...`) always blocks, because the hook must not let the model pretend it was applied.

Hooks are read-only by default. Optional `--proposals` recognizes whole-message commands such as `Remember: user.constraint = "No late meetings"`, plus a small delivery grammar. It never approves a fact. Review with `proposals`, then `approve PROPOSAL_ID` or `reject PROPOSAL_ID` in the owner CLI. General conversation extraction is not automatic.

The bounded grammar also accepts `Recuerda: user.constraint = "No reuniones por la tarde"` and `Corrige: user.constraint = "No reuniones después de las 16"`. Spanish delivery commands include `Pedí laptop.`, `Todavía no llegó.`, `Al final llegó.`, and `Lo devolví.`. These remain proposals, not automatically accepted facts. See the [prepared hook pilot](docs/hook-pilot.md) for the separate owner-trust step.

MCP exposes `memory_context`, `memory_inspect`, `memory_status`, `memory_why`, and `memory_propose`. It cannot approve, revoke, forget, choose another scope, execute a shell command, or change permissions. Those restrictions do not protect the database from an agent that separately has filesystem or shell access.

## Optional calibrated selection with jev

With [jevmate](https://github.com/GastonGelhorn/jevmate) installed (`jev` on the PATH, its own backend and key configured):

```sh
python3 -m context_kernel --pretty project 'Would a snack hamper be a good gift for my friend?' --strategy jev
python3 -m context_kernel adapter claude --workspace /absolute/path/project --strategy jev --raw
```

Every authorized entity/property pair is judged against the question (`Is the candidate a fact that someone answering the query must take into account?`). Pairs at or above `--jev-critical` (0.6) become critical needs, at or above `--jev-supporting` (0.5) supporting ones. A pair in the uncertain band (`--jev-band` 0.35 up to supporting) is kept as supporting only when the question lexically mentions it, and the trace lists it under `lexical_rescues`; the rest are excluded. The trace records one probability per pair, never a value. If `jev` is missing, fails, or times out, the rules plan is used and the trace carries `jev_unavailable`.

Measured with the local backend (tev1-32k through Ollama, warm): about 0.2 s for 2 pairs, 1.7 s for 32, 2.6 s for 48; the first call after Ollama loads the model costs several seconds, and above roughly 60 pairs latency becomes erratic. Prompt hooks have ten seconds, so `--jev-max-pairs` (48) bounds each call: beyond it the pairs the question mentions are judged first, then the most recently recorded, and the trace carries `jev_inventory_capped` with `unjudged_pairs`.

jev's own configuration decides where the inventory goes: a local backend keeps it on this machine; a hosted backend sends it to that vendor and costs money. Environment variables such as `TYPESAFE_BASE_URL` override jev's config file and are inherited by the host's hook processes, so check `jev doctor` from the shell that launches the agent. The kernel never installs, configures, or authenticates jev; the generated hook pins the absolute `jev` path because hosts run hooks with a minimal PATH, and a jev failure is traced with jev's own error line. Measured here with the local backend: the correct pair ranked first on all four probes, with margins as narrow as 0.56 versus 0.48, so tune the thresholds for your inventory with `jev tune` before relying on them. See [verification](docs/verification.md#v03-results).

## Optional local planning

With the existing `qwen3.5:9b` model loaded by your own Ollama server:

```sh
python3 -m context_kernel --pretty project 'Should I accept this job offer?' --strategy inferred
python3 -m context_kernel.demo --live --repetitions 3
```

Only loopback HTTP is allowed; redirects and environment proxies are disabled. The request uses `think: false`, an 8,192-token context configuration, a conservative input byte ceiling, and bounded structured output. Failures produce explicit warnings rather than fabricated evidence. The live demo reports complete local message counts, bytes, hashes, and Ollama token counts, not only projection size. Repeated runs use a fixed seed and do not establish statistical independence.

v0.2 supplements valid inferred plans with bounded deterministic family rules and records their contribution as `rule_supplements`. This can recover a known allergy omission; it does not demonstrate that the model discovered an unknown dependency. The live demo still returns nonzero on a missed model check. See the verification report before treating inferred planning as reliable.

The planner proposes needs using authorized inventory values. Available needs must reference existing source pairs; missing needs remain explicitly unavailable. This prevents invented keys from looking like successful retrieval, but does not prove relevance or completeness. Keep the default rules selector unless local checks support enabling inference for your use case.

## Boundaries

- One trusted local owner; scopes are application filters, not operating-system security boundaries.
- Evidence is an attributed statement, not automatically verified truth. Hypotheses and inferences are not current retrieval evidence.
- Projections are derived data, capped at 2,048 UTF-8 bytes by default. Critical evidence never gets silently sliced to fit.
- Traces record prepared/emitted/failed state. Host attachment remains `unknown` without an acknowledgement.
- Conflicting active claims remain visible; newest timestamp does not win automatically.
- A local kernel does not make a remote coding agent local. An enabled client may send projected memory to its existing model provider.
- Invalid MCP arguments return structured unavailable/error data with a bounded retry hint. A failed call is never proof that a fact is missing. Successful empty retrieval means no evidence was selected for that query, not that all possible needs were discovered.
- Reader rules and explicit quantity metadata reduce opportunities for guessing; they do not enforce general factual correctness. The native harness includes a narrow currency-review signal, not a general answer judge.
- Dependencies are owner-declared edges between statement versions, not an inferred or generic graph; staleness is computed when queried, never stored as a verdict.
- No embeddings, cloud sync, background ingestion, credential store, generic graph, or autonomous policy learning.

Read [architecture and safety](docs/architecture.md), [the implementation plan](docs/plan.md), and [verification](docs/verification.md) for details.
