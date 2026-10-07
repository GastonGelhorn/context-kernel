# Prompt-hook pilots

The two runners below exercise the v0.4 conversation flow in real Claude Code and Codex sessions. Each one wires the kernel into a pilot folder with the generated `adapter` configuration and its own `pilot` scope and database.

## Claude Code (v0.4, conversation only)

```sh
mkdir -p work/claude-pilot
python3 -m tests.native_claude_check --workspace "$(mktemp -d work/claude-pilot/run.XXXXXX)"
```

The runner writes the generated hooks into the empty pilot folder's `.claude/settings.local.json` and passes the memory MCP server with `--mcp-config`. It then runs five fresh `claude -p` sessions with file, shell and web tools disallowed and only the memory tools allowed. Each session gets `SHELFLIFE_CONTEXT_OFF=1`, so a Shelflife plugin you have installed stays out of the run and the fictional prompts never reach your real memory. None of the prompts is a memory command:

1. "We have three months to deliver the checkout project. Should we rewrite its payment module?"
2. "Update: the checkout deadline changed, we now have three weeks."
3. "Should we still go ahead with what we discussed for the checkout payment module?"
4. "Please forget the checkout deadline."
5. A generic SQLite question.

After each session the runner reads the kernel database directly. Across the run it checks:

- capture and inferred linking;
- the new version and the review flag;
- the fresh session's projection and answer;
- removal;
- no claims on the generic question;
- that every session was bound.

The runner uses your Claude Code sign-in and quota and sends the fictional prompts to Anthropic. It never touches `~/.claude`.

## Codex (v0.4, ChatGPT subscription)

Codex asks a person to review project hooks once, so this runner works on a prepared pilot. The hooks keep their trust between runs, and each run resets only the pilot's memory:

```sh
python3 -m tests.native_codex_v04_check --prepare /abs/pilot
python3 -m tests.native_codex_v04_check --workspace /abs/pilot
```

Between the two commands:

1. Open Codex once in `/abs/pilot`.
2. Trust the folder.
3. Review and trust the three shelflife-context hooks in its prompt or under `/hooks`.
4. Quit.

The run uses the model configured in your Codex CLI and your ChatGPT sign-in. The runner removes API-key variables from the environment and pre-approves the memory tools for these invocations only. It runs the same five phases and pre-registered checks as the Claude Code runner (`tests/native_claude_check.py:evaluate`). Pass `--disable-mcp NAME …` to turn off unrelated MCP servers for the run.

If you regenerate the hooks, for example after an upgrade, their definitions change and Codex asks you to review them again.

## Historical pilots (v0.1 to v0.3)

The read-only Codex hook pilot below predates v0.4. It used fictional fixtures and ran Codex itself on a local Qwen model. It is kept for the record; current Codex checks use the subscription runner above.

### Codex (v0.3)

This pilot ran on this machine with fictional data only, in a database separate from personal and real project memory. The kernel creates or modifies no trust hash.

Pilot workspace: /path/to/shelflife-context/work/codex-hook-v02.zoKyao

The prepared `.codex/hooks.json` runs the local, owner-reviewed kernel command with a fixed `pilot` scope and proposal capture turned off. The hook can read current evidence and write derived local traces. It cannot approve facts or erase memory.

### Owner review

Start the CLI in the pilot workspace with the existing local model:

```text
(historical) codex --cd /path/to/shelflife-context/work/codex-hook-v02.zoKyao --oss --local-provider ollama --model qwen3.5:9b -c 'model_reasoning_effort="none"' --disable apps --disable multi_agent --disable shell_tool -c 'web_search="disabled"'
```

The interactive CLI reads your existing user settings. Don't add `--ignore-user-config` here; it only works with `exec`. The command selects the local provider explicitly and turns off shell, apps, web search and subagents for this pilot. User-level hooks or MCP configuration you already have may still load, so check them in the normal client UI.

Accept the normal project-trust prompt if it appears. Then open `/hooks`, check the exact command and database path, and trust the definition only if it matches the scope you intend. Do this through Codex's own review flow; no bypass is needed.

### Observe delivery

Ask `Who is the current release approver? Reply with just the name. Do not use tools.`. The fixture answer is `Nyra Vale`. The pilot has no project-local MCP server configured. Before crediting automatic delivery for a correct answer, check the hook observations and confirm that no tool supplied it.

From the kernel checkout, inspect the projection traces or get the latest projection ID with the owner CLI. An emitted trace shows that the hook produced output locally. It does not show that the host attached all of it, so a correct answer and a native hook observation have to be checked separately.

Correct the fictional approver with the owner CLI, start a fresh CLI conversation and ask again. Then forget that fixture explicitly and ask once more in another fresh conversation. Forgetting does not erase host history that already received the fact.

```sh
python3 -m shelflife_context --db /path/to/shelflife-context/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot list
python3 -m shelflife_context --db /path/to/shelflife-context/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot traces --limit 5
python3 -m shelflife_context --db /path/to/shelflife-context/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot correct STATEMENT_ID '"Orin Keel"' --evidence 'Fictional pilot: the current approver is Orin Keel.'
python3 -m shelflife_context --db /path/to/shelflife-context/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot why PROJECTION_ID
```

Check the replacement ID the CLI returns before running an explicit `forget`. Keep the old fixture only for history tests, and don't use it as current evidence.

### Limits

This prepared hook has not been trusted by the owner yet and is not active in the current desktop chat. The native MCP results so far do not replace that step. No global settings were edited. To disconnect the pilot, remove only its shelflife-context hook entry; unrelated settings and host histories stay as they are.

The paths in this pilot are specific to this machine. On another checkout, regenerate the configuration with `memory adapter codex --workspace /your/pilot --raw` and review the new command.
