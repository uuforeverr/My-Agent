---
name: file-summary
description: Summarize workspace files clearly and write concise summaries when requested.
---

# File Summary Skill

Use this skill when the user asks to summarize one or more files.

Steps:

1. Inspect the workspace with `glob` if the target file is ambiguous.
2. Read the relevant file with `read_file`.
3. Produce a concise summary in the user's language.
4. If the user asks to save the summary, write it with `write_file`.

Keep summaries short unless the user asks for detail.
