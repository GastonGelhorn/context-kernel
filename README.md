# Shelflife

Durable context for coding agents that tracks when earlier advice goes stale. You keep it up to date just by talking: it remembers what you tell the agent and what your repository's decision records say, notices when you change your mind or finish something, and when a fact a recommendation rested on changes, it tells the agent that recommendation needs another look. The memory is the mechanism; not building on assumptions that no longer hold is the point.

The Python core uses only the standard library (SQLite with FTS5). It plugs into Claude Code and Codex through their own hooks and a local MCP server, and into agents without hooks through `AGENTS.md` and MCP ([how](#agents-without-hooks)). Automatic capture needs two things besides your agent: [jev](https://github.com/GastonGelhorn/jevmate) and a backend for it. Your agent does the extraction; jev makes the small classification judgments (is this worth keeping, is it relevant to this question, did that recommendation depend on it) on a local model by default. No additional generative model is involved. Without jev, reading memory keeps working and nothing new is saved.

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

On each prompt jev also scores stored facts for relevance, and only the relevant ones are delivered. When the agent saves a fact it adds a few cues, the words a later question would use when the fact matters ("ship date", "launch" for a deadline), so a question in other words can still find it. jev judges as many facts as the hook's time allows, about 0.3 s each on the local model: the ones your words match first, then constraints and decisions, then the most recent. A fact matched in words that jev also finds plausible goes first. Scores are cached, so asking the same thing again is nearly free.

## What the repository already says

Teams write decisions down before anyone tells an agent about them, in decision records (`docs/adr/0007-use-postgres.md`). When a session starts in a git repository that changed since the last look, the kernel reads them in a background pass, so you never wait for it. Commit messages ("Adopt pnpm as the package manager") are a weaker source and are read only if you turn them on.

- Decision records are read with a parser, not a model. The Nygard/adr-tools and MADR formats are both understood, in English or Spanish. An accepted record becomes a fact about the project, with the file as its source. A record that is still proposed is skipped. When a record is superseded, it points at the record that replaced it. When it is deprecated or deleted, it stops counting. Either way, every recommendation that rested on it is flagged for review, exactly as if you had changed the fact yourself.
- Commits, when you turn them on (`memory policy --repository-commits on`), go through two word filters and then jev. Fixes, tests, docs, bumps and merges are skipped, and so is any subject that doesn't use the language of a decision (use, require, keep, stop, instead of…). jev then judges each remaining subject on its own: is it a choice for the whole project, or a change in one place? A later decision ends the earlier one it replaces ("Use Redis for the job queue" after "Use SQLite for the job queue"), and a revert takes back the decision it reverts. A commit that writes a decision record adds nothing, because the record is read instead. They are off by default since v0.8: on this repository they had turned "Add the MIT license…" and two other subjects into project decisions. Turning them off retires what was read from commits; turning them back on reads it again.
- What it learned is announced once, in that session's next memory line ("Memory: learned 2 decision(s) from the repository (ADR 1, commit 6818af9)."). The agent sees these facts attributed to `repository`, with the file or commit they came from.

Forgetting one of them works like any other forget, and it sticks until that record changes again in the repository. `memory policy --repository off` turns the whole thing off for a scope, and you can also ask for that in chat.

## Things you keep repeating

When you've stated the same thing in three separate sessions ("use pnpm"), you shouldn't have to say it a fourth time. That fact becomes standing: from then on it's handed to the agent at the start of every session, whether or not the question touches it. It's sent again after a session is resumed, cleared or compacted, and whenever its value changes. A preference, constraint or decision that you state as a rule ("never add Co-Authored-By to commits", "from now on…", "in every repo") becomes standing right away. Standing facts never age out, since they stop being repeated once they work.

Ask "you don't need to keep that in mind every time" to take one off the list, and counting won't put it back. "Always keep this in mind" adds one directly.

## When something is over

Plans finish. When you say a stored step is done ("we shipped the billing migration"), cancelled ("al final no vamos a Lisboa") or over ("the hiring freeze is over"), the kernel asks the agent to close it with `memory_close`, and the line after the turn says so ("Memory: closed billing_service.next_step (done)."). A closed fact stays in the history but is no longer handed to the agent, and anything that rested on it is flagged for review. Unlike a forget, it isn't counted as a memory mistake, because it was right. jev checks your words against that fact first: on 35 labelled messages it accepted 16 of 19 real closes (17 in a second run) and none of the 16 that weren't ("we're halfway through the migration", "¿ya está publicada la v0.3?"). `memory close ID` and `memory reopen ID` do the same by hand.

A fact stated with a relative period ends with that period on its own. "Decidimos publicar la v0.3 esta semana" is kept until Sunday, and the receipt says so ("Memory: saved portal.release_v0_3 until 2026-10-11."). The period has to be in the fact and in your own words. Dates are values and never end a fact, and a start ("desde mañana"), a habit ("los viernes") or an idiom ("hoy en día") isn't a period. Today, tomorrow, this or next week, month and year, and a named weekday are understood in English and Spanish; "next Friday" is read as the later of the two Fridays it can mean, so nothing ends early.

## Agents without hooks

Hooks are what let Shelflife put memory into every prompt and check what the agent saves against your own words, and not every agent has them. Many editors and agents only read instruction files (`AGENTS.md`) and talk to MCP servers. Cloud and CI agents work on a clone of the repository, far from your memory. An organization can turn hooks off, and a subagent gets its task from the main agent, not from you. Two things cover them, and you can use either or both:

- **The brief.** `memory brief --workspace . --on` keeps a section of the repository's `AGENTS.md`, the file most agents read on their own, with what memory holds about the project: the current facts with their source and end date, what changed lately, the advice that rested on something that changed (never the old advice itself), and what was closed. It's rewritten only when that changes, so it adds no commits of its own, and it stays under about 3 KB. Only facts about the project and its parts go in: nothing about people, and nothing held for review. It's opt-in per repository because `AGENTS.md` is usually committed and shared; `--off` removes the section, and the file too if Shelflife created it.
- **A server for agents without hooks.** `memory adapter generic --workspace .` prints an MCP server configuration that any MCP client takes. The agent can read memory and save what you tell it, but since nothing records your message, what it saves is held for your review until you confirm it (`memory confirm ID`, or in an agent with hooks). Forgetting, closing and changing facts stay with you. Its reads also keep the brief current.

In a native check, Claude Code with no hooks at all, reading only `AGENTS.md` and this server, answered "Who signs off releases here, and how long do we have for checkout?" with the current approver and "three weeks, down from three months, so any advice from earlier today that assumed three months needs another look", and held "our staging database is Postgres 16" for review instead of saving it.

## Does it help? Measured

`tests/compare_check.py` runs the same continuity script in fresh headless Claude Code sessions with no plugin, jevmate alone, the kernel alone (selection by rules) and both. The script teaches facts, corrects one, changes a deadline, forgets something, and supersedes a decision record. Over three repetitions:

| | none | jevmate | kernel | kernel + jev selection |
| --- | --- | --- | --- | --- |
| Facts the user had to explain again (of 9) | 8 | 9 | 3 | 0 |
| Stale recommendations not flagged (of 6) | 3 | 3 | 2 | 0 |
| Median time per session | 8.1 s | 8.7 s | 11.0 s | 13.1 s |
| Cost relative to none | – | +7% | +18% | +28% |

Memory costs about 5 s per session: 3 s in the prompt hook and the rest in the agent's save round trip. One script on one machine is a trend, not a benchmark. The details and what it found are in [verification](docs/verification.md#v07-jevmate-alone-the-kernel-alone-both-and-neither).

Since v0.8 there is a benchmark anyone can rerun without an account ([how](docs/benchmark.md)): 30 multi-session scenarios in English and Spanish replayed through the kernel's own hooks, with what a competent agent would save fixed in the scenario, and the local judge (tev1-32k on Ollama) under the hook's real time budget. Each scenario says what should be delivered, flagged, saved, held or gone afterwards; there are 211 such checks. The scenarios also bury the facts that matter among unrelated ones:

| | Empty memory | +200 unrelated facts | +1,000 unrelated facts |
| --- | --- | --- | --- |
| Checks passed (of 211) | 193 | 186 | 179 |
| Stale recommendations flagged (of 7) | 7 | 6 | 5 |
| False memories kept out (of 45 checks) | 42 | 42 | 42 |
| Prompt hook, median / p90 | 1.3 s / 1.7 s | 4.6 s / 8.0 s | 4.6 s / 8.0 s |

Privacy requests, forgetting and decision records passed every check at every size. On 38 held-out messages, 17 of 18 statements were saved and none of the 20 that stated nothing (v0.7: 2). On 60 labelled questions over 122 facts, the facts that matter reached the agent within the hook's time 17 to 18 times out of 30, against 6 to 7 for v0.7, whose judge ran out of time at that size and fell back to keyword rules. The details, including what went wrong on the way, are in [verification](docs/verification.md#v08-trust-and-scale).

## Trust and privacy

Facts have three trust levels. `confirmed` ones come from you, either through the owner CLI or by restating something that was captured. `captured` ones were validated from the conversation and are delivered labelled as such. `quarantined` ones are stored but never delivered: pasted or quoted text, private details about other people, things that sounded unsure, jokes, things that were true only in the past ("until August deploys were manual"), requests for work ("write a test that checks invoices are PDF only" is a task, not a fact about invoices), and turns a person didn't type.

A false memory costs more than a missing one, so capture leans to holding. The agent cites the words you used (`quote`), and words that aren't in your message, or are only in what you pasted, aren't saved as yours. A fact the agent proposes when the kernel saw nothing stated in your message needs a near-certain reading, or it is held for review. And the thresholds belong to the model they were measured on: when jev's model changes, a canary of twelve rows checks them in the background, and until it passes nothing is saved, only held (`memory calibrate --check` runs it now).

Each scope has its own policy about what gets saved without asking. In `work`, project state, decisions, constraints, roles and preferences are saved automatically. Personal details and other people's private information are held until you turn those categories on, which you can also do by asking in chat. If you change a confirmed fact, the new value is applied as a new version and the receipt tells you what it replaced (`user.approver (was "Gaston")`). You only get asked first when the kernel itself had to guess which fact you meant, or when two stored values already disagree. Explicit requests like "remember that…" or "from now on…" count as you stating the fact.

If you say "don't remember this", nothing from that message is stored, not even in quarantine, and the excerpt of the message is dropped.

"Undo" takes back the last thing saved, and only when you don't name something else. "Forget the checkout deadline" forgets that fact; it never undoes whatever happened to be saved last. This holds even when jev is down.

Deleting things takes more than the model asking. `memory_forget`, `revoke`, `confirm`, `reaffirm` and policy changes need a turn token that only the hook hands out, a session the hook tied to the MCP server's own host process (by process ancestry, outside the model), and a message you typed that jev agrees is asking for that action on that fact. Prompts that come from continuations, schedules or host markup never count. A forget also wins over writes already in progress: anything that started before it is rejected.

Where your memory goes, in two levels:

- **Storage and judging stay local.** The database is a SQLite file on your machine, and jev judges on a local model by default. An Ollama on another machine of yours (a Mac on your Tailscale network, a box on your LAN) counts as local once setup names it, and only that server: its scheme, host and port. If jev is pointed at a hosted backend (say, through `TYPESAFE_BASE_URL`) or at a server you haven't named, the kernel sends it nothing unless you've allowed that for the scope; in that case it selects facts with keyword rules instead and refuses new writes. Calls that carry your text skip jev's own cache, and your words reach jev through a private temporary file, never on the command line.
- **Delivery goes to your agent's provider.** The facts selected for a prompt are added to that prompt's context (Claude Code's `additionalContext`, Codex's hook output), so the agent's model reads them, and they travel to that model's provider like the rest of the conversation. A local database doesn't make the agent local.

When jev is unavailable, reading keeps working and writing stops. Selection falls back to keyword rules with a warning and your prompt goes through; nothing new is saved without a judge.

## Install

You need Python 3.9 or newer with SQLite FTS5 (the `python3` that ships with macOS works) and `jev` from [jevmate](https://github.com/GastonGelhorn/jevmate).

### Claude Code

Install jevmate and the kernel from the same marketplace, then run the setup command:

```text
/plugin marketplace add GastonGelhorn/jevmate
/plugin install jevmate@gastongelhorn
/plugin install shelflife-context@gastongelhorn
/shelflife-context:setup
```

The plugin brings its own hooks and MCP server, so there's nothing to copy into your projects. It asks for a scope, a database and a selection mode when you install it, and you can change those later in the plugin's settings. `/shelflife-context:setup` checks the install and lets you pick the judge: jev on Ollama on this machine, which is free and keeps everything here; jev on Ollama on another machine of yours, free and kept on your machines; or jev's paid hosted service, which is faster but sends your memory text out, so the scope has to allow it.

After each turn, a line above the prompt tells you what memory saved, held for review or didn't save, with an undo button. It's meant to show up in the desktop app too, which doesn't display hook messages. Its configuration is validated, but nobody has watched it in an interactive session yet; until then, the receipt in the agent's reply ("Memory: saved …") is the dependable signal.

### Terminal and Codex

From a checkout:

```sh
./install.sh
```

The script finds a Python that works (or uses one managed by `uv`), installs `~/.local/bin/shelflife-context`, and starts `shelflife-context setup`. The setup asks about the scope, the database and the judge (saved to `~/.shelflife-context/config.json`), offers to install the Claude Code plugin, and can wire Codex for a project. It shows you the Codex files before writing them, and Codex will ask you to review the hooks the next time it opens that folder. Every question has a flag, so a whole setup is one line; with the judge on another machine of yours:

```sh
shelflife-context setup --yes --claude no --scope work --judge remote --judge-url 100.74.0.1
```

jev (jevmate 1.10 or newer) checks that Ollama, makes the `tev1-32k` it needs (`--pull` also downloads `tev1`, 4.5 GB), and setup names that server as yours. That machine's Ollama has to listen beyond itself (`OLLAMA_HOST=0.0.0.0`) and has no password, so keep it on a network you trust. Run `shelflife-context doctor` any time to check the install. It also warns you if a project still wires the kernel by hand, which would make every hook run twice.

### By hand

If you'd rather wire a single project yourself, the generator prints the configuration:

```sh
python3 -m shelflife_context --db ~/.shelflife-context/memory.sqlite --scope work init
python3 -m shelflife_context --db ~/.shelflife-context/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev
python3 -m shelflife_context --db ~/.shelflife-context/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev --mode mcp
```

The first `adapter` command prints the hooks (prompt, stop, session start) for `.claude/settings.local.json` and the second prints the MCP server for `.mcp.json`. `adapter codex` does the same for `.codex/hooks.json` and `.codex/config.toml`. The generator doesn't write anything; you review and merge the output yourself. It pins absolute paths, because hosts run hooks with a minimal PATH, and it doesn't skip any of the host's trust or approval steps. Codex asks you to review hooks again whenever they change.

### Other agents

Any agent that speaks MCP or reads `AGENTS.md` can use Shelflife without hooks: `adapter generic` prints its MCP server, and `brief --on` keeps the project's memory in `AGENTS.md`. See [Agents without hooks](#agents-without-hooks).

### Upgrading from the earlier name

Shelflife was published as Context Kernel up to v0.9. To keep your memory, move `~/.context-kernel` to `~/.shelflife-context`, uninstall the old plugin, and install `shelflife-context@gastongelhorn`.

## Looking under the hood

Day to day you just talk. The CLI is there when you want to see what's going on:

```sh
memory --pretty inventory          # everything held, by entity, with trust and category
memory --pretty traces --limit 3   # what each prompt delivered and why (no values, only ids and scores)
memory --pretty metrics            # how often you took back what was saved, hook latency, outcomes by reason
memory --pretty calibrate --check  # does the current jev model still pass the canary with these thresholds?
memory policy --enable personal_attributes
memory undo STATEMENT_ID
memory forget STATEMENT_ID
memory close STATEMENT_ID --outcome done   # it was right and it is over (done, cancelled, ended); `reopen` takes it back
memory learn --workspace .         # read the repository's decisions now instead of at the next session start
memory standing STATEMENT_ID off   # stop handing a fact to every session
memory brief --workspace . --on    # keep the project's memory in AGENTS.md, for agents without hooks
memory confirm STATEMENT_ID        # use a fact an agent without hooks saved for your review
```

`remember`, `correct`, `depend`, `reaffirm`, `revoke` and `confirm` are still around for scripting and for confirmed facts. `forget` removes every version of a property in that scope, along with its orphaned evidence, derived plans, traces, turn excerpts and cached judgments. It can't erase host transcripts, backups, or anything a model has already seen.

## Thresholds

The thresholds were measured on the local model (`tev1-32k` through Ollama). You can re-measure them on your own setup:

```sh
memory calibrate affirmed --score        # the kernel's exact question over bilingual fixtures, with a threshold sweep
memory calibrate facts_present --score
memory calibrate forget_asked --score
memory calibrate close_asked --score
memory calibrate decision_commit --score
memory policy --threshold affirmed=0.75
```

| Judgment | Default | Measured on 6 to 30 fixtures |
| --- | --- | --- |
| The message asserts this fact | 0.75 (held for review from 0.60) | every true row ≥ 0.76; highest false row 0.757 (a question) when set in v0.4; held-out results below |
| … the sentence the agent cited, when the whole message is in the band | 0.75 | facts next to a question: 0.64–0.75 as a message, 0.81–0.94 as a sentence; "…20k EUR. Just kidding" 0.06 as a message never qualifies |
| The writer is joking about it | 0.60, or 0.45 with a joke's markers (jaja, lol, "como si"): held | jokes 0.59 to 0.73; true statements ≤ 0.33 |
| It used to be true and has stopped | 0.70: held | 6 past states ≥ 0.76; 77 of 78 true statements ≤ 0.46, the 78th 0.67 |
| The writer asks for work rather than stating a fact | 0.60, asked only when the sentence opens with a work verb: held | requests 0.73 to 0.89; a fact stated next to a question 0.68, hence the second condition |
| A fact the gate saw nothing of | saved only from 0.90 | every false row ≤ 0.875; the gate saw a fact in all 21 true statements, so the bar touched none of them |
| The message states something worth keeping | P(none) < 0.15 | recall 1.0; the one false hit, a forget request, is excluded before judging |
| The message asks to forget this fact | 0.70 | true rows 0.94 to 0.98; false rows ≤ 0.60 |
| The reply recommends something | 0.70, on the whole reply or its opening sentence | advice 0.80 to 0.95 whole, 0.76 to 0.97 opening; reports, questions and refusals ≤ 0.62 whole, ≤ 0.57 opening |
| The recommendation rests on this premise | 0.85, or 0.50 when the reply names the premise's value | 7 true and 0 false links on 15 labelled pairs |
| A commit states a project-wide choice | 0.50, after the word filters | 25 of 28 written decisions; 0 of 16 routine changes worded like decisions (highest 0.48) |
| A later decision replaces an earlier one | 0.70, for decisions sharing a topic word | 7 of 10 replacements; 0 of 12 other pairs (highest 0.654) |
| The message asks to keep a fact in mind always, or to stop | 0.70 | requests 0.71 to 0.96; everything else ≤ 0.61 |
| The message says a stored fact is done, cancelled or over | 0.70, the higher of two questions (done; cancelled or ended) | 16 to 17 of 19 closes over two runs; 0 of 16 others (highest 0.66, "ya he reiniciado, ¿qué queda?" against a step that starts "tras reiniciar") |

The first row has drifted since it was set. Re-scored with the current local model, three false rows now reach 0.75, including "Remind me tomorrow to call Ana." (0.94). Re-run `memory calibrate affirmed --score` whenever jev's model changes. Cached judgments are keyed by the model's weights digest when the backend is a local Ollama, so a re-pulled alias doesn't reuse old answers.

Thresholds were chosen on `fixtures/calibration.jsonl`. `fixtures/holdout.jsonl` was written afterwards and is never used to tune them; `python3 -m tests.holdout_check` runs it through the whole capture path with the real jev. With `tev1-32k` (weights `527084f384df0682`), 17 of 18 true statements were captured (the 18th was held because its category is off by default), and of 20 messages that stated nothing, none was saved: 14 were refused and 6 held for review. In v0.7 two were saved, a request for work ("Write a test that checks invoices are PDF only.") and sarcasm ("lol sure, we totally have infinite budget for AWS"); both are now held. 38 rows is still a small sample.

With ten stored facts, the prompt hook took 0.33 s for a cached question and about 4 s cold, about a second of which is the check for something worth saving. Since v0.8 a session's start loads the model in the background and asks Ollama to keep it 30 minutes, and a prompt that still finds it unloaded answers from keyword matches instead of waiting. That check is skipped for acknowledgements, picks among options the agent listed ("haz 1 y 2", "do both"), plain questions, generic questions and forget requests. These are small fixture sets, so read them as a trend rather than a benchmark. The details are in [verification](docs/verification.md).

## Limits

- One owner per database. Scopes separate memories inside the application; they aren't OS-level security, and anything that can read the SQLite file can read the memory.
- Tying a session to its MCP server by process ancestry assumes the host starts hooks and MCP servers under the same per-session process. That's true in the Claude desktop app, in headless Claude Code and in Codex, where it was checked. Where it isn't, the server stays unbound and refuses every write.
- `UserPromptSubmit` doesn't prove a person typed the prompt. The kernel is cautious about where a prompt came from and never lets an uncertain origin authorize a deletion, but that's a heuristic, not authentication.
- jev's scores feed a policy; they aren't proof. A fact can pass validation and still be wrong, which is why captures are labelled, reversible, and announced when they change a confirmed value.
- Your agent's model still writes the answer. Better context makes stale or invented answers less likely; it doesn't rule them out.
- Paraphrases are still the weak spot. On the labelled set's 45 paraphrased questions (no word in common with the facts that matter), 21 of the relevant facts reach the agent within the hook's time on the local judge, 29 with time for twice as many pairs; on direct questions, 14 of 15. The local judge alone cannot order a busy inventory ("Is there room to squeeze the payments refactor in before we ship?" put the deadline 23rd of 41), which is why the agent's cues and the keyword match carry so much weight.
- The local judge is slow per pair: about 0.3 s, so the 8 s hook judges 12 to 19 facts per new question (cached questions are free). Memory larger than that relies on the order of judging: what the question matches in words, then constraints and decisions, then the most recent, and the rest only while 3 s would still be left. With 200 to 1,000 unrelated facts in memory, a prompt took 4.5 to 5.8 s at the median and up to the full 8 s. A hosted jev is much faster but sends memory text out, so the scope has to allow it.
- A close needs your words to name the fact. Of the 19 labelled closes, "La release de la v0.3 salió el lunes" and "Close the security review item" were refused in both runs (0.51 to 0.69 against the 0.70 bar), and in one run so was "ya hicimos la prueba conjunta" against a step with two parts ("tras reiniciar…: probar jevmate junto con … y medir…"). Saying it plainly again, or `memory close`, works. Facts saved before v0.9 have no end date even when they say "this week"; close them when they're done.
- Commits are a narrow channel and are off by default. With them on, the filters and jev keep only clear choices for the whole project; in this repository three subjects still became "decisions" no one had made for the whole project. Decision records are the dependable source.
- Git can be slow in a folder synced by iCloud, because objects evicted to the cloud are downloaded on first read. That's why hooks never run git: only the background pass does, and it waits up to a minute. Python reads the kernel's own modules the same way, so a hook running from such a folder can be cancelled; `shelflife-context doctor` warns when that can happen.

More detail in [architecture](docs/architecture.md), [plan](docs/plan.md), [client setup](docs/adapters.md), [verification](docs/verification.md), [measuring it yourself](docs/benchmark.md) and [compatibility](docs/compatibility.md).
