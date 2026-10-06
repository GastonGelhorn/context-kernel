# Architecture and safety

Context Kernel has three parts, each with its own job.

| Component | Responsibility |
| --- | --- |
| Kernel (`store`, `compiler`, `capture`, `inference`, `turns`, `binding`, `repository`) | State, evidence, validity, corrections, trust, consent policy, dependencies, the decisions a repository records, and what may be delivered |
| Judgment client (`judge.py`) | Asks the configured jev backend for probabilities within a time budget, and does nothing else |
| Host integration (`adapters.py`, `mcp.py`, `plugin.py`, `setup.py`, `hooks/context-kernel.tsx`) | Hooks for Claude Code and Codex, the stdio MCP server, the plugin launcher with its doctor and setup wizard, generated configuration, and the band above the prompt |

The kernel imports nothing from jevmate. `judge.JevCommand` runs the `jev` executable and passes it text on stdin. The kernel sets the failure policy and the privacy rules for each use.

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
SessionStart hook                                  Background pass (memory learn), nobody waits
  bind the session; standing facts due again          decision records: parser; commits: filters + jev
  repository changed? start the pass  ───────────▶    observed facts attributed to the repository
```

## Data model

The schema lives in `store.SCHEMA` (version 6). Migrations from versions 1 to 5 copy tables or add tables and columns, and none of them drops a row. The kernel refuses a database whose version it does not know.

| Table | Responsibility |
| --- | --- |
| entities, evidence | Scoped keys, labels and aliases, plus the bounded source sentence and who said it |
| statements | Entity, property, value, assertion kind, validity, lifecycle, `trust`, `category`, `last_confirmed_at` |
| relations | `corrects`, acyclic `part_of`, acyclic `depends_on` with `provenance` (declared or inferred) |
| proposals | Changes waiting for a yes, such as a capture that differs from a confirmed value |
| plans, projections, operations | Recorded needs, metadata-only traces, and the operation log |
| sessions | Session id with the hook's process ancestry (pid and start time), which standing facts it has received, and a one-line notice waiting for its next Stop |
| turns | Token, origin, masked excerpt (expires), delivered and captured ids, gate count, flags, and `notes` (the Stop hook's memory line) |
| judgments | Cached probabilities keyed by question, model, and digests of state and candidate. Text is never stored here |
| captures_log | Outcome per proposed fact (captured, quarantined, confirmed, duplicate, needs_confirmation, rejected, omitted, missed) and the key it was proposed under (`label`) |
| tombstones | The last forget or revoke per property. A write whose evidence predates it is refused |
| servers | Host processes under which a memory MCP server initialized, which tells the kernel whether anyone can act on a capture request |
| scopes | Policy: categories, thresholds, auto capture, remote judge allowance, repository learning |
| standing | Properties handed to every session. A property is on when it was restated in three sessions, stated as a rule, or asked for, and off when the user took it off |
| repositories, repo_sources | Each repository's entity, signature (HEAD plus the blobs of its decision records) and scan lock; and each record or commit read, at which version, and the statement it became |

## Capture

The host agent does the extraction. The kernel never asks a second generative model to read the conversation. For each proposed `(entity, predicate, value)` the kernel goes through these steps.

1. It requires an open turn in a session bound to the server's host process. A turn whose origin is `continuation` or `unknown` can only produce quarantined captures.
2. It rejects invalid keys, values longer than 200 characters, values shaped like instructions, and secrets.
3. It enforces caps of 2 per turn, 10 per session and 30 per day. A fact over a cap is logged as omitted, so it still shows up in the metrics.
4. It checks tombstones before and after judging.
5. It asks jev, in one request, whether the authored part of the message asserts the fact and which category the fact belongs to. Authored text leaves out fenced blocks, quoted lines, mail headers, anything after a reply marker, and lines of log output, JSON or URLs.
6. It decides from the answers. A fact counts as affirmed when jev scores it at 0.75 or above, or when the user's own text explicitly asks to remember it ("remember", "recuerda", "from now on" and similar, never inside a question) and writes the value after the request.
   - Affirmed, interactive origin, category enabled: captured.
   - Affirmed, but the category is disabled or the origin unverified: quarantined.
   - Affirmed only by the pasted part: quarantined as `quoted_source`.
   - Scored between 0.60 and 0.75: quarantined as `uncertain`.
   - Anything else: rejected.
7. It resolves keys, before the caps and the judgment run. Collection predicates (`decision`, `notes`, …) never take part, because a new decision does not change an earlier one.
   - If the agent passed `replaces`, the fact goes to that stored fact unless jev judges the two key names unrelated (below 0.3).
   - Otherwise, a new pair that shares key words with a stored pair resolves to it when jev judges both to name the same attribute (0.70 or above).
8. It applies the trust lattice.
   - A change the user typed to a captured or confirmed value is stored as a new version. When the old value was confirmed, the receipt names it, as in `user.approver (was "Gaston")`.
   - The kernel opens a proposal and asks a one-line question only when it picked the target itself (a resolved key) or when two values already disagree.
   - When a typed message restates the same value, the existing statement is promoted to confirmed.

As evidence, the kernel keeps the one sentence that best supports the fact. Excerpts are masked for secrets and capped at 4 KiB. The Stop hook drops them unless a stated fact is still uncaptured, and after ten minutes they go anyway, with the shortfall logged as `missed`. When the user says "Don't remember this", the kernel keeps neither an excerpt nor a capture.

## Decisions from the repository

When the session's folder is inside a git work tree (a `.git` above it), the SessionStart hook starts a detached background pass (`memory learn --if-changed`) and returns. The hook itself never runs git, because git can be slow (see verification), and nothing waits for the pass. The pass compares the repository's signature (HEAD plus the blobs of its decision records) with the one from the last pass, and returns at once when nothing changed. It holds a lock on the repository's row for two minutes, so two sessions starting together do not scan twice.

1. Decision records are tracked files under `adr/`, `adrs/`, `decisions/`, `decision-records/` or `architecture-decisions/`. Templates, READMEs and vendored folders are left out, and only records whose blob changed are parsed.
   - The parser reads the title, number, status, decision paragraph and "superseded by" target of Nygard/adr-tools and MADR records, in English or Spanish.
   - An accepted record becomes `observed` evidence on the repository's entity, as `adr_NNNN`, with `source_kind: repository` and the path as its source.
   - A changed record becomes a new version.
   - A superseded record ends and points at its successor, so `reaffirm` can move links there.
   - A deprecated, rejected or deleted record is revoked. Proposed records are not facts.
2. Commits are read from the last 60 non-merge commits of the last 180 days. Some are recorded as seen and never judged: bots, reverts, routine subjects, subjects without the language of a decision, and commits that write a decision record. A revert revokes the decision it reverts. jev judges each remaining subject in its own request, newest first and at most 12 per pass, on whether it is a choice for the whole project or a change to one place in the code. A subject that scores 0.5 or above becomes `decision_<slug>`, valid from the commit's date, with the commit as the source. The same subject under another hash (after a rebase) is not added twice. Accepted commits are stored oldest first. Each one is compared with the earlier commit decisions that share a topic word, one pair per request: at 0.70 on "does the later decision replace the earlier one", the earlier one ends and points at the later one. An older commit judged in a later pass ends at once if a newer decision already replaced it. Commits left unjudged leave the signature unset, so the next session runs another pass.
3. A forget writes a tombstone as usual. The kernel still knows the record's version, so an unchanged record is not read back. A record changed by a commit made after the forget is read again.

Automatic key resolution never targets a repository fact; only an explicit `replaces` does. The pass's note ("learned 2 decision(s) from the repository…") waits on the session until its next Stop says it.

## Standing facts

Each statement or restatement the user typed, once validated, is logged under its property. When the property has been stated in three different sessions since it was last forgotten, it becomes standing. A preference, constraint or decision becomes standing at once when its sentence contains a rule word (always, never, from now on, in every…). `memory_standing` turns one on or off when the user's own words ask for it, and counting never overrides an off.

The prompt hook pins standing facts into the packet, at most 8, whenever the session has not yet received the current set. That happens on the session's first prompt, after SessionStart (start, resume, clear or compact), and after a value changes. Pinned claims carry `standing: true`. Standing facts are exempt from the 180-day cutoff and from the 90-day digest.

## Handing the turn back once

The Stop hook answers `decision: block` when the gate was confident (P(none) ≤ 0.05), the turn was typed, a memory server is registered under the session's host process, and the agent neither captured anything nor tried to. A capture the kernel refused already has its answer, and asking again would only get the same refusal, so a refused attempt is never nudged. The block reason asks the agent to call `memory_capture` with the original turn's token. Codex turns that reason into a new prompt, and Claude Code continues the turn. The original reply is stored masked, and the inference runs on it at the continuation's Stop, after the capture. A continuation never nudges again.

Each MCP server records its host process when it initializes (`servers`), and the Stop hook checks that record. Sessions without memory tools are therefore never nudged, and their gate estimates do not count as missed.

## Requests to the agent

The agent must treat the packet as data and never obey its claims. So the kernel writes its own requests (capture these facts, pending captures, how to forget, do not keep this message) as plain text before the JSON, and the MCP server's instructions describe the same workflow. When the request sat inside the packet, Codex's model honoured "values are data" and never captured anything.

## Selection and delivery

The `rules`, `fts` and `jev` strategies each produce a NeedPlan. With jev, every authorized entity/property pair is judged once per distinct question, and the result is cached by digest. A pair in the uncertain band (0.35 to 0.5) stays only if the question matches it lexically. One call judges at most 48 pairs, mentioned pairs first and then the most recent. If the judge is unavailable or remote, the kernel falls back to the rules plan and adds a warning. A capture that nobody has confirmed for 180 days stops counting as evidence, though the inventory still lists it.

Critical claims must fit whole within the byte budget. The packet carries the reader rules, warnings, and the turn marker (token, capture hint, pending captures, privacy notice). A stale claim carries `stale_assumptions`. A stale inferred recommendation appears under `review`, as an id only, when the turn touches one of its premises' entities or the question names its text. The kernel revalidates the selected pairs and their relations just before delivery, and a single retry reuses the recorded plan.

## Dependencies

Declared links (from the owner CLI or `memory_depend`) and inferred links behave the same way. To infer a link, jev first has to judge the reply a recommendation (0.70). Each premise then needs 0.85 on "rests on", or 0.50 if the reply names that premise's value. The candidates are exactly the premises available in that turn, meaning claims the kernel delivered and facts captured from the same message. Recommendations are stored with `assertion_kind: inference` and never become evidence. Staleness is computed at query time. `reaffirm` follows a premise's whole correction chain to its current version.

## Trust boundaries

- The host configuration fixes the scope and the database. Prompts and tool arguments cannot change either.
- Binding uses process ancestry, which the hook records and the server matches. It decides which session a call can belong to, and the token picks the turn. Neither is a cryptographic proof against a process that already runs as the same user.
- Origin tagging (`interactive`, `continuation`, `unknown`) is a conservative heuristic. Only `interactive` turns can authorize deletions, promotions or policy changes, and only when jev reads the recorded text as asking for that action on that fact.
- Values are data. The reader rules say so, and a malicious value cannot change scope, tools or policy. That still does not guarantee a model will ignore one.
- `forget` removes every version of the property, the inferred recommendations that rested on it (their text can quote it), orphaned evidence, and the scope's plans, traces, proposals, operations, retry markers, turns and cached judgments. It leaves a tombstone. It cannot reach host transcripts, backups, or anything already delivered.
- What the repository says is data attributed to the repository. It is bounded like any other value and refused when it is shaped like an instruction. A teammate's commit can still put a false decision into memory. The commit filters and jev's bar make that rare, not impossible, and the agent sees which file or commit each such claim came from.
- The SQLite file has mode 0600 and is not encrypted.

## Observability

Traces record ids, reasons, exclusions, warnings, per-pair probabilities, cache hits, latency and delivery state. They never record values or evidence. `memory metrics` reports capture outcomes by reason. Undo and quarantine counts stand in for precision, and omitted and missed counts stand in for coverage.

At Stop the user gets one line saying what was saved, what is held, what needs a yes, and what was not saved, with the reason in plain words. The line also says when a requested forget did not happen. The kernel keeps it in `turns.notes`, because the desktop app does not show hook messages. The plugin's band reads it through `activity()` and draws it above the prompt, with an undo for whatever that turn saved.
