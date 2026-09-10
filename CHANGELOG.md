# Changelog

## 0.5.5 — Live REPL output no longer collapses into the prompt row; Ctrl+C stops the answer

**Live REPL (`--live`).** Observed on a real Windows terminal: the answer streamed into a one- or
two-word fragment stuck bottom-left, next to the prompt, while blank space grew above it. The
cause was precise: `_consume_plain` printed every streamed token with `end=""` and `flush=True`.
prompt_toolkit's `patch_stdout` renders a *complete* line cleanly above the prompt, but a flushed
*partial* line is written on the prompt's own row and redrawn on every refresh — once per token.
Streamed text is now buffered and only whole lines are printed: at newlines, or soft-wrapped at
the last space once a line passes 100 characters. Output arrives line by line instead of token
by token, which is the price of it rendering at all under `patch_stdout`.

**Plain REPL (default).** Ctrl+C mid-answer used to escape as a `KeyboardInterrupt` traceback and
kill the whole session. It now stops the current answer, keeps the conversation, and leaves the
transcript well-formed (a stopped turn gets a placeholder assistant message, so the next request
is not two user turns in a row). A turn is minutes long on an 8 GB box; a way out that does not
cost the session is not optional.

**Tests** — 10 new, including one asserting the invariant behind the fix: `print` is never called
with `end=""` in the live renderer.

## 0.5.4 — No external command can abort the installer

Second report of the same bug class, from a machine with no real Python:

```
python.exe : Python was not found; run without arguments to install from the Microsoft Store...
At install.ps1:177 char:18
+         $vraw = (& $cand --version 2>&1) | Out-String
```

0.5.2 fixed this for git and stopped there. It is not a git problem — it is every external command
in the script. PowerShell 5.1 wraps a native command's stderr in an ErrorRecord that **throws**
under `$ErrorActionPreference = "Stop"`, so any chatty tool kills the install. Here the `python`
on PATH was the Microsoft Store alias stub, whose "Python was not found" is a perfectly good
*answer* to "is python here?" — and the installer died on it, never reaching the step that would
have installed real Python.

- New `Invoke-Native` helper runs an external command with the preference relaxed and returns
  `@{ Output; Code }`. Every probe in the script goes through it. Judge by `Code`, never by
  whether stderr spoke.
- Python detection recognises the Store placeholder by name and keeps looking, also trying
  `python3`, and reports a too-old Python instead of silently skipping it.
- After a winget install it **re-probes** rather than assuming `python` now works. A fresh install
  is often invisible to the current process; it now says to open a new terminal instead of failing
  later with a confusing pip error.
- `Get-OllamaVersion` goes through the helper too: chatty output there previously read as "no
  ollama", which would re-download a gigabyte for nothing.

Verified by reproducing the reported failure: the old line throws with the exact message from the
report, the new one recognises the placeholder and continues.

## 0.5.3 — Find gemma.exe where pip actually put it

`gemma update` was taking the inline install path on a real machine where it should have deferred.
`launcher_path()` looked for `gemma.exe` beside `sys.executable`, which is true in a venv — python.exe
and gemma.exe share `Scripts\` — and false for the common system-Python layout: with Python under
`C:\Program Files`, pip falls back to a per-user install and the shim lands in
`%APPDATA%\Python\PythonXY\Scripts`. Finding nothing, it concluded "not locked" and reinstalled in
place, which is exactly the situation the deferred path exists to avoid.

It happened to succeed, because pip renames a running executable aside rather than failing outright.
That is luck, not correctness — the leftover it can strand is the same `~`-prefixed wreckage that
jams later installs.

PATH is now consulted first (that is the shim the user actually invoked), then the interpreter's own
script directories including the per-user scheme, then the venv-style locations.

## 0.5.2 — The installer no longer dies on a chatty git

Reported from a real install: the script aborted before doing anything, with

```
git : fatal: detected dubious ownership in repository at 'C:/Users/.../LOCAL-Intelligence'
At install.ps1:73 char:20
```

`git ... 2>$null` looks harmless, but PowerShell 5.1 wraps a native command's stderr in an
ErrorRecord, which **throws** under `$ErrorActionPreference = "Stop"`. So any git complaint —
"dubious ownership" from a clone made by another account, or ordinary progress output — killed
the whole install before Ollama, the model or the CLI were touched.

Every git call in the self-update block is advisory now: run with the preference relaxed, judged
by exit code, and nothing there can stop the install. Dubious ownership is detected specifically
and answered with the command that fixes it:

```
git config --global --add safe.directory '<repo>'
```

Verified by reproducing the original failure: the old code throws, the new code warns and carries
on. Also restores the pre-pull hash of `install.ps1`, without which the "installer updated itself,
re-run it" path would have silently stopped working.

## 0.5.1 — Fixes found by running against a real model

First run against live `gemma4:12b` on an 8 GB GPU (measured 35%/65% CPU/GPU spill). It found
things no unit test could:

- **`/skill new` could time out and throw the capture away.** `_chat_once` posted `"stream": false`,
  so Ollama sends nothing until generation completes and the read timeout has to cover the entire
  generation — fine on fast hardware, a coin flip when the model spills to CPU. It observably lost a
  real capture after 600s. `_chat_once` now streams, so the timeout means the gap between chunks,
  which is what it always should have meant. `_maybe_compact` uses the same call and had the same
  latent failure, silently.
- **A failed capture now saves the transcript as an editable draft** instead of discarding the work
  the user just asked to keep.
- **`/skill new` shows progress** while generating — on CPU-spilled hardware it runs for minutes,
  and a silent terminal is indistinguishable from a hang.
- **`gemma4:e4b` is the LARGER download** (8.95 GB vs 7.04 GB for `12b`), measured from the registry
  manifest. README, CLAUDE.md and USAGE.md all called it the "smaller/faster" model, which steered
  people toward the bigger download. Reworded to what is actually true: it fits an 8 GB GPU fully.
- **`install.ps1` judges `git pull` by its exit code**, not by whether the pipeline threw — git
  writes progress to stderr and PowerShell 5.1 turns that into a terminating error.
- CLAUDE.md records two findings: the measured 35%/65% split on an RTX 2070, and an unresolved
  installer hang after the Ollama step when output is redirected (with a repro).

**Tests** — 4 new cases (147 total), including one asserting `stream` is never flipped back to false.

## 0.5.0 — `gemma update`

Update from any folder, in any terminal, without finding the repo first:

```
gemma update            # pull the latest source and reinstall
gemma update --check    # report whether new commits exist; install nothing
gemma update --full     # run the platform installer too (Ollama, model, SearXNG)
gemma update --repo <path>
```

- **Finds the checkout**: `--repo`, then `repo_dir` in config, then the package's own location
  (editable installs), then the working directory and its parents, then `~/LOCAL-Intelligence`.
  The first run records the location; the installers now record it at install time too. With no
  checkout at all it offers to clone one, so ZIP downloads get working updates.
- **The Windows lock is handled properly.** Windows holds `gemma.exe` open while it runs, and a
  pip reinstall from inside a running `gemma` is what leaves a corrupt `~ocal_intelligence*.dist-info`
  behind and jams the *next* install. So the git pull happens inline and the install is handed to a
  helper that waits for the process to exit first. Whether the lock exists is tested directly — by
  asking Windows for a write handle on `gemma.exe` — rather than inferred from `sys.argv[0]`, whose
  shape depends on which console-script launcher pip generated.
- The deferred helper waits on the parent PID with stdlib `ctypes`, not `psutil`: a compiled
  extension can fail to import at runtime, and the updater must not be the thing that breaks.
- Pull success is judged by git's return code, never by whether stderr had output — git writes
  progress to stderr, which is the old "git pull failed" false alarm.
- Installer output is written to `<config_dir>/last_update.log`.

**Tests** — 39 new cases (141 total).

## 0.4.0 — Skills, and memory you can actually find

**Skills** — reusable procedures saved as markdown, run as `/<name>`.
- `<project>/skills/<name>.md` and a global `<config_dir>/skills/<name>.md`; project wins on a
  name collision. YAML frontmatter (`name`, `description`, `when`) plus a body of steps.
- **Progressive disclosure**: only the index (names + one-line descriptions) goes into the system
  prompt — ~15 tokens per skill. Bodies load on invocation. Inlining twenty skill bodies would
  spend a third of a 32K context before the user typed anything.
- `/skill new <name>` writes a skill from the conversation that just happened: the model is asked
  to generalise the procedure, keep the commands that stay the same, and record what went wrong.
- `load_skill` tool lets the model pull in a skill itself when the request matches its `when`.
  Disable with `allow_model_skills: false` if a small model over-triggers; `/<name>` still works.
- Reporting: `/skills` in the REPL and `gemma skills [folder]` from the shell — description, scope,
  use count, last used, and whether each use came from the command or the model. Counts live in
  `.gemma/skill_usage.json` (project-local, gitignored).
- Skill names are validated against an anchored pattern, so a name from frontmatter can never
  escape the skills directory.

**Memory** — the human-writable memory files were always there; now they're findable.
- `/memory` opens the project `GEMMA.md`, `/memory global` the global one, in `$EDITOR` (or the
  system default on Windows). Both were already auto-loaded into the system prompt every run.

**Tests** — 44 new cases (104 total).

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
