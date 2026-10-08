---
description: Check Shelflife and choose its judge (Ollama here or on another machine of yours, or paid jev), scope and clients
---

Set up Shelflife for the user. Speak in the user's language and in plain words; never show them internal ids.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/bin/shelflife-context" doctor` and read the result.
   - If jev is missing, tell the user the kernel needs jevmate and how to install it (`/plugin marketplace add GastonGelhorn/jevmate`, then `/plugin install jevmate@gastongelhorn`), and stop.
   - If doctor warns that hooks run twice, tell the user which project files also run the kernel and offer to remove only the kernel's entries from them.
2. Ask the user (one AskUserQuestion with these questions):
   - Judge: "local" (jev on Ollama on this machine: free, private, about 0.3 s per prompt once warm), "remote" (jev on Ollama on another machine of theirs, such as a Mac on their Tailscale network: free, and memory stays on their machines; ask for its address), "hosted" (jev's paid service: faster, but the text of their memory and messages leaves the machine), or "keep" the current one. Mention that jev's backend is shared with jevmate.
   - Scope: keep the current one (from doctor) or name another (work, personal, a client).
   - Codex: whether to also wire Codex for this project.
3. Apply the answers with `python3 "${CLAUDE_PLUGIN_ROOT}/bin/shelflife-context" setup --yes --claude no --judge <local|remote|hosted|keep> --scope <scope>`, adding `--judge-url "<address>"` for remote, `--allow-hosted` only if the user chose hosted and explicitly agreed that their memory text may be sent to it, and `--codex "<project folder>"` if they asked for Codex.
   - For local or remote, jev itself prepares the Ollama (jevmate 1.10 or newer). If the output says tev1 is missing, ask before running again with `--pull`: a 4.5 GB download on that machine. If it says jevmate is too old, tell the user to update it (`/plugin update jevmate@gastongelhorn`).
   - Remote names that machine as theirs in `~/.shelflife-context/config.json`, so memory text may go there without allowing a hosted judge. Say so in one line: Ollama has no password, so it should be a machine of theirs on a network they trust.
   - If they chose hosted and jev has no key yet, tell them to run `jev auth set <key>` themselves in a terminal (never ask for the key in chat), then run this again.
4. Tell the user in two or three lines what is now configured, and that a line above the prompt will say after each turn what memory saved or did not save, with undo. Plugin option changes (scope, database) take effect in a new session.
