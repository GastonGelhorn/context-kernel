# Prompt-hook pilots

## Claude Code (v0.4, conversation only)

```sh
mkdir -p work/claude-pilot
python3 -m tests.native_claude_check --workspace "$(mktemp -d work/claude-pilot/run.XXXXXX)"
```

The runner writes the generated hooks into the empty pilot's `.claude/settings.local.json` and passes the memory MCP server with `--mcp-config`. It then runs five fresh `claude -p` sessions. File, shell and web tools are disallowed, and only the memory tools are allowed. No memory command is typed:

1. "We have three months to deliver the checkout project. Should we rewrite its payment module?"
2. "Update: the checkout deadline changed, we now have three weeks."
3. "Should we still go ahead with what we discussed for the checkout payment module?"
4. "Please forget the checkout deadline."
5. A generic SQLite question.

After each session it reads the kernel database directly. It checks capture, inferred linking, the new version, the review flag, the fresh-session projection and answer, removal, the absence of claims on the generic question, and that every session was bound. It uses your Claude Code sign-in and quota and sends fictional prompts to Anthropic. It never touches `~/.claude`.

## Codex (v0.4, ChatGPT subscription)

Codex asks a person to review project hooks once. The runner therefore works on a prepared pilot, whose hooks keep their trust between runs while its memory is reset:

```sh
python3 -m tests.native_codex_v04_check --prepare /abs/pilot
python3 -m tests.native_codex_v04_check --workspace /abs/pilot
```

Between the two commands:

1. Open Codex once in `/abs/pilot`.
2. Trust the folder.
3. Review and trust the three context-kernel hooks in its prompt or under `/hooks`.
4. Quit.

The run uses the model configured in your Codex CLI with your ChatGPT sign-in. API-key variables are removed, and the memory tools are pre-approved for these invocations only. It runs the same five phases and pre-registered checks as the Claude Code runner (`tests/native_claude_check.py:evaluate`). Pass `--disable-mcp NAME …` to switch off unrelated servers for the run.

Regenerating the hooks (for example after an upgrade) changes their definitions, and Codex asks for review again.

## Historical pilots (v0.1 to v0.3)

The read-only Codex hook pilot below predates v0.4. It used fictional fixtures and a local Qwen model for Codex itself. Keep it for history only; current Codex checks use the subscription runner above.

### Codex (v0.3)

This host-local pilot contains fictional data only. It is separate from personal and real project databases. No trust hash is created or modified by the kernel.

Pilot workspace: /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao

The prepared `.codex/hooks.json` runs the local owner-reviewed kernel command with a fixed `pilot` scope. Proposal capture is disabled. The hook can read current evidence and write derived local traces; it cannot approve facts or erase memory.

## Owner review

Start the CLI in the pilot workspace with the existing local model:

```text
(historical) /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex --cd /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao --oss --local-provider ollama --model qwen3.5:9b -c 'model_reasoning_effort="none"' --disable apps --disable multi_agent --disable shell_tool -c 'web_search="disabled"'
```

The interactive CLI uses your existing user settings. `--ignore-user-config` is an exec-only option and must not be used here. The command explicitly selects the local provider and disables shell, apps, web, and subagents for this pilot. Existing user-level hooks or MCP configuration may still be present; inspect them in the normal client UI.

Use the normal project-trust prompt if shown. Then open `/hooks`, inspect the exact command and database path, and trust this definition only if it matches your intended scope. This action must be performed through Codex's review flow; no bypass is necessary.

## Observe delivery

Ask `Who is the current release approver? Reply with just the name. Do not use tools.`. The fixture answer is `Nyra Vale`. There is no project-local MCP server configured for this pilot. Inspect hook observations and confirm that no tool supplied the answer before attributing it to automatic delivery.

From the kernel checkout, inspect projection traces or retrieve the latest projection ID using the owner CLI. An emitted trace proves local hook output, not complete host attachment. A correct answer and native hook observation are separate checks.

Correct the fictional approver using the owner CLI, start a fresh CLI conversation, and repeat the question. Then explicitly forget that fixture and repeat in another fresh conversation. Forgetting does not erase already-delivered host history.

```sh
python3 -m context_kernel --db /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot list
python3 -m context_kernel --db /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot traces --limit 5
python3 -m context_kernel --db /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot correct STATEMENT_ID '"Orin Keel"' --evidence 'Fictional pilot: the current approver is Orin Keel.'
python3 -m context_kernel --db /Users/gaston/Documents/Codex/2026-10-04/te-x20/outputs/context-kernel/work/codex-hook-v02.zoKyao/memory.sqlite --scope pilot why PROJECTION_ID
```

Inspect the returned replacement ID before an explicit `forget` command. Keep the old fixture only for history tests, not as current evidence.

## Limits

This prepared hook is not yet owner-trusted or automatically active in the current desktop chat. Current native MCP results do not substitute for this step. No global settings were edited. Remove only the pilot's context-kernel hook entry to disconnect it; unrelated settings and host histories remain untouched.

On another checkout, regenerate configuration using `memory adapter codex --workspace /your/pilot --raw` and review the new command. This pilot uses absolute paths specific to this host.

