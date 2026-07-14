# Changelog

## 0.2.0 — Agent upgrades: tooling, memory, safety

**Tooling**
- `edit_file` — surgical exact-string replacement (unique match or `replace_all`);
  the safe, cheap way for a small model to change part of a file. On a near-miss
  it shows the closest matching lines to copy. `write_file` is now whole-file-only.
- `delete_file` — sends files to the OS trash / Recycle Bin (recoverable) instead
  of an irreversible `rm`.

**Memory**
- Auto-loads `GEMMA.md` (project) and a global memory file into the system prompt.
- `remember` tool appends durable facts (project or global scope).

**Sessions**
- Conversations autosave to `.gemma/sessions/`. `gemma go --resume` restores the
  latest; `/save`, `/resume`, `/sessions` manage them in the REPL.

**Reliability** (patterns inspired by the MIT-licensed lutelute/local-cli)
- Context compaction: summarizes old turns when the transcript nears `num_ctx`.
- Malformed tool-call rescue and empty-turn nudge.
- Loop detection: intervenes when the model repeats the same call.

**Safety**
- Backup-on-write: prior file versions are copied to `.gemma/backups/`.
- Approval mode: `--approve writes|all` gates mutating actions on a y/N prompt.
- Shell blocklist now checks the union of Windows + POSIX dangerous patterns.

**Plan scaffold**
- `set_plan` / `complete_step` maintain an in-session checklist to keep the model
  on task for multi-step work.

**Project**
- `tests/` pytest suite (27 cases). `send2trash` added as a dependency.

## 0.1.0 — Initial release
- Local CLI agent: Ollama + gemma4:12b, agentic tool loop, filesystem/shell/web/
  vision tools, `gemma go` project-folder awareness, Windows/Linux installers.
