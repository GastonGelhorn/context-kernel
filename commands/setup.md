---
description: Check Context Kernel and choose its judge (local Ollama or paid jev), scope and clients
---

Set up Context Kernel for the user. Speak in the user's language and in plain words; never show them internal ids.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/context-kernel" doctor` and read the result.
   - If jev is missing, tell the user the kernel needs jevmate and how to install it (`/plugin marketplace add GastonGelhorn/jevmate`, then `/plugin install jevmate@gastongelhorn`), and stop.
   - If doctor warns that hooks run twice, tell the user which project files also run the kernel and offer to remove only the kernel's entries from them.
2. Ask the user (one AskUserQuestion with these questions):
   - Judge: "local" (jev on Ollama on this machine: free, private, about 0.3 s per prompt once warm) or "hosted" (jev's paid service: faster, but the text of their memory and messages leaves the machine), or "keep" the current one. Mention that jev's backend is shared with jevmate.
   - Scope: keep the current one (from doctor) or name another (work, personal, a client).
   - Codex: whether to also wire Codex for this project.
3. Apply the answers with `python3 "${CLAUDE_PLUGIN_ROOT}/bin/context-kernel" setup --yes --claude no --judge <local|hosted|keep> --scope <scope>`, adding `--allow-hosted` only if the user chose hosted and explicitly agreed that their memory text may be sent to it, and `--codex "<project folder>"` if they asked for Codex.
   - If they chose hosted and jev has no key yet, tell them to run `jev auth set <key>` themselves in a terminal (never ask for the key in chat), then run this again.
4. Tell the user in two or three lines what is now configured, and that a line above the prompt will say after each turn what memory saved or did not save, with undo. Plugin option changes (scope, database) take effect in a new session.
