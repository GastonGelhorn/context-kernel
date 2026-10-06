# Context Kernel

Memory for coding agents that you keep up to date just by talking to them. It remembers what you tell it, notices when you change your mind, and warns the agent when an earlier recommendation was based on something that is no longer true.

The Python core uses only the standard library (SQLite with FTS5). It plugs into Claude Code and Codex through their own hooks and a local MCP server. Automatic capture needs two things besides your agent: [jev](https://github.com/GastonGelhorn/jevmate) and a backend for it. Your agent does the extraction; jev makes the small classification judgments (is this worth keeping, is it relevant to this question, did that recommendation depend on it) on a local model by default. No additional generative model is involved. Without jev, reading memory keeps working and nothing new is saved.

## An example

```text
You: We have three months to deliver checkout. Should we rewrite the payment module?
Agent: (memory_capture: checkout.deadline = "three months")  Yes, three months leaves room to rewrite and test it.
       Memory: saved checkout.deadline. Linked the recommendation to 1 fact it relied on.

        ... days later, another session ...
You: The checkout deadline changed, we now have three weeks.
Agent: (memory_capture corrects checkout.deadline)  Noted.

        ... a new conversation, maybe in the other agent ...
You: Should we still go ahead with the payment module plan?
Agent: That recommendation assumed three months; with three weeks it needs review before we commit.
```

You never typed a memory command in that exchange. Here's what happened underneath.

On every prompt, a hook asks jev whether your message says something worth keeping. If it does, the kernel asks the agent to call `memory_capture`, and it lists facts already stored that look related so a change can point at the one it replaces. If the agent answers without saving something you clearly stated, the Stop hook gives the turn back to it once. Each proposed memory is checked against your current message before it can be stored, held for review, or refused. These checks reduce unsupported captures, but they don't eliminate them: on held-out messages, 2 of 20 that stated nothing were still saved (see [Thresholds](#thresholds)). When two sessions name the same fact differently ("delivery timeline" in one, "deadline" in the next), the kernel works out that they're the same thing.

Changes are stored as new versions linked to the old ones. You can look at the history, and you can take a change back by asking.

When the agent recommends something, the Stop hook asks jev which facts the recommendation relied on: the ones the kernel handed to the agent that turn, plus anything you said in the same message. Those links are saved. Later, when one of those facts changes, the next prompt that touches it tells the agent the earlier recommendation needs another look. It doesn't call the recommendation wrong and it doesn't paste the old text back in; the agent can ask `memory_dependents` if it wants the details.

On each prompt jev also scores stored facts for relevance, and only the relevant ones are delivered. It judges at most 48 of them per prompt: the ones your words mention first, then constraints and decisions, then the most recent. Scores are cached, so asking the same thing again takes about 0.3 s; a new question over 48 facts takes 15 to 20 s on a cold local model, longer than the hook waits, and then selection falls back to keyword rules for that prompt.

## What the repository already says

Teams write decisions down before anyone tells an agent about them: in decision records (`docs/adr/0007-use-postgres.md`) and in commit messages ("Adopt pnpm as the package manager"). When a session starts in a git repository that changed since the last look, the kernel reads them in a background pass, so you never wait for it.

- Decision records are read with a parser, not a model. The Nygard/adr-tools and MADR formats are both understood, in English or Spanish. An accepted record becomes a fact about the project, with the file as its source. A record that is still proposed is skipped. When a record is superseded, it points at the record that replaced it. When it is deprecated or deleted, it stops counting. Either way, every recommendation that rested on it is flagged for review, exactly as if you had changed the fact yourself.
- Commits go through two word filters and then jev. Fixes, tests, docs, bumps and merges are skipped, and so is any subject that doesn't use the language of a decision (use, require, keep, stop, instead of…). jev then judges each remaining subject on its own: is it a choice for the whole project, or a change in one place? A later decision ends the earlier one it replaces ("Use Redis for the job queue" after "Use SQLite for the job queue"), and a revert takes back the decision it reverts. A commit that writes a decision record adds nothing, because the record is read instead.
- What it learned is announced once, in that session's next memory line ("Memory: learned 2 decision(s) from the repository (ADR 1, commit 6818af9)."). The agent sees these facts attributed to `repository`, with the file or commit they came from.

Forgetting one of them works like any other forget, and it sticks until that record changes again in the repository. `memory policy --repository off` turns the whole thing off for a scope, and you can also ask for that in chat.

## Things you keep repeating

When you've stated the same thing in three separate sessions ("use pnpm"), you shouldn't have to say it a fourth time. That fact becomes standing: from then on it's handed to the agent at the start of every session, whether or not the question touches it. It's sent again after a session is resumed, cleared or compacted, and whenever its value changes. A preference, constraint or decision that you state as a rule ("never add Co-Authored-By to commits", "from now on…", "in every repo") becomes standing right away. Standing facts never age out, since they stop being repeated once they work.

Ask "you don't need to keep that in mind every time" to take one off the list, and counting won't put it back. "Always keep this in mind" adds one directly.

## Does it help? Measured

`tests/compare_check.py` runs the same continuity script in fresh headless Claude Code sessions with no plugin, jevmate alone, the kernel alone (selection by rules) and both. The script teaches facts, corrects one, changes a deadline, forgets something, and supersedes a decision record. Over three repetitions:

| | none | jevmate | kernel | kernel + jev selection |
| --- | --- | --- | --- | --- |
| Facts the user had to explain again (of 9) | 8 | 9 | 3 | 0 |
| Stale recommendations not flagged (of 6) | 3 | 3 | 2 | 0 |
| Median time per session | 8.1 s | 8.7 s | 11.0 s | 13.1 s |
| Cost relative to none | – | +7% | +18% | +28% |

Memory costs about 5 s per session: 3 s in the prompt hook and the rest in the agent's save round trip. One script on one machine is a trend, not a benchmark. The details and what it found are in [verification](docs/verification.md#v07-jevmate-alone-the-kernel-alone-both-and-neither).

## Trust and privacy

Facts have three trust levels. `confirmed` ones come from you, either through the owner CLI or by restating something that was captured. `captured` ones were validated from the conversation and are delivered labelled as such. `quarantined` ones are stored but never delivered: pasted or quoted text, private details about other people, things that sounded unsure, and turns a person didn't type.

Each scope has its own policy about what gets saved without asking. In `work`, project state, decisions, constraints, roles and preferences are saved automatically. Personal details and other people's private information are held until you turn those categories on, which you can also do by asking in chat. If you change a confirmed fact, the new value is applied as a new version and the receipt tells you what it replaced (`user.approver (was "Gaston")`). You only get asked first when the kernel itself had to guess which fact you meant, or when two stored values already disagree. Explicit requests like "remember that…" or "from now on…" count as you stating the fact.

If you say "don't remember this", nothing from that message is stored, not even in quarantine, and the excerpt of the message is dropped.

"Undo" takes back the last thing saved, and only when you don't name something else. "Forget the checkout deadline" forgets that fact; it never undoes whatever happened to be saved last. This holds even when jev is down.

Deleting things takes more than the model asking. `memory_forget`, `revoke`, `confirm`, `reaffirm` and policy changes need a turn token that only the hook hands out, a session the hook tied to the MCP server's own host process (by process ancestry, outside the model), and a message you typed that jev agrees is asking for that action on that fact. Prompts that come from continuations, schedules or host markup never count. A forget also wins over writes already in progress: anything that started before it is rejected.

Where your memory goes, in two levels:

- **Storage and judging stay local.** The database is a SQLite file on your machine, and jev judges on a local model by default. If jev is pointed at a hosted backend (say, through `TYPESAFE_BASE_URL`), the kernel sends it nothing unless you've allowed that for the scope; in that case it selects facts with keyword rules instead and refuses new writes. Calls that carry your text skip jev's own cache, and your words reach jev through a private temporary file, never on the command line.
- **Delivery goes to your agent's provider.** The facts selected for a prompt are added to that prompt's context (Claude Code's `additionalContext`, Codex's hook output), so the agent's model reads them, and they travel to that model's provider like the rest of the conversation. A local database doesn't make the agent local.

When jev is unavailable, reading keeps working and writing stops. Selection falls back to keyword rules with a warning and your prompt goes through; nothing new is saved without a judge.

## Install

You need Python 3.9 or newer with SQLite FTS5 (the `python3` that ships with macOS works) and `jev` from [jevmate](https://github.com/GastonGelhorn/jevmate).

### Claude Code

Install jevmate and the kernel from the same marketplace, then run the setup command:

```text
/plugin marketplace add GastonGelhorn/jevmate
/plugin install jevmate@gastongelhorn
/plugin install context-kernel@gastongelhorn
/context-kernel:setup
```

The plugin brings its own hooks and MCP server, so there's nothing to copy into your projects. It asks for a scope, a database and a selection mode when you install it, and you can change those later in the plugin's settings. `/context-kernel:setup` checks the install and lets you pick the judge: jev on a local Ollama model, which is free and keeps everything on your machine, or jev's paid hosted service, which is faster but sends your memory text out, so the scope has to allow it.

After each turn, a line above the prompt tells you what memory saved, held for review or didn't save, with an undo button. It's meant to show up in the desktop app too, which doesn't display hook messages. Its configuration is validated, but nobody has watched it in an interactive session yet; until then, the receipt in the agent's reply ("Memory: saved …") is the dependable signal.

### Terminal and Codex

From a checkout:

```sh
./install.sh
```

The script finds a Python that works (or uses one managed by `uv`), installs `~/.local/bin/context-kernel`, and starts `context-kernel setup`. The setup asks about the judge, the scope and the database (saved to `~/.context-kernel/config.json`), offers to install the Claude Code plugin, and can wire Codex for a project. It shows you the Codex files before writing them, and Codex will ask you to review the hooks the next time it opens that folder. Run `context-kernel doctor` any time to check the install. It also warns you if a project still wires the kernel by hand, which would make every hook run twice.

### By hand

If you'd rather wire a single project yourself, the generator prints the configuration:

```sh
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work init
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev --mode mcp
```

The first `adapter` command prints the hooks (prompt, stop, session start) for `.claude/settings.local.json` and the second prints the MCP server for `.mcp.json`. `adapter codex` does the same for `.codex/hooks.json` and `.codex/config.toml`. The generator doesn't write anything; you review and merge the output yourself. It pins absolute paths, because hosts run hooks with a minimal PATH, and it doesn't skip any of the host's trust or approval steps. Codex asks you to review hooks again whenever they change.

## Looking under the hood

Day to day you just talk. The CLI is there when you want to see what's going on:

```sh
memory --pretty inventory          # everything held, by entity, with trust and category
memory --pretty traces --limit 3   # what each prompt delivered and why (no values, only ids and scores)
memory --pretty metrics            # captured / held / refused / omitted / missed counts
memory policy --enable personal_attributes
memory undo STATEMENT_ID
memory forget STATEMENT_ID
memory learn --workspace .         # read the repository's decisions now instead of at the next session start
memory standing STATEMENT_ID off   # stop handing a fact to every session
```

`remember`, `correct`, `depend`, `reaffirm`, `revoke` and `confirm` are still around for scripting and for confirmed facts. `forget` removes every version of a property in that scope, along with its orphaned evidence, derived plans, traces, turn excerpts and cached judgments. It can't erase host transcripts, backups, or anything a model has already seen.

## Thresholds

The thresholds were measured on the local model (`tev1-32k` through Ollama). You can re-measure them on your own setup:

```sh
memory calibrate affirmed --score        # the kernel's exact question over bilingual fixtures, with a threshold sweep
memory calibrate facts_present --score
memory calibrate forget_asked --score
memory calibrate decision_commit --score
memory policy --threshold affirmed=0.75
```

| Judgment | Default | Measured on 6 to 30 fixtures |
| --- | --- | --- |
| The message asserts this fact | 0.75 (held for review from 0.60) | every true row ≥ 0.76; highest false row 0.757 (a question) when set in v0.4; held-out results below |
| The message states something worth keeping | P(none) < 0.15 | recall 1.0; the one false hit, a forget request, is excluded before judging |
| The message asks to forget this fact | 0.70 | true rows 0.94 to 0.98; false rows ≤ 0.60 |
| The reply recommends something | 0.70 | advice 0.80 to 0.95; reports, questions and refusals ≤ 0.61 |
| The recommendation rests on this premise | 0.85, or 0.50 when the reply names the premise's value | 7 true and 0 false links on 15 labelled pairs |
| A commit states a project-wide choice | 0.50, after the word filters | 25 of 28 written decisions; 0 of 16 routine changes worded like decisions (highest 0.48) |
| A later decision replaces an earlier one | 0.70, for decisions sharing a topic word | 7 of 10 replacements; 0 of 12 other pairs (highest 0.654) |
| The message asks to keep a fact in mind always, or to stop | 0.70 | requests 0.71 to 0.96; everything else ≤ 0.61 |

The first row has drifted since it was set. Re-scored with the current local model, three false rows now reach 0.75, including "Remind me tomorrow to call Ana." (0.94). Re-run `memory calibrate affirmed --score` whenever jev's model changes. Cached judgments are keyed by the model's weights digest when the backend is a local Ollama, so a re-pulled alias doesn't reuse old answers.

Thresholds were chosen on `fixtures/calibration.jsonl`. `fixtures/holdout.jsonl` was written afterwards and is never used to tune them; `python3 -m tests.holdout_check` runs it through the whole capture path with the real jev. With `tev1-32k` (weights `527084f384df0682`), 17 of 18 true statements were captured (the 18th was held because its category is off by default), and of 20 messages that stated nothing, 12 were refused, 4 held for review and 2 saved: an instruction to the agent ("Write a test that checks invoices are PDF only.") and sarcasm ("lol sure, we totally have infinite budget for AWS"). 38 rows is still a small sample.

With ten stored facts, the prompt hook took 0.33 s for a cached question and about 4 s cold, about a second of which is the check for something worth saving. That check is skipped for acknowledgements, picks among options the agent listed ("haz 1 y 2", "do both"), plain questions, generic questions and forget requests. These are small fixture sets, so read them as a trend rather than a benchmark. The details are in [verification](docs/verification.md).

## Limits

- One owner per database. Scopes separate memories inside the application; they aren't OS-level security, and anything that can read the SQLite file can read the memory.
- Tying a session to its MCP server by process ancestry assumes the host starts hooks and MCP servers under the same per-session process. That's true in the Claude desktop app, in headless Claude Code and in Codex, where it was checked. Where it isn't, the server stays unbound and refuses every write.
- `UserPromptSubmit` doesn't prove a person typed the prompt. The kernel is cautious about where a prompt came from and never lets an uncertain origin authorize a deletion, but that's a heuristic, not authentication.
- jev's scores feed a policy; they aren't proof. A fact can pass validation and still be wrong, which is why captures are labelled, reversible, and announced when they change a confirmed value.
- Your agent's model still writes the answer. Better context makes stale or invented answers less likely; it doesn't rule them out.
- Relevance on paraphrases is weak. Asked "Is there room to squeeze the payments refactor in before we ship?", the local judge scored the stored checkout deadline 0.55 and unrelated facts (a feature flag, a Slack channel) up to 0.63. `python3 -m tests.growth_check` measures how often an old constraint, asked about in other words, reaches the agent as memory grows: 2 of 3 at 13 facts, 1 to 2 of 3 at 100 to 200.
- Commits are a narrow channel. The filters and jev keep only clear choices for the whole project, so most commits add nothing. In this repository, where commits change how one tool behaves, 2 of its 13 decision-like commits were kept. Decision records are the dependable source.
- Git can be slow in a folder synced by iCloud, because objects evicted to the cloud are downloaded on first read. That's why hooks never run git: only the background pass does, and it waits up to a minute. Python reads the kernel's own modules the same way, so a hook running from such a folder can be cancelled; `context-kernel doctor` warns when that can happen.

More detail in [architecture](docs/architecture.md), [plan](docs/plan.md), [client setup](docs/adapters.md) and [verification](docs/verification.md).
