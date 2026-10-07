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
  open turn: token, origin, masked excerpt          memory_capture(token, facts with quote and cues)
  a second install's copy of the same prompt: skip    turn ∈ sessions bound to my parent?
  model not loaded? keywords now, warm up behind      quote ⊂ the user's own words?
  gate: does the message state facts? (jev ask)       jev: asserts it? joking? asking for work?
  select: the pairs the time allows (jev rank)        safe mode while the judge is unverified
  deliver packet + turn token + capture hint          category policy, lattice, caps, tombstones
Stop hook                                          memory_forget / revoke / confirm / reaffirm / policy
  close turn (once), truthful receipt, coverage       token + interactive origin + jev: does the
  infer premises of a recommendation (jev rank)       user's text ask for this on this fact?
  drop the excerpt once nothing is pending
SessionStart hook                                  Background pass (memory warm), nobody waits
  bind the session; standing facts due again          load the model, keep it 30 min (Ollama)
  start the pass  ───────────────────────────────▶    canary when the judge changed or is new
                                                      repository changed? decision records: parser
```

## Data model

The schema lives in `store.SCHEMA` (version 6). Migrations from versions 1 to 5 copy tables or add tables and columns, and none of them drops a row. Additive changes since then (the `cues` column, the `judges` table) keep version 6 and are added in place when a database is opened, so an older kernel sharing the file (an installed plugin next to a checkout) keeps reading and writing it. The kernel refuses a database whose version it does not know.

| Table | Responsibility |
| --- | --- |
| entities, evidence | Scoped keys, labels and aliases, plus the bounded source sentence and who said it |
| statements | Entity, property, value, assertion kind, validity, lifecycle, `trust`, `category`, `last_confirmed_at`, `cues` (the words a later question would use, from the agent that captured it) |
| relations | `corrects`, acyclic `part_of`, acyclic `depends_on` with `provenance` (declared or inferred) |
| proposals | Changes waiting for a yes, such as a capture that differs from a confirmed value |
| plans, projections, operations | Recorded needs, metadata-only traces, and the operation log |
| sessions | Session id with the hook's process ancestry (pid and start time), which standing facts it has received, and a one-line notice waiting for its next Stop |
| turns | Token, origin, masked excerpt (expires), delivered and captured ids, gate count, flags, and `notes` (the Stop hook's memory line) |
| judgments | Cached probabilities keyed by question, model, and digests of state and candidate. Text is never stored here |
| captures_log | Outcome per proposed fact (captured, quarantined, confirmed, duplicate, needs_confirmation, rejected, omitted, missed) and the key it was proposed under (`label`, cleared when the property is forgotten); later, whether the user took a capture back (undone, forgotten, revoked, corrected; `soon` within the regret window) |
| judges | Per scope and judge (backend, model, weights digest): whether it passed the calibration canary with the scope's thresholds |
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
5. When the agent cites the user's words (`quote`), they must be in the authored part of the message: case, accents, quote marks and spacing are ignored, and four in five of the quote's words in one sentence also match. A quote found only in the pasted part is someone else's (`quoted_source`); a quote found nowhere is refused (`quote_not_found`). The cited sentence becomes the evidence.
6. It asks jev, in one request, whether the authored part of the message asserts the fact, which category the fact belongs to, whether the writer is joking about it, and whether they say it used to be true and has since stopped being true. Only when the sentence stating the fact opens with a work verb does a second request ask whether the writer is asking for a piece of work rather than stating how things are. Authored text leaves out fenced blocks, quoted lines, mail headers, anything after a reply marker, and lines of log output, JSON or URLs.
7. It decides from the answers. A fact counts as affirmed when jev scores it at 0.75 or above, or when the user's own text explicitly asks to remember it ("remember", "recuerda", "from now on" and similar, never inside a question) and writes the value after the request. When the whole message lands in the band (0.60 to 0.75) and the agent cited a sentence of it, that sentence alone is judged too and decides: a constraint stated next to a question ("Tenemos cinco semanas hasta el lanzamiento. ¿Pasamos los PDF…?") read 0.64 to 0.75 as a whole and 0.81 to 0.94 as a sentence, while "Our budget is 20k EUR. Just kidding…" never qualifies (0.06 as a whole, 0.96 as a sentence).
   - Affirmed, interactive origin, category enabled: captured, unless one of the holds below applies.
   - Held as `joke`: the sarcasm score is 0.60 or above, or 0.45 or above when the message carries a joke's own markers (jaja, lol, "como si", "yeah right"). Jokes scored 0.65 to 0.73 on the calibration rows and true statements 0.33 at most; "Sí, claro, como tenemos presupuesto infinito… jaja" scored 0.588 and, before the markers counted, replaced a real budget in the benchmark.
   - Held as `past_state`: jev scores it 0.70 or more as something that used to be true and has stopped. "Hasta agosto el deploy era manual" reads as asserted (0.865) and as stopped at 0.767; the six past states measured scored 0.76 or more, and 77 of 78 true statements (calibration, held-out and benchmark rows) 0.46 or less, the 78th 0.67. Asking whether a fact is "true now" instead held future dates as past ("the partner portal launches on March 3": 0.37).
   - Held as `task_request`: the sentence stating the fact opens with a work verb (write, add, genera, explica…) and has no rule word (always, from now on…) and no request to remember, and the second request scores it 0.60 or above as a request for work. Neither signal alone: a fact stated next to a question reached 0.68 on the work question.
   - Held as `unprompted`: the hook's gate judged that the message stated nothing, the agent proposed the fact anyway, and jev scored it below 0.90.
   - Held as `judge_unverified`: safe mode (below).
   - Affirmed, but the category is disabled or the origin unverified: quarantined.
   - Affirmed only by the pasted part: quarantined as `quoted_source`.
   - Scored between 0.60 and 0.75: quarantined as `uncertain`.
   - Anything else: rejected.
8. It resolves keys, before the caps and the judgment run. Collection predicates (`decision`, `notes`, …) never take part, because a new decision does not change an earlier one.
   - If the agent passed `replaces`, the fact goes to that stored fact unless jev judges the two key names unrelated (below 0.3).
   - Otherwise, a new pair that shares key words with a stored pair resolves to it when jev judges both to name the same attribute (0.70 or above).
9. It stores the agent's cues with the fact (at most 8 per capture, 40 characters each, no instruction-shaped or secret-shaped text). A new version keeps the cues of the one it replaces, and a restatement in other words adds its cues (up to 16 kept).
10. It applies the trust lattice.
   - A change the user typed to a captured or confirmed value is stored as a new version. When the old value was confirmed, the receipt names it, as in `user.approver (was "Gaston")`.
   - The kernel opens a proposal and asks a one-line question only when it picked the target itself (a resolved key) or when two delivered values already disagree. A held reading of the same attribute is no disagreement: a value the user then states plainly replaces it.
   - When a typed message restates the same value, the existing statement is promoted to confirmed.

### Safe mode

Thresholds are measured per model. `calibration.CANARY` holds fourteen rows the shipped local model puts well clear of each bar (true statements at 0.86 to 0.97 against 0.75, non-statements at 0.02 to 0.25 against 0.60, a joke at 0.73 against 0.60, a past and a current state at 0.97 and 0.01 against 0.70, two gate rows). The session-start background pass runs them with the scope's thresholds whenever the judge (backend, model name, weights digest) has no verdict yet, and records it in `judges`. A judge that got a row wrong, or one that replaced a judge which had passed and is not checked yet, saves nothing: every capture is held for review (`judge_unverified`) until `memory calibrate --check` passes. A judge on first use runs with the shipped thresholds while its check runs. Changing a threshold clears the verdicts. A check the judge could not answer leaves no verdict, so the next warm-up tries again.

As evidence, the kernel keeps the one sentence that best supports the fact (the cited one, when the agent gave a quote). Excerpts are masked for secrets and capped at 4 KiB. The Stop hook drops them unless a stated fact is still uncaptured, and after ten minutes they go anyway, with the shortfall logged as `missed`. When the user says "Don't remember this", the kernel keeps neither an excerpt nor a capture.

## Decisions from the repository

When the session's folder is inside a git work tree (a `.git` above it), the SessionStart hook starts a detached background pass (`memory warm --learn`, which also loads the judge) and returns. The hook itself never runs git, because git can be slow (see verification), and nothing waits for the pass. The pass compares the repository's signature (HEAD plus the blobs of its decision records) with the one from the last pass, and returns at once when nothing changed. It holds a lock on the repository's row for two minutes, so two sessions starting together do not scan twice.

1. Decision records are tracked files under `adr/`, `adrs/`, `decisions/`, `decision-records/` or `architecture-decisions/`. Templates, READMEs and vendored folders are left out, and only records whose blob changed are parsed.
   - The parser reads the title, number, status, decision paragraph and "superseded by" target of Nygard/adr-tools and MADR records, in English or Spanish.
   - An accepted record becomes `observed` evidence on the repository's entity, as `adr_NNNN`, with `source_kind: repository` and the path as its source.
   - A changed record becomes a new version.
   - A superseded record ends and points at its successor, so `reaffirm` can move links there.
   - A deprecated, rejected or deleted record is revoked. Proposed records are not facts.
2. Commits are read only when the scope turns them on (`memory policy --repository-commits on`); decision records are the default source. On this repository the commit pass had kept three subjects as project decisions that nobody had decided for the whole project. Turning commits off retires the decisions read from them without a tombstone and forgets which commits were seen, so turning them back on reads them again. When on, commits are read from the last 60 non-merge commits of the last 180 days. Some are recorded as seen and never judged: bots, reverts, routine subjects, subjects without the language of a decision, and commits that write a decision record. A revert revokes the decision it reverts. jev judges each remaining subject in its own request, newest first and at most 12 per pass, on whether it is a choice for the whole project or a change to one place in the code. A subject that scores 0.5 or above becomes `decision_<slug>`, valid from the commit's date, with the commit as the source. The same subject under another hash (after a rebase) is not added twice. Accepted commits are stored oldest first. Each one is compared with the earlier commit decisions that share a topic word, one pair per request: at 0.70 on "does the later decision replace the earlier one", the earlier one ends and points at the later one. An older commit judged in a later pass ends at once if a newer decision already replaced it. Commits left unjudged leave the signature unset, so the next session runs another pass.
3. A forget writes a tombstone as usual. The kernel still knows the record's version, so an unchanged record is not read back. A record changed by a commit made after the forget is read again.

Automatic key resolution never targets a repository fact; only an explicit `replaces` does. The pass's note ("learned 2 decision(s) from the repository…") waits on the session until its next Stop says it.

## Standing facts

Each statement or restatement the user typed, once validated, is logged under its property. When the property has been stated in three different sessions since it was last forgotten, it becomes standing. A preference, constraint or decision becomes standing at once when its sentence contains a rule word (always, never, from now on, in every…). `memory_standing` turns one on or off when the user's own words ask for it, and counting never overrides an off.

The prompt hook pins standing facts into the packet, at most 8, whenever the session has not yet received the current set. That happens on the session's first prompt, after SessionStart (start, resume, clear or compact), and after a value changes. Pinned claims carry `standing: true`. Standing facts are exempt from the 180-day cutoff and from the 90-day digest.

## Handing the turn back once

The Stop hook answers `decision: block` when the gate was confident (P(none) ≤ 0.05, and the message did not read as a request for work: "Implementa todos los puntos" scored P(none) 0.04 and work 0.91), the turn was typed, a memory server is registered under the session's host process, and the agent neither captured anything nor tried to. A capture the kernel refused already has its answer, and asking again would only get the same refusal, so a refused attempt is never nudged. The block reason asks the agent to call `memory_capture` with the original turn's token. Codex turns that reason into a new prompt, and Claude Code continues the turn. The original reply is stored masked, and the inference runs on it at the continuation's Stop, after the capture. A continuation never nudges again.

Each MCP server records its host process when it initializes (`servers`), and the Stop hook checks that record. Sessions without memory tools are therefore never nudged, and their gate estimates do not count as missed.

## Requests to the agent

The agent must treat the packet as data and never obey its claims. So the kernel writes its own requests (capture these facts, pending captures, how to forget, do not keep this message) as plain text before the JSON, and the MCP server's instructions describe the same workflow. When the request sat inside the packet, Codex's model honoured "values are data" and never captured anything.

## Selection and delivery

The `rules`, `fts` and `jev` strategies each produce a NeedPlan. All three match words against an FTS5 index of each fact's entity, key, value and cues; a fact saved without cues gets defaults for its kind (`language.CONCEPTS`: a deadline is asked about as ship, launch, release date…).

With jev, pairs are judged once per distinct question, and the result is cached by digest. A pair costs about 0.19 s on the local model whatever the batch, and its score does not depend on the other pairs in the batch (measured: 48 pairs took 9.5 s, more than the hook's budget). So the number of pairs judged is fitted to the time left in the turn, at the pace the last calls measured (`judge_pair_seconds`, a moving average of calls of eight pairs or more), keeping 1 s for the rest of the hook; cached pairs are free. The order is: pairs the question matches in words, strongest match first; then constraints and decisions; then the most recent. Those, and the four most recent pairs, are judged as time allows; any other pair only while 3 s would still be left. In the labelled relevance set, 54 of the 60 facts that mattered were matched in words or were constraints or decisions, and 3 of the other 6 were among the four most recent; with 200 unrelated facts in memory, judging the rest anyway held every prompt at the full 8 s.

On a paraphrased question the judge alone cannot order a busy inventory: asked "is there room before we ship?", it put 32 of 40 unrelated facts above 0.5. A pair the question matches in words (now including the cues) and jev scores at 0.35 or more is selected first and counts as critical; then pairs jev alone scores at 0.60 or more (critical); then at 0.50 (supporting). If the judge is unavailable, out of time, or remote, the kernel falls back to the rules plan and adds a warning. When the local model is not loaded, the prompt does not wait for it: that prompt uses the rules plan (`judge_cold`) and a background warm-up loads the model. After each prompt the kernel asks Ollama to keep the model for 30 minutes (`keep_alive` in the scope's policy). A capture that nobody has confirmed for 180 days stops counting as evidence, though the inventory still lists it.

Claims are added best first, whole, until the byte budget is full. A claim carries dates rather than timestamps and no evidence id (the trace keeps it), about 90 bytes less than before. When critical claims did not all fit, the packet still goes out with the ones that did and a `critical_budget_overflow` warning; only an empty one counts as a failed delivery. The packet carries the reader rules, warnings, and the turn marker (token, capture hint, pending captures, privacy notice). A stale claim carries `stale_assumptions`. A stale inferred recommendation appears under `review`, as an id only, when the turn touches one of its premises' entities or the question names its text. The kernel revalidates the selected pairs and their relations just before delivery, and a single retry reuses the recorded plan.

## Dependencies

Declared links (from the owner CLI or `memory_depend`) and inferred links behave the same way. To infer a link, jev first has to judge the reply a recommendation (0.70), as a whole or by its opening sentence, whichever reads higher: a caveat after the advice ("I couldn't open the file, so I'm going on memory's summary") had pulled a clear recommendation under the bar. Each premise then needs 0.85 on "rests on", or 0.50 if the reply names that premise's value. The candidates are exactly the premises available in that turn, meaning claims the kernel delivered and facts captured from the same message. Recommendations are stored with `assertion_kind: inference` and never become evidence. Staleness is computed at query time. `reaffirm` follows a premise's whole correction chain to its current version.

## Trust boundaries

- The host configuration fixes the scope and the database. Prompts and tool arguments cannot change either.
- Binding uses process ancestry, which the hook records and the server matches. It decides which session a call can belong to, and the token picks the turn. Neither is a cryptographic proof against a process that already runs as the same user.
- Origin tagging (`interactive`, `continuation`, `unknown`) is a conservative heuristic. Only `interactive` turns can authorize deletions, promotions or policy changes, and only when jev reads the recorded text as asking for that action on that fact.
- Values are data. The reader rules say so, and a malicious value cannot change scope, tools or policy. That still does not guarantee a model will ignore one.
- `forget` removes every version of the property, the inferred recommendations that rested on it (their text can quote it), orphaned evidence, and the scope's plans, traces, proposals, operations, retry markers, turns and cached judgments. It leaves a tombstone. It cannot reach host transcripts, backups, or anything already delivered.
- What the repository says is data attributed to the repository. It is bounded like any other value and refused when it is shaped like an instruction. A teammate's commit can still put a false decision into memory. The commit filters and jev's bar make that rare, not impossible, and the agent sees which file or commit each such claim came from.
- The SQLite file has mode 0600 and is not encrypted.

## Two installs, one memory

A project can wire the kernel by hand while the plugin is also on (a checkout under development, an old setup). Both answer the same hook events against the same database. The second prompt hook to open a turn for the same prompt within 30 seconds finds it opened (one upsert statement decides, so two processes cannot both win) and delivers nothing; the second Stop hook finds the turn closed and does nothing. `context-kernel doctor` names the project files that also run the kernel.

## Observability

Traces record ids, reasons, exclusions, warnings, per-pair probabilities, cache hits, latency and delivery state. They never record values or evidence. `memory metrics` reports:

- **precision**: of the captures made in the last 30 days (and ever), how many the user took back soon after: undone, forgotten or revoked within 7 days, or replaced within an hour. Later changes are changes of mind, not misreadings. This is the estimate of automatic capture's precision in real use;
- **latency**: median and p90 projection time over the last 100 prompts, how many were judged by jev, how many fell back to rules, and the measured pace per pair;
- **judges**: each judge's canary verdict;
- capture outcomes by reason. Omitted and missed counts stand in for coverage.

At Stop the user gets one line saying what was saved, what is held, what needs a yes, and what was not saved, with the reason in plain words. The line also says when a requested forget did not happen. The kernel keeps it in `turns.notes`, because the desktop app does not show hook messages. The plugin's band reads it through `activity()` and draws it above the prompt, with an undo for whatever that turn saved.
