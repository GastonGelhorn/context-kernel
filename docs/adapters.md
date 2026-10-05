# Client setup

Initialize a database first and decide its scope. Bind an absolute path and a fixed scope in every generated command. Two clients share memory only when you give them the same database path and scope. Do not connect unrelated projects to personal memory by default.

The generator prints configuration. It does not install, enable, approve, or replace existing settings. It pins absolute paths for Python and `jev`, because hosts run hooks with a minimal PATH. Run it from the kernel checkout, or pass `--python` for a virtual environment's interpreter.

Every client needs two pieces: the **hooks** (prompt, stop, session start) and the **MCP server** (the chat tools: capture, undo, inventory, forget, and the rest). Hooks without the server still deliver context, but nothing is captured. The server without hooks has no turns to bind to, so it can read but cannot write.

## Claude Code

```sh
python3 -m context_kernel --db /abs/memory.sqlite --scope work adapter claude --workspace /abs/project --strategy jev --raw
python3 -m context_kernel --db /abs/memory.sqlite --scope work adapter claude --workspace /abs/project --strategy jev --mode mcp --raw
```

Merge the first into `.claude/settings.local.json` under `hooks` and the second into `.mcp.json`. Approve the server when Claude Code asks; no approval bypass is generated. The prompt hook output uses `hookSpecificOutput.additionalContext`. The Stop and SessionStart hooks speak to you only through `systemMessage`, and never re-prompt the model. [Claude hook reference](https://code.claude.com/docs/en/hooks), [Claude MCP setup](https://code.claude.com/docs/en/mcp).

## Codex

```sh
python3 -m context_kernel --db /abs/memory.sqlite --scope work adapter codex --workspace /abs/project --strategy jev --raw
python3 -m context_kernel --db /abs/memory.sqlite --scope work adapter codex --workspace /abs/project --strategy jev --mode mcp --raw
```

Merge the JSON into `.codex/hooks.json` and the TOML into `.codex/config.toml`. Codex lists new or changed hooks for review. Use "Review hooks" or "Trust all and continue" in its own prompt, and review again after any change. The packet stays within the kernel's 2 KiB budget. The generated `additionalContextLimit` (3072) leaves room for the kernel's plain-text requests before it. [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

## Antigravity

`adapter antigravity` emits an `mcpServers` entry for `.agents/mcp_config.json`. Antigravity's hooks do not provide the prompt, so there are no turns: the server can read and propose, but chat capture and deletions stay unbound. Ask the agent to use `memory_context` when memory could change its answer.

## Options

- `--strategy jev` (recommended) or `rules` / `fts` choose how relevance is selected. Capture, inference, and owner actions use jev whenever it is installed, whatever the strategy.
- `--fail-closed` blocks the prompt when memory is unavailable. The default lets the prompt proceed and explains why in `systemMessage`.
- `--proposals` keeps the v0.2 exact-grammar proposal capture (`Remember: user.x = "y"`) as a jev-free fallback.
- Blocking responses carry both `decision: block` (Claude Code) and `continue: false` (Codex).

## Policy

```sh
memory --db /abs/memory.sqlite --scope work policy
memory --db /abs/memory.sqlite --scope work policy --enable personal_attributes
memory --db /abs/memory.sqlite --scope work policy --threshold affirmed=0.75 --allow-remote-judge no
```

You can ask for the same in chat ("you can remember personal things too"). The agent calls `memory_policy`, which runs only if your typed message asks for it.

## Removing

Remove the context-kernel hook entries and the MCP server entry through the client's UI or a careful settings edit. Memory stays in the database until you forget it. Removing the adapter does not erase earlier client history.
