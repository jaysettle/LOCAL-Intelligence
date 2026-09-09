# Changelog

## 0.3.0 — Document reading

**New tool: `read_document`**
- Extracts text from the formats `read_file` cannot decode: PDF, Word (`.docx`), Excel
  (`.xlsx`/`.xls`), PowerPoint (`.pptx`), OpenDocument (`.odt`/`.ods`/`.odp`), RTF, EPUB,
  email (`.eml`), Jupyter notebooks, CSV/TSV and HTML.
- **Content beats the extension** — format is decided by magic bytes, and for ZIP containers by
  the marker entry inside, so a `.docx` renamed to `.doc` still reads correctly (with a note).
- **Structured, capped output** — labelled pages / sheets / slides / parts with `offset`+`limit`
  paging and a `max_chars` cap, so a long PDF cannot blow a 32K context.
- Scanned PDFs report that they have no text layer instead of returning silence; encrypted PDFs
  say so; legacy `.doc`/`.ppt` convert via LibreOffice when installed, otherwise explain the fix.
- `read_file` now redirects binary document formats to `read_document` rather than returning
  mojibake, and the system prompt tells the model which tool to reach for.

**Dependencies** — `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `striprtf`, `xlrd`. All
MIT/BSD, pure Python or prebuilt wheels. Deliberately *not* `markitdown`, whose base install pulls
`onnxruntime`+`numpy`+`protobuf` (~300 MB) for file-type sniffing and still ships no parsers; and
deliberately not `extract-msg` for `.msg`, which pulls 20+ packages including GPLv3/LGPLv3.

**Docs** — README is now installation-only; everything else moved to `docs/USAGE.md`.

**Tests** — 27 new cases (60 total), each building a real file of the format under test.

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
