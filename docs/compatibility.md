# Compatibility

End-to-end checks run against the real clients (`python3 -m tests.compat_matrix`). Each row is one run; the newest first. A check is the whole flow through the client's own hooks and MCP server, judged from the kernel's database, not from the model's answer alone.

| When (UTC) | Kernel | jev | Check | Client | Result | Failed |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-07 08:53 | 0.9.0 | jev 1.9.3 | claude-v04 | 2.1.287 (Claude Code) | pass 10/10 |  |
| 2026-10-07 08:53 | 0.9.0 | jev 1.9.3 | claude-v06 | 2.1.287 (Claude Code) | pass 9/9 |  |
| 2026-10-07 08:53 | 0.9.0 | jev 1.9.3 | claude-v09 | 2.1.287 (Claude Code) | pass 5/5 |  |
| 2026-10-07 03:28 | 0.8.0 | jev 1.9.3 | claude-v04 | 2.1.287 (Claude Code) | pass 10/10 |  |
| 2026-10-07 03:28 | 0.8.0 | jev 1.9.3 | claude-v06 | 2.1.287 (Claude Code) | pass 9/9 |  |
| 2026-10-07 02:19 | 0.8.0 | jev 1.9.3 | claude-v04 | 2.1.287 (Claude Code) | pass 10/10 |  |
| 2026-10-07 02:19 | 0.8.0 | jev 1.9.3 | claude-v06 | 2.1.287 (Claude Code) | flaky 9/9 | first attempt failed: recommendation_linked_to_the_record, review_flagged_in_a_fresh_session |
| 2026-10-07 02:13 | 0.8.0 | jev 1.9.3 | claude-v04 | 2.1.287 (Claude Code) | pass 10/10 |  |
| 2026-10-07 02:13 | 0.8.0 | jev 1.9.3 | claude-v06 | 2.1.287 (Claude Code) | FAIL 7/9 | recommendation_linked_to_the_record, review_flagged_in_a_fresh_session |

A failed check is run once more, since the agent writes a new answer each time: `flaky` means it passed on the second attempt, and the first attempt's failures are listed. Full reports are kept in ~/.shelflife-context/compat-runs. Codex rows need a pilot folder whose hooks were trusted in Codex once by hand; without one the run skips Codex.
