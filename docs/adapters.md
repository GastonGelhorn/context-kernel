# Client setup by hand

Most people should install the Claude Code plugin, or run `install.sh` for the terminal and Codex, as described in the [README](../README.md#install). The `adapter` generator on this page wires a single project by hand. If the plugin is also enabled, that project runs every hook twice, and `shelflife-context doctor` warns about it.

Initialize a database first and decide which scope it belongs to. Every generated command pins an absolute database path and a fixed scope, and two clients share memory only when they use the same path and the same scope. Don't connect unrelated projects to personal memory by default.

The generator only prints configuration. It does not install, enable or approve anything, and it leaves existing settings alone. It pins absolute paths for Python and `jev` because hosts run hooks with a minimal PATH. Run it from the kernel checkout, or pass `--python` with a virtual environment's interpreter.

Each client needs the hooks (prompt, stop and session start) and the MCP server, which provides the chat tools such as capture, undo, inventory and forget. With hooks and no server, context still reaches the agent but nothing is captured. With the server and no hooks there are no turns to bind to, so a regular server can read but cannot write; for a client without hooks, use `adapter generic` below.

## Claude Code

```sh
python3 -m shelflife_context --db /abs/memory.sqlite --scope work adapter claude --workspace /abs/project --strategy jev --raw
python3 -m shelflife_context --db /abs/memory.sqlite --scope work adapter claude --workspace /abs/project --strategy jev --mode mcp --raw
```

Merge the first output into `.claude/settings.local.json` under `hooks`, and the second into `.mcp.json`. Approve the server when Claude Code asks; the generator includes no way around that approval. The prompt hook returns its context in `hookSpecificOutput.additionalContext`. The SessionStart hook talks to you only through `systemMessage`. In a git repository it also starts a detached background pass that reads the repository's decisions, passes jev's path to it, and returns at once. The pass does nothing when the repository has not changed. So does the Stop hook, with one exception: if your message stated facts worth keeping and nothing was saved, it hands the turn back to the agent once (`decision: block`) so it can call `memory_capture`. See the [Claude hook reference](https://code.claude.com/docs/en/hooks) and [Claude MCP setup](https://code.claude.com/docs/en/mcp).

## Codex

```sh
python3 -m shelflife_context --db /abs/memory.sqlite --scope work adapter codex --workspace /abs/project --strategy jev --raw
python3 -m shelflife_context --db /abs/memory.sqlite --scope work adapter codex --workspace /abs/project --strategy jev --mode mcp --raw
```

Merge the JSON into `.codex/hooks.json` and the TOML into `.codex/config.toml`. Codex lists new or changed hooks for review. Choose "Review hooks" or "Trust all and continue" in its own prompt, and review again after any change. The context packet stays within the kernel's 2 KiB budget, and the generated `additionalContextLimit` of 3072 leaves room for the kernel's plain-text requests that come before it. See [Codex hooks](https://learn.chatgpt.com/docs/hooks) and [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

## Antigravity

`adapter antigravity` prints an `mcpServers` entry for `.agents/mcp_config.json`. Antigravity's hooks don't receive the prompt, so the kernel never sees a turn. The server can read and propose, but chat capture and deletions stay unbound. Ask the agent to call `memory_context` whenever memory could change its answer.

## Any agent without hooks

```sh
python3 -m shelflife_context --db /abs/memory.sqlite --scope work adapter generic --workspace /abs/project --strategy jev --raw
python3 -m shelflife_context --db /abs/memory.sqlite --scope work brief --workspace /abs/project --on
```

The first prints an `mcpServers` entry that runs `serve --hookless`; most MCP clients take that JSON in their own settings file. That server offers the read tools and `memory_capture`, and holds what it saves for your review (`memory confirm ID` promotes it), because no hook recorded your message. The second keeps a section of the project's `AGENTS.md` with what memory holds about the project; the server's reads, the background pass and the Stop hook of any session with hooks keep it current. See [Agents without hooks](../README.md#agents-without-hooks).

## Options

- `--strategy` picks how relevant facts are selected: `jev` (recommended), `rules` or `fts`. Capture, inference and owner actions use jev whenever it is installed, whichever strategy you pick.
- `--fail-closed` blocks the prompt when memory is unavailable. By default the prompt goes through and `systemMessage` explains why memory was missing.
- `--proposals` keeps the v0.2 exact-grammar proposal capture (`Remember: user.x = "y"`) as a fallback that works without jev.
- A blocking response carries both `decision: block` for Claude Code and `continue: false` for Codex.

## Policy

```sh
memory --db /abs/memory.sqlite --scope work policy
memory --db /abs/memory.sqlite --scope work policy --enable personal_attributes
memory --db /abs/memory.sqlite --scope work policy --threshold affirmed=0.75 --allow-remote-judge no
memory --db /abs/memory.sqlite --scope work policy --repository off
memory --db /abs/memory.sqlite --scope work policy --repository-commits on
memory --db /abs/memory.sqlite --scope work policy --keep-alive 2h
```

`--repository off` stops the background pass that reads decision records. Commit messages are read only with `--repository-commits on`; turning them off again retires what was read from them. `--keep-alive` sets how long Ollama keeps the local judge loaded after each turn (`off` leaves Ollama's own default, five minutes). Setting a threshold clears the judge's canary verdict, and the next session start checks it again. You can also ask in chat ("you can remember personal things too", "don't read the repository's decisions"). The agent then calls `memory_policy`, which takes effect only if the message you typed asks for that change.

## Removing

To disconnect a client, remove the shelflife-context hook entries and the MCP server entry, through the client's UI or by editing its settings carefully. The memory stays in the database until you forget it, and removing the adapter does not erase what the client already has in its history.
