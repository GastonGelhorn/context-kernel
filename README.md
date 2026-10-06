# Context Kernel

Memory for coding agents that you keep up to date just by talking to them. It remembers what you tell it, notices when you change your mind, and warns the agent when an earlier recommendation was based on something that is no longer true.

It's plain Python with SQLite and FTS5 and has no runtime dependencies. It plugs into Claude Code and Codex through their own hooks and a local MCP server. The yes/no calls (is this worth keeping, is it relevant to this question, did that recommendation depend on it) are made by [jev](https://github.com/GastonGelhorn/jevmate), a small calibrated model that runs on your machine. Your agent's own model does the extraction, so there's no second LLM involved.

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

On every prompt, a hook asks jev whether your message says something worth keeping. If it does, the kernel asks the agent to call `memory_capture`, and it lists facts already stored that look related so a change can point at the one it replaces. If the agent answers without saving something you clearly stated, the Stop hook gives the turn back to it once. Each proposed fact is checked against your own words in that message before it's stored, held for review, or refused, so the agent can't save something you didn't say. When two sessions name the same fact differently ("delivery timeline" in one, "deadline" in the next), the kernel works out that they're the same thing.

Changes are stored as new versions linked to the old ones. You can look at the history, and you can take a change back by asking.

When the agent recommends something, the Stop hook asks jev which facts the recommendation relied on: the ones the kernel handed to the agent that turn, plus anything you said in the same message. Those links are saved. Later, when one of those facts changes, the next prompt that touches it tells the agent the earlier recommendation needs another look. It doesn't call the recommendation wrong and it doesn't paste the old text back in; the agent can ask `memory_dependents` if it wants the details.

On each prompt jev also scores every stored fact for relevance, and only the relevant ones are delivered. Scores are cached, so asking the same thing again takes about 0.3 s.

## Trust and privacy

Facts have three trust levels. `confirmed` ones come from you, either through the owner CLI or by restating something that was captured. `captured` ones were validated from the conversation and are delivered labelled as such. `quarantined` ones are stored but never delivered: pasted or quoted text, private details about other people, things that sounded unsure, and turns a person didn't type.

Each scope has its own policy about what gets saved without asking. In `work`, project state, decisions, constraints, roles and preferences are saved automatically. Personal details and other people's private information are held until you turn those categories on, which you can also do by asking in chat. If you change a confirmed fact, the new value is applied as a new version and the receipt tells you what it replaced (`user.approver (was "Gaston")`). You only get asked first when the kernel itself had to guess which fact you meant, or when two stored values already disagree. Explicit requests like "remember that…" or "from now on…" count as you stating the fact.

If you say "don't remember this", nothing from that message is stored, not even in quarantine, and the excerpt of the message is dropped.

"Undo" takes back the last thing saved, and only when you don't name something else. "Forget the checkout deadline" forgets that fact; it never undoes whatever happened to be saved last. This holds even when jev is down.

Deleting things takes more than the model asking. `memory_forget`, `revoke`, `confirm`, `reaffirm` and policy changes need a turn token that only the hook hands out, a session the hook tied to the MCP server's own host process (by process ancestry, outside the model), and a message you typed that jev agrees is asking for that action on that fact. Prompts that come from continuations, schedules or host markup never count. A forget also wins over writes already in progress: anything that started before it is rejected.

Your memory text stays on your machine. If jev is pointed at a hosted backend (say, through `TYPESAFE_BASE_URL`), the kernel sends it nothing unless you've allowed that for the scope. In that case it selects facts with keyword rules instead and refuses new writes. Calls that carry your text skip jev's own cache.

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

After each turn, a line above the prompt tells you what memory saved, held for review or didn't save, with an undo button. It shows up in the desktop app too, which doesn't display hook messages.

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
```

`remember`, `correct`, `depend`, `reaffirm`, `revoke` and `confirm` are still around for scripting and for confirmed facts. `forget` removes every version of a property in that scope, along with its orphaned evidence, derived plans, traces, turn excerpts and cached judgments. It can't erase host transcripts, backups, or anything a model has already seen.

## Thresholds

The thresholds were measured on the local model (`tev1-32k` through Ollama). You can re-measure them on your own setup:

```sh
memory calibrate affirmed --score        # the kernel's exact question over bilingual fixtures, with a threshold sweep
memory calibrate facts_present --score
memory calibrate forget_asked --score
memory policy --threshold affirmed=0.75
```

| Judgment | Default | Measured on 6 to 30 fixtures |
| --- | --- | --- |
| The message asserts this fact | 0.75 (held for review from 0.60) | every true row ≥ 0.76; highest false row 0.757 (a question) when set in v0.4; see below |
| The message states something worth keeping | P(none) < 0.15 | recall 1.0; the one false hit, a forget request, is excluded before judging |
| The message asks to forget this fact | 0.70 | true rows 0.94 to 0.98; false rows ≤ 0.60 |
| The reply recommends something | 0.70 | advice 0.80 to 0.95; reports, questions and refusals ≤ 0.61 |
| The recommendation rests on this premise | 0.85, or 0.50 when the reply names the premise's value | 7 true and 0 false links on 15 labelled pairs |

The first row has drifted since it was set. Re-scored with the current local model, three false rows now reach 0.75, including "Remind me tomorrow to call Ana." (0.94). Re-run `memory calibrate affirmed --score` whenever jev's model changes.

With ten stored facts, the prompt hook took 0.33 s for a cached question and about 4 s cold, about a second of which is the check for something worth saving. That check is skipped for acknowledgements, plain questions, generic questions and forget requests. These are small fixture sets, so read them as a trend rather than a benchmark. The details are in [verification](docs/verification.md).

## Limits

- One owner per database. Scopes separate memories inside the application; they aren't OS-level security, and anything that can read the SQLite file can read the memory.
- Tying a session to its MCP server by process ancestry assumes the host starts hooks and MCP servers under the same per-session process. That's true in the Claude desktop app, in headless Claude Code and in Codex, where it was checked. Where it isn't, the server stays unbound and refuses every write.
- `UserPromptSubmit` doesn't prove a person typed the prompt. The kernel is cautious about where a prompt came from and never lets an uncertain origin authorize a deletion, but that's a heuristic, not authentication.
- jev's scores feed a policy; they aren't proof. A fact can pass validation and still be wrong, which is why captures are labelled, reversible, and announced when they change a confirmed value.
- Your agent's model still writes the answer. Better context makes stale or invented answers less likely; it doesn't rule them out.

More detail in [architecture](docs/architecture.md), [plan](docs/plan.md), [client setup](docs/adapters.md) and [verification](docs/verification.md).
