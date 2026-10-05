# Architecture and safety

The runtime is an independent Python package. Context Contract Compiler is a conceptual reference, not a dependency. No inherited requirement IDs are assumed to solve natural-language relevance.

```text
Owner CLI -> transactional scoped state -> authorized current view
                                             |
Question -> NeedPlan -> scoped FTS/retrieval -> byte-bounded projection
                                             |
                                revalidate -> hook or MCP -> host
                                             |
                                   metadata-only trace
```

## Data model

The executable schema is `context_kernel/store.py:SCHEMA`. Schema version 1 rejects unsupported databases; there is no destructive automatic migration.

| Table | Responsibility |
| --- | --- |
| entities | Scoped keys, labels, types, and exact aliases |
| evidence | Original bounded source fragment, attribution, optional source reference, recorded timestamp |
| statements | Entity/property/value, evidence, assertion kind, validity interval, lifecycle, replacement link |
| relations | Typed `corrects` and acyclic `part_of`; statement `about` is implicit in entity ownership |
| proposals | Pending, accepted, or rejected owner-reviewed changes |
| plans | Recorded evidence needs and uncertainty codes, not full conversations |
| projections | IDs, selection reasons, exclusions, snapshot, policy, timing, size, delivery status |
| operations | Minimal operation metadata and safe failure codes |
| processed_events | Optional proposal-hook retry deduplication within a scope |

The database has foreign keys and indexes over scope, entity/property/lifecycle, and validity. FTS5 is built in memory from the already-authorized current view; hidden records do not affect corpus ranking. No embedding service or persistent search cache is involved.

## State rules

`user_statement` and `observed` can enter current retrieval. `hypothesis` and `inference` remain stored but excluded. These names express attribution, not calibrated confidence or objective truth. Owner approval accepts an attributed claim, not a proof of it.

Validity is half-open: `valid_from <= as_of < valid_until`; no end means until changed. ISO timestamps without a timezone, and date-only inputs, are explicitly interpreted as UTC. Ambiguous natural dates are not parsed. The injectable clock makes boundary tests deterministic.

Corrections atomically create a statement and a `corrects` relation, close the previous interval, and retain history. Two unrelated statements with conflicting values stay conflicting; chronology alone does not settle them. Future corrections become effective at their specified start; revoking a future replacement does not resurrect its predecessor automatically. Reviewing or replacing a scheduled correction requires an explicit owner decision.

Delivery transitions update three properties together: delivery status, ownership status, and pending-loop state. Repeated equal transitions do not add versions. Exact entity/alias resolution and unique delivery-reference resolution are supported; unrestricted discourse or intention inference is not.

## NeedPlan and selection

The manual rules cover career, delivery, project state, housing, and food/gift constraints in English and Spanish. Predicate aliases are read-time vocabulary, not automatic source rewrites. Explicit entity keys, labels, and aliases narrow selection; ambiguous aliases and unnamed multiple projects ask for clarification. Personal constraints remain eligible across named career/housing choices. Other questions fall back to scoped current-state FTS with stopword removal and a small bilingual vocabulary. A conservative generic-question guard returns no personal context, while contextual explanations such as "our deployment" remain eligible. These are bounded rules, not universal need discovery.

Explicit scoped `part_of` ancestors can add context for a named component up to two levels. Siblings are not traversed. Only relations whose endpoints have eligible current evidence enter the view, and the authorized relation view participates in the snapshot hash. Hidden-scope relations cannot change rankings or snapshots. Parent claims retain their original entity and attribution; the kernel does not manufacture an inferred child claim.

Optional Qwen inference sees a bounded authorized inventory, not evaluator relevance labels. Its structured output selects existing source pairs and separately lists missing needs. Pair validation prevents invented subjects from being treated as available evidence. Missing needs have `unavailable: true` and cannot accidentally match existing claims. The resulting recorded NeedPlan uses the same compiler as the rules and oracle controls. Replay is reproducible from the plan and snapshot, not from independently rerunning a model.

Every critical matched claim must fit as a whole. If the required packet exceeds the byte budget, no partial critical values are injected and the trace reports `insufficient_context`. Optional claims can be excluded with recorded reasons. Missing critical needs produce `incomplete` and warnings; oracle needs do not imply perfect evidence.

Projections carry IDs, values, attribution, evidence IDs, and validity. Original evidence can be inspected on demand. Projections never update the source database. Immediately before delivery, the selected IDs and authorized snapshot are revalidated. Rules/FTS delivery retries one changed snapshot with a fresh projection; repeated changes fail visibly. Inferred delivery does not automatically repeat an expensive local model call.

Schema version 1 remains compatible. A reserved JSON quantity value stores numeric amount plus optional unit, currency, and period. Plain historical numbers retain their values and receive null quantity metadata in projections. Unknown fields are not completed from locale, evidence-adjacent guesses, or model preference. Reader guidance is derived policy, not another source of facts. The narrow currency-review helper flags references absent from projected numeric metadata; it does not certify sentences, bind every claim to evidence, or disambiguate dollar/yen symbols.

Valid inferred plans are supplemented with the same bounded family rules, with `rule_supplements` counted in local usage. This recovers some catalogued omissions without pretending the model discovered a novel need. Malformed plans, inventory overflow, and model failures remain unavailable; the supplement is not a silent success fallback.

## Trust and deletion

Scope and database are fixed by owner startup arguments, not prompt content or MCP tool arguments. Agents can read and propose through MCP but cannot approve or withdraw facts there. The stdio server implements the tools subset of MCP's supported handshake-era revisions and explicitly negotiates 2025-06-18 with newer compatible clients. It does not claim to implement every newer protocol feature. [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).

Agent inspection is restricted to eligible current evidence; it cannot resurface revoked, superseded, expired, future, or hypothetical values by supplying an old ID. The owner CLI can explicitly inspect history. Already-delivered host copies remain outside the kernel's withdrawal boundary.

Invalid tool arguments return `isError: true` and structured `status: unavailable` data with required fields and bounded retry guidance. A successful `empty` projection is distinct from a failed query. The model can still misuse an error; the contract and native checks reduce this risk without guaranteeing compliance. Hooks block visibly on unavailable or insufficient critical context rather than claiming no memory exists.

These controls do not sandbox a process already able to read the database or run the owner CLI. Use separate OS users/permissions if that threat matters. The SQLite file is mode 0600 on POSIX; newly created leaf directories use 0700. The database is not encrypted. Credential-pattern rejection is only a guardrail, not secret detection certification.

`forget` deletes all versions of the selected entity/property, orphan evidence and entities, and all same-scope proposals, plans, traces, retry markers, and operation metadata. A new event records only the removed count. SQLite `secure_delete` is enabled and the kernel uses a rollback journal, not a persistent WAL. Other scopes are untouched. This is managed-content deletion, not forensic erasure, remote-provider deletion, or backup cleanup.

Malicious values cannot change scope, add tools, approve proposals, or gain authority through the kernel. That does not guarantee that a model will ignore their instructions while answering. Capability authorization and answer contamination are tested separately. The current contamination check is one synthetic probe, not a security certification.

## Observability

Projection traces contain no duplicate claim values or evidence fragments. Plans contain source keys and needs; failures contain controlled error messages/codes. Forgotten scope derivatives are invalidated rather than retained for audit convenience.

`prepared` means compiled; `emitted` means locally written; `failed` records failed revalidation. `host_attachment` remains `unknown`. Hook/MCP traces observe the projection, not the full host request. Local demo traces instead measure the complete known message payload, its hash and byte length, plus model-reported token counts. No host transcript is scraped to fabricate a full-request trace.

Ollama calls have no proxy use or redirects and accept only loopback HTTP. Inventory, message bytes, message counts, output tokens, response bytes, and timeouts are bounded. Truncated output, invalid structured responses, context-boundary counts, overflow, or unavailability never become accepted facts. Input byte ceilings are conservative controls, not exact tokenizer accounting.
