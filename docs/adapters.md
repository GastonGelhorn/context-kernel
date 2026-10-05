# Client adapters

Initialize the chosen database first. Bind an absolute path and a fixed owner-selected scope in every adapter command. Two clients share state only when the owner gives them the same path and scope. Do not connect unrelated projects to personal memory by default.

The generator prints configuration; it does not install, enable, approve, or replace existing settings. All examples below run from the kernel checkout. Prefer a dedicated virtual environment's Python executable with `--python` if the client cannot find the interpreter you use interactively.

## Codex

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite --scope work adapter codex --workspace /absolute/path/project --raw
```

Merge the JSON into `.codex/hooks.json`. Review and trust the hook in Codex's `/hooks` flow. `UserPromptSubmit` receives a prompt and adds `additionalContext`; this does not rewrite accumulated history. The adapter validates the reported working directory against its startup boundary and never reads private transcript files. [Codex hooks](https://learn.chatgpt.com/docs/hooks#userpromptsubmit).

Project-local hooks load only through a trusted project layer; reviewing the exact hook definition is an additional step. An exec run with `--ignore-user-config` does not prove that an owner's project trust settings were loaded. The interactive owner-review command must not use that exec-only flag. See the [prepared pilot](hook-pilot.md) for the current host-local walkthrough. [Hook discovery](https://learn.chatgpt.com/docs/hooks#where-codex-looks-for-hooks).

The 2 KiB projection limit is the kernel's UTF-8 byte limit, not the host's token estimate. A small projection does not imply a small total model request. [Host output limits](https://learn.chatgpt.com/docs/hooks#large-hook-output).

For on-demand MCP tools instead:

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite --scope work adapter codex --workspace /absolute/path/project --mode mcp --raw
```

Merge the emitted TOML into `.codex/config.toml`, without replacing other tables. The generated command uses local stdio. Review the server through the client's normal approval flow. [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

## Claude Code

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite --scope work adapter claude --workspace /absolute/path/project --raw
```

Merge the JSON into `.claude/settings.local.json`. The prompt hook uses the same `hookSpecificOutput` envelope. It is not proof of human origin: the documented event can also receive scheduled or other agent messages. Consequently, default hooks never commit memory and optional capture creates pending proposals only. [Claude hook reference](https://code.claude.com/docs/en/hooks).

For MCP, use `--mode mcp` and merge the generated JSON into the project's `.mcp.json`. Start Claude Code in the project and approve the server normally; no approval-bypass setting is generated. [Claude MCP setup](https://code.claude.com/docs/en/mcp).

## Antigravity

```sh
python3 -m context_kernel --db /absolute/path/memory.sqlite --scope work adapter antigravity --workspace /absolute/path/project --raw
```

The generator emits a local `mcpServers` entry for `.agents/mcp_config.json`, as described by the current documentation. Use the configuration location shown by your installation if it differs. The kernel does not edit global config. [Antigravity MCP](https://www.antigravity.google/docs/mcp).

This adapter is on-demand: ask the agent to use `memory_context` when personal/project state can change its answer. It does not promise automatic per-prompt retrieval. The documented `PreInvocation` hook does not provide the current prompt, so we do not reconstruct it from unstable transcript internals. [Antigravity hooks](https://www.antigravity.google/docs/hooks).

## Selection strategy and failure mode

Both hook generators accept `--strategy jev` (calibrated selection through the `jev` command line; see the README) and `--fail-closed`. By default a hook that cannot serve memory emits empty context plus a `systemMessage`; the prompt proceeds. With `--fail-closed` it blocks. Privacy commands block in both modes. The MCP server accepts the same `--strategy` flags after `serve`.

## Optional proposal capture

Add `--proposals` to the **hook configuration generator**, then review the new command. Exact supported English messages are:

```text
Remember: user.constraint = "No late meetings"
Correct: user.constraint = "No meetings after 16:00"
I ordered laptop.
laptop has not arrived.
laptop arrived.
I returned laptop.
It finally arrived.
It has not arrived.
I returned it.
```

JSON values allow explicit negation and structured data. Quoted, fenced, multiline, or merely mentioned commands do not match this grammar. An arbitrary pasted message could still be exactly shaped like a command; this is why capture never accepts it automatically. Review the original evidence before owner approval.

`it` requires exactly one relevant pending delivery, or exactly one owned object for a return. Multiple candidates require an explicit object. Pending transitions become stale if their target state changes before approval. `Forget:` and `Revoke:` commands block with instructions to use the owner CLI; they do not falsely acknowledge privacy changes.

## Acceptance checklist

In a trusted fictional pilot workspace, enable only the intended scope. Use two fresh conversations to check that a registered fact is available, its correction replaces the old version, and forgetting/revocation prevents new retrieval. Inspect the projection ID with `why`. Check a job-offer question with a caregiving constraint and a generic technical turn that needs no personal memory.

Host debug facilities can help confirm hook execution. The kernel labels its trace `projection_only` and host attachment `unknown`; successful stdout or SDK parsing does not establish the complete model request. Native Codex CLI MCP calls and answers were observed using both local Ollama and the owner's ChatGPT subscription, with failures preserved in the [verification report](verification.md#native-codex-cli). This does not establish automatic prompt-hook activation or desktop history behavior.

To remove a pilot, remove only its `context-kernel` hook/server entry, using the client's UI or a careful settings edit. Keep unrelated configuration. Memory remains in the database until the owner explicitly withdraws it. Removing an adapter does not erase previous client history.
