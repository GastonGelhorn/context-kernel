# Context Kernel

Memory for coding agents that you maintain by talking, not by typing commands. It remembers what you state, notices when you change it, and tells the agent which earlier recommendations rested on something that has since changed.

Python, SQLite and FTS5, no runtime dependencies. It runs in Claude Code and Codex through their own hooks and a local MCP server. Judgments (is this a fact worth keeping, is it relevant, does this recommendation rest on it) come from [jev](https://github.com/GastonGelhorn/jevmate), a small calibrated model already on your machine. Extraction is done by your agent's own model. There is no second LLM.

## What it does

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

Nothing in that exchange was typed as a memory command. The kernel:

1. **Captures from the conversation.** On every prompt a hook asks jev whether the message states something worth keeping. If it does, the kernel asks the agent, in plain text outside the data packet, to call `memory_capture`. It also lists related stored facts, so a change can name the fact it `replaces`. When two sessions name the same fact differently, the kernel resolves the keys. The kernel checks each proposed fact against your own words in that message, classifies it, and stores it, holds it for review, or refuses it. The agent cannot make up a fact you did not say.
2. **Keeps versions, not summaries.** A change is a new version linked to the old one. History stays inspectable, and you can undo by saying so.
3. **Links recommendations to their premises.** When the agent recommends something, the Stop hook asks jev which premises it rested on: the facts the kernel delivered in that turn, plus facts you stated in that same message. It records those links as inferences.
4. **Flags rather than replaces.** When a premise changes, the next projection that touches it says an earlier recommendation needs review. It does not say the recommendation is wrong, and it does not inject the old text. The agent can ask `memory_dependents` for details.
5. **Delivers only what matters.** On each prompt jev scores every stored fact for relevance to the question. Judgments are cached by digest, so a repeated question costs about 0.3 s.

## Trust, consent and privacy

- **Three trust levels.** `confirmed` comes from you, through the owner CLI or by restating a captured value. `captured` was validated from the conversation and is delivered marked as such. `quarantined` is stored but never delivered: pasted or quoted text, private details about other people, uncertain readings, and turns that a person did not type.
- **Autonomy within categories you allow.** Each scope has a policy. In `work`, project state, decisions, constraints, roles and preferences are captured automatically. Personal attributes and other people's private details are held until you enable them, which you can do by asking in chat. A change you type to a confirmed fact is applied as a new version, and the receipt says what it was ("user.approver (was \"Gaston\")"). A question is asked only when the kernel itself chose which fact to change, or when two values already disagree.
- **"Don't remember this" means nothing is stored**, not even in quarantine, and the turn's excerpt is dropped too.
- **Undo takes back only the last save, and only when you don't name something else.** "Forget the checkout deadline" is a forget of that fact, never an undo of whatever was saved last. That holds even when the judge is down.
- **Deletions need your words and a real turn.** `memory_forget`, `revoke`, `confirm`, `reaffirm` and policy changes need three things:
  - a turn token that only the hook injects;
  - a session that the hook bound to the MCP server's own host process (by process ancestry, outside the model);
  - a message you typed whose recorded text asks for that action on that fact, as judged by jev.

  Prompts produced by continuations, schedules or host markup never authorize them.
- **A forget beats writes already in flight.** A tombstone rejects any capture or inference that started before the forget.
- **Memory text stays local.** If jev is configured for a hosted backend (for example `TYPESAFE_BASE_URL`), the kernel sends it nothing unless the scope explicitly allows it. Selection falls back to rules, and validated writes are refused. Calls that carry your text bypass jev's own cache.
- **Failure policy per use.** Selection fails open: if jev is down, rules are used with a warning and your prompt still goes through. Writes fail closed: without a usable judge nothing is stored.

## Install

Python 3.11+, SQLite with FTS5, and `jev` from [jevmate](https://github.com/GastonGelhorn/jevmate) pointing at a local backend (`jev doctor` should say `backend: ollama` or another local server).

```sh
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work init
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev
python3 -m context_kernel --db ~/.context-kernel/memory.sqlite --scope work adapter claude --workspace /path/to/project --strategy jev --mode mcp
```

The first `adapter` command prints the hooks (prompt, stop, session start) for `.claude/settings.local.json`. The second prints the MCP server for `.mcp.json`. Use `adapter codex` for `.codex/hooks.json` and `.codex/config.toml`. Review and merge them yourself. The generator writes nothing, pins absolute paths (hosts run hooks with a minimal PATH), and never bypasses the host's trust or approval steps. Codex asks you to review hooks again after they change.

## Audit from the terminal (optional)

Day to day you talk. The CLI is for looking under the hood:

```sh
memory --pretty inventory          # everything held, by entity, with trust and category
memory --pretty traces --limit 3   # what each prompt delivered and why (no values, only ids and scores)
memory --pretty metrics            # captured / held / refused / omitted / missed counts
memory policy --enable personal_attributes
memory undo STATEMENT_ID
memory forget STATEMENT_ID
```

`remember`, `correct`, `depend`, `reaffirm`, `revoke` and `confirm` still exist for scripting and for confirmed facts. `forget` removes every version of a property, its orphan evidence, derived plans, traces, turn excerpts and cached judgments in that scope. It does not erase host transcripts, backups or anything a model already received.

## Calibrated, not guessed

Thresholds come from measurements on the local model (`tev1-32k` through Ollama). Re-measure on your own setup:

```sh
memory calibrate affirmed --score        # the kernel's exact question over bilingual fixtures, with a threshold sweep
memory calibrate facts_present --score
memory calibrate forget_asked --score
memory policy --threshold affirmed=0.75
```

| Judgment | Default | Measured on 6 to 30 fixtures |
| --- | --- | --- |
| The message asserts this fact | 0.75 (held for review from 0.60) | every true row ≥ 0.76; highest false row 0.757 (a question) |
| The message states something worth keeping | P(none) < 0.15 | recall 1.0; the one false hit, a forget request, is excluded before judging |
| The message asks to forget this fact | 0.70 | true rows 0.94 to 0.98; false rows ≤ 0.60 |

Measured prompt-hook latency with ten stored facts: 0.33 s for a cached question and about 4 s cold, of which the capture gate takes about one second. The gate is skipped for acknowledgements, question-only messages, generic questions and forget requests. These are small fixture sets, not a benchmark. See [verification](docs/verification.md).

## Boundaries

- One local owner per database. Scopes are application filters, not OS security. A process that can read the SQLite file can read the memory.
- Process-ancestry binding assumes the host starts hooks and MCP servers under the same per-session process. That holds in the Claude desktop app's process tree and in local subprocess tests; `tests/native_claude_check.py` checks it in headless Claude Code, and the Codex walkthrough checks it there. Where it does not hold, the server stays unbound and refuses every write.
- `UserPromptSubmit` does not prove a person typed the prompt. The kernel tags origin conservatively and never lets an uncertain origin authorize deletions. That is a heuristic, not authentication.
- jev's scores are signals for the policy. A fact can be validated and still be wrong. That is why captures are marked, reversible, and never override confirmed facts.
- The agent's own model still answers. A projection reduces the chance of stale or invented context. It does not guarantee the answer.

Read [architecture](docs/architecture.md), [plan](docs/plan.md), [client setup](docs/adapters.md) and [verification](docs/verification.md).
