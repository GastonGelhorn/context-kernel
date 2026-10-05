# Architecture and safety

Three components with separate responsibilities:

| Component | Responsibility |
| --- | --- |
| Kernel (`store`, `compiler`, `capture`, `inference`, `turns`, `binding`) | State, evidence, validity, corrections, trust, consent policy, dependencies, and what may be delivered |
| Judgment client (`judge.py`) | Ask the configured jev backend, apply time budgets, return probabilities; nothing else |
| Host integration (`adapters.py`, `mcp.py`) | Hooks for Claude Code and Codex, the stdio MCP server, generated configuration |

The kernel imports nothing from jevmate. `judge.JevCommand` runs the `jev` executable with text on stdin. The kernel decides failure policy and privacy for each use.

```text
UserPromptSubmit hook (≤ 8 s)                     MCP server (same host process)
  open turn: token, origin, masked excerpt          memory_capture(token, facts)
  bind session to host process ancestry  ─────────▶   turn ∈ sessions bound to my parent?
  gate: does the message state facts? (jev ask)       jev: does the user's text assert this fact?
  select: relevance per pair (jev rank, cached)       category policy, lattice, caps, tombstones
  deliver packet + turn token + capture hint          write captured / quarantined / ask
Stop hook                                          memory_forget / revoke / confirm / reaffirm / policy
  close turn, truthful receipt, coverage              token + interactive origin + jev: does the
  infer premises of a recommendation (jev rank)       user's text ask for this on this fact?
  drop the excerpt once nothing is pending
```

## Data model

The executable schema is `store.SCHEMA` (version 3). Migrations from version 1 and version 2 are copy-based and additive; no row is dropped. Unknown versions are refused.

| Table | Responsibility |
| --- | --- |
| entities, evidence | Scoped keys, labels, aliases; the bounded source sentence and its attribution |
| statements | Entity, property, value, assertion kind, validity, lifecycle, `trust`, `category`, `last_confirmed_at` |
| relations | `corrects`, acyclic `part_of`, acyclic `depends_on` with `provenance` (declared or inferred) |
| proposals | Changes waiting for a yes, such as a capture that differs from a confirmed value |
| plans, projections, operations | Recorded needs; metadata-only traces; operation log |
| sessions | Session id with the hook's process ancestry (pid and start time) |
| turns | Token, origin, masked excerpt (expires), delivered and captured ids, gate count, flags |
| judgments | Cached probabilities keyed by question, model, and digests of state and candidate, never text |
| captures_log | Outcome per proposed fact: captured, quarantined, confirmed, duplicate, needs_confirmation, rejected, omitted, missed |
| tombstones | Last forget or revoke per property; writes whose evidence predates it are refused |
| scopes | Policy: categories, thresholds, auto capture, remote judge allowance |

## Capture

The host agent extracts. The kernel never asks a second generative model to read the conversation. For each proposed `(entity, predicate, value)` the kernel:

1. Requires an open turn of a session bound to the server's host process. A turn of `continuation` or `unknown` origin can only produce quarantined captures.
2. Rejects invalid keys, values over 200 characters, instruction-shaped values, and secrets.
3. Enforces caps (2 per turn, 10 per session, 30 per day). Anything over a cap is logged as omitted, not dropped from metrics.
4. Checks tombstones before and after judging.
5. Asks jev, in one request, whether the authored part of the message asserts the fact and which category it belongs to. Authored text excludes fenced blocks, quoted lines, mail headers, text after a reply marker, and log, JSON or URL lines.
6. Decides from the answers:
   - affirmed at 0.75 or above, interactive origin, category enabled: captured;
   - affirmed but category disabled or origin unverified: quarantined;
   - affirmed only by the pasted part: quarantined as `quoted_source`;
   - affirmed between 0.60 and 0.75: quarantined as `uncertain`;
   - otherwise: rejected.
7. Applies the trust lattice. A capture may correct a captured value. A confirmed value that differs produces a proposal and a one-line question. The same value restated in a typed message promotes the existing statement to confirmed.

The evidence stored is the sentence that best supports the fact, not the message. Excerpts are masked for secrets, capped at 4 KiB, and dropped at Stop unless a stated fact is still uncaptured. After ten minutes they are dropped in any case and the shortfall is logged as `missed`. "Don't remember this" stores neither an excerpt nor a capture.

## Selection and delivery

`rules`, `fts` or `jev` strategies produce a NeedPlan. With jev, every authorized entity/property pair is judged once per distinct question and cached by digest. A pair in the uncertain band (0.35 to 0.5) is kept only with a lexical match. At most 48 pairs are judged per call: mentioned pairs first, then the most recent. If the judge is unavailable or remote, the rules plan is used with a warning. Captures that nobody confirmed for 180 days stop being evidence; the inventory still lists them.

Critical claims must fit whole within the byte budget. The packet carries reader rules, warnings, and the turn marker (token, capture hint, pending captures, privacy notice). Stale claims carry `stale_assumptions`. Stale inferred recommendations whose premises' entities the turn touches, or whose text the question names, appear under `review` as ids only. Before delivery the selected pairs and their relations are revalidated. One retry reuses the recorded plan.

## Dependencies

Declared links (owner CLI, `memory_depend`) and inferred links behave the same way. The inferred candidates for a recommendation are exactly the premises available in that turn: claims delivered by the kernel and facts captured from the same message. Recommendations are stored with `assertion_kind: inference` and never become evidence. Staleness is computed when queried. `reaffirm` follows a premise's full correction chain to its current version.

## Trust boundaries

- Scope and database are fixed by the host configuration, never by prompts or tool arguments.
- Binding is by process ancestry, recorded by the hook and matched by the server. It establishes which session a call can belong to; the token picks the turn. Neither is a cryptographic proof against a process that already runs as the same user.
- Origin tagging (`interactive`, `continuation`, `unknown`) is conservative and heuristic. Only `interactive` turns can authorize deletions, promotions, or policy changes, and only when jev reads the recorded text as asking for that action on that fact.
- Values are data. Reader rules say so, and malicious values cannot change scope, tools, or policy. That does not guarantee a model ignores them.
- `forget` removes every version of the property, orphan evidence, and same-scope plans, traces, proposals, operations, retry markers, turns, and cached judgments. It leaves a tombstone. It does not reach host transcripts, backups, or anything already delivered.
- The SQLite file is mode 0600 and not encrypted.

## Observability

Traces record ids, reasons, exclusions, warnings, per-pair probabilities, cache hits, latency, and delivery state. They never record values or evidence. `memory metrics` reports capture outcomes by reason, which yields precision proxies (undo, quarantine) and coverage proxies (omitted, missed). The Stop receipt tells the user, in one line, what was saved, what is held, and what needs a yes. It also says when a requested forget did not happen.
