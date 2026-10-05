# Context Kernel

Local memory that can be corrected, inspected, and withdrawn. Python, SQLite, and FTS5; no runtime dependencies and no paid API calls.

The kernel keeps attributed state outside the model, selects bounded evidence for a question, and explains the selection. A correction creates a new version rather than another competing summary. New sessions and different agents can use the same owner-selected database and scope.

This is a working v0.1, not a claim that arbitrary personal context or prompt injection is solved. The default selector uses rules and scoped lexical search. Local Qwen planning is optional and still needs relevance checks. The kernel does not replace an agent's existing conversation history.

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

`forget` removes **every version of the selected entity/property**, associated orphan evidence, and same-scope derived logs and proposals. It is destructive within that explicitly selected property. It does not erase host transcripts, backups, or forensic disk copies. `revoke` instead retains evidence but makes that statement ineligible for subsequent retrieval.

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
| Codex | Project prompt hook; optional stdio MCP | Hook envelope and subprocess contract |
| Claude Code | Project prompt hook; optional stdio MCP | Hook envelope and subprocess contract |
| Antigravity | On-demand stdio MCP | Documented config shape; native client not installed |
| MCP clients | Read/propose tools with fixed scope | Official Python SDK 2.3.0 interoperability |

Generate configuration without modifying any client settings:

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter codex --workspace /absolute/path/project
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter claude --workspace /absolute/path/project
python3 -m context_kernel --db /absolute/path/memory.sqlite adapter antigravity --workspace /absolute/path/project
```

The output names the destination and includes its configuration. Add `--raw` to print just the JSON/TOML content. Review and merge it into the indicated project file; do not overwrite unrelated settings. Approve the hook or server through the client's normal trust flow. Paths are generated for this checkout and Python executable; regenerate them if either moves.

See [client setup](docs/adapters.md) before activation. Native two-conversation acceptance remains a manual check, not something the subprocess tests establish.

Hooks are read-only by default. Optional `--proposals` recognizes whole-message commands such as `Remember: user.constraint = "No late meetings"`, plus a small delivery grammar. It never approves a fact. Review with `proposals`, then `approve PROPOSAL_ID` or `reject PROPOSAL_ID` in the owner CLI. General conversation extraction is not automatic.

MCP exposes `memory_context`, `memory_inspect`, `memory_status`, `memory_why`, and `memory_propose`. It cannot approve, revoke, forget, choose another scope, execute a shell command, or change permissions. Those restrictions do not protect the database from an agent that separately has filesystem or shell access.

## Optional local planning

With the existing `qwen3.5:9b` model loaded by your own Ollama server:

```sh
python3 -m context_kernel --pretty project 'Should I accept this job offer?' --strategy inferred
python3 -m context_kernel.demo --live --repetitions 3
```

Only loopback HTTP is allowed; redirects and environment proxies are disabled. The request uses `think: false`, an 8,192-token context configuration, a conservative input byte ceiling, and bounded structured output. Failures produce explicit warnings rather than fabricated evidence. The live demo reports complete local message counts, bytes, hashes, and Ollama token counts, not only projection size. Repeated runs use a fixed seed and do not establish statistical independence.

The current gift/allergy relevance probe still fails. The live demo returns nonzero on a missed model check; the dependency-free core suite and default demo pass. See the verification report before treating inferred planning as reliable.

The planner proposes needs using authorized inventory values. Available needs must reference existing source pairs; missing needs remain explicitly unavailable. This prevents invented keys from looking like successful retrieval, but does not prove relevance or completeness. Keep the default rules selector unless local checks support enabling inference for your use case.

## Boundaries

- One trusted local owner; scopes are application filters, not operating-system security boundaries.
- Evidence is an attributed statement, not automatically verified truth. Hypotheses and inferences are not current retrieval evidence.
- Projections are derived data, capped at 2,048 UTF-8 bytes by default. Critical evidence never gets silently sliced to fit.
- Traces record prepared/emitted/failed state. Host attachment remains `unknown` without an acknowledgement.
- Conflicting active claims remain visible; newest timestamp does not win automatically.
- A local kernel does not make a remote coding agent local. An enabled client may send projected memory to its existing model provider.
- No embeddings, cloud sync, background ingestion, credential store, generic graph, or autonomous policy learning.

Read [architecture and safety](docs/architecture.md), [the implementation plan](docs/plan.md), and [verification](docs/verification.md) for details.
