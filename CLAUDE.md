# CLAUDE.md — LOCAL-Intelligence

Guidance for Claude (and humans) working on this repo. Read this before making changes; it encodes non-obvious decisions and hard-won pitfalls that are easy to re-break.

---

## What this is

`LOCAL-Intelligence` is a fully local, offline CLI AI agent. It runs a Gemma model on the user's own machine via [Ollama](https://ollama.com) and gives it real tools — read/write/edit/delete files, run shell commands, read PDFs and Office documents, search the web, and understand images — with no cloud, no API keys, nothing leaving the machine. Think "local Claude/Codex CLI."

The installed command is **`gemma`**. Primary entry: `gemma go` (interactive chat, anchored in the current folder).

- **Language:** Python 3.10+. **Deps:** `requests`, `rich`, `pyyaml`, `send2trash`, `pillow`, `prompt_toolkit`, `psutil`, plus the document backends `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `striprtf`, `xlrd` (all MIT/BSD, pure/prebuilt, small — keep it that way).
- **Model:** `gemma4:12b` by default (7.0 GB download); `gemma4:e4b` is the edge fallback. Note `e4b` is the LARGER download (9.0 GB, measured from the registry manifest) despite the "small model" framing — its advantage is fitting an 8 GB GPU fully, not being smaller on disk.
- **Backend:** Ollama's HTTP API (`/api/chat`, native function-calling). Never talk to Ollama any other way.

---

## Architecture

### Request flow
`gemma go` → `main.py` chooses a REPL → user prompt → `agent.run_turn()` streams from Ollama `/api/chat` with tool schemas → when the model emits a tool call, the bridge executes it locally via `tools/executor.py` → feeds the result back → loops until a final answer → renderer prints it.

### Module map
| File | Responsibility |
|------|----------------|
| `gemma_cli/main.py` | CLI arg parsing, the two REPLs (`_repl_plain`, `_repl_interactive`), shared `_handle_command`, one-shot path |
| `gemma_cli/agent.py` | `run_turn()` — the streaming tool loop; also the reliability harness (compaction, malformed-tool-call rescue, loop detection, empty-turn nudge) and cooperative `cancel` |
| `gemma_cli/config.py` | Layered config: defaults → `config.yaml` → env (`GEMMA_*`) → CLI flags; pushes runtime settings into tool modules via `apply_to_tools()` |
| `gemma_cli/sysprompt.py` | Builds the system prompt, templated from the host (user, host, OS, cwd + listing, date/time, memory, skill index) |
| `gemma_cli/render.py` | `Renderer` — rich-based output for the one-shot / plain paths |
| `gemma_cli/sessions.py` | Save/restore conversations under `.gemma/sessions/` (per project) |
| `gemma_cli/updater.py` | `gemma update` — locate the checkout, git pull, reinstall (deferred on Windows) |
| `gemma_cli/skills.py` | Skill discovery, frontmatter parsing, the system-prompt index, capture-from-transcript, usage reporting |
| `gemma_cli/statusline.py` | GPU/CPU/VRAM sampling (nvidia-smi + psutil) + toolbar string for the live REPL |
| `gemma_cli/clipboard.py` | Grab an image off the clipboard (Pillow `ImageGrab`) for `/paste` and Alt+V |
| `gemma_cli/tools/` | `definitions.py` (schemas), `executor.py` (dispatch), `file_tools.py`, `doc_tools.py`, `shell_tools.py`, `web_tools.py`, `memory_tools.py`, `plan_tools.py`, `skill_tools.py` |
| `install.ps1` / `install.sh` | Idempotent, self-updating installers (Windows / POSIX) |
| `README.md` / `docs/USAGE.md` | README is **installation only** — keep it that way; usage docs go in `docs/USAGE.md` |

### Module contracts (the non-obvious ones)

- **`run_turn()` is a generator, not a printer.** It yields typed events (`think`, `text`,
  `tool_start`, `tool_result`, `notice`, `error`, `done`) and mutates the caller's `messages` list
  in place. That is what lets one loop drive the rich renderer, the plain worker-thread renderer,
  and tests. Never print from `agent.py`.
- **Config is *pushed* into tools, not pulled.** `config.apply_to_tools()` writes module-level
  globals in `file_tools` (write roots), `web_tools` (SearXNG URL), `memory_tools` (memory
  paths) and `skill_tools` (`allow_model_skills`). Tools never import config. Tests must call the setters (`set_allowed_write_roots`,
  `configure`) to isolate themselves.
- **Every tool takes one dict and returns a `str`.** Errors are returned as readable strings for
  the model, never raised — `execute_tool` catches anything that escapes.
- **The system prompt is rebuilt every launch** (date, cwd, cwd listing, write roots, memory) and
  is deliberately excluded from saved sessions, which store `messages[1:]` only.
- **Compaction cuts at a user-message boundary** so a `tool` message is never orphaned from the
  `assistant` message carrying its `tool_calls`. Ollama rejects that shape.

### Adding a tool
1. Function in the right `tools/*.py` returning a `str`.
2. Schema (Anthropic-style `input_schema`) in `tools/definitions.py` — `OLLAMA_TOOLS` auto-derives.
3. Dispatch entry in `tools/executor.py` `_DISPATCH`.
4. Optionally a discipline line in `sysprompt.py`.
5. A pytest in `tests/`.

---

## Key design decisions

- **Two REPLs.** `_repl_plain` (default) is a simple synchronous line-by-line reader — reliable, renders answers cleanly. `_repl_interactive` (`--live` / `live_repl: true`) adds a type-ahead queue, Esc-cancel, and a live GPU/CPU status line via `prompt_toolkit` + `patch_stdout` + a worker thread. **The live REPL is experimental and off by default** (see pitfalls).
- **Write scope is fenced.** File writes/edits/deletes are restricted to `allowed_write_roots` (home + temp + the launch cwd). The shell tool blocks a per-OS list of destructive commands. This is a guard-rail, not a sandbox — the agent runs with the user's own privileges by design.
- **Recoverable by default.** Edits back up the prior version to `.gemma/backups/`; `delete_file` routes to the OS trash.
- **Small-model reality shapes everything.** `edit_file` uses exact-string replacement (a 12B rewriting a whole file drops content). The reliability harness (compaction, tool-call rescue, loop detection) exists because small models fumble; patterns adapted from the MIT-licensed `lutelute/local-cli`.
- **Config is the seam for remote GPUs.** `ollama_url` can point at another machine's Ollama, so a weak-GPU laptop can borrow a stronger networked GPU without new hardware.
- **Document reading is a targeted dispatcher, not a mega-library.** `read_document` picks a small backend per format (pypdf / python-docx / openpyxl / python-pptx / striprtf / xlrd) and uses the standard library alone for OpenDocument, EPUB, `.eml`, `.ipynb` and CSV. **Do not swap this for `markitdown`:** its *base* install pulls `magika` → `onnxruntime` + `numpy` + `protobuf` (~300 MB of ML runtime, just for file-type sniffing) and still ships none of the parsers — they are extras. That breaks the "small, offline, prebuilt wheels" constraint. Same reasoning rules out `extract-msg` for `.msg`: 20+ transitive packages including GPLv3/LGPLv3, in an MIT repo.
- **Skills load progressively — never inline a body in the system prompt.** `skills.index_block()` emits names + one-line descriptions only (~15 tokens each); bodies load when a skill is actually invoked (`/<name>`, or the model calling `load_skill`). This is the whole design: twenty skill bodies inlined would cost a third of a 32K window before the user types anything. If you add a field to the index, check what it costs × 40 skills.
- **Skill names are a security boundary, not a label.** They become slash commands *and* filenames, so `skills._SAFE_NAME` is anchored and rejects dots and separators — a `name:` in frontmatter can never escape the skills dir. A frontmatter name that fails validation falls back to the filename stem. Keep it that way.
- **`gemma update` must never reinstall in-place on Windows.** (And finding the shim is half the battle: `launcher_path()` must consult PATH and the per-user scripts scheme, not just `sys.executable`'s folder. Beside-the-interpreter is only true in a venv; with system Python under Program Files pip does a per-user install and the shim lands in `%APPDATA%PythonPythonXYScripts`. Getting this wrong silently disables the deferred path.) Windows holds `gemma.exe` open while it runs; a pip reinstall launched from inside a running `gemma` is exactly what leaves a corrupt `~ocal_intelligence*.dist-info` behind and jams the *next* install (see the install-failure pitfall below). So the git pull runs inline and the install is handed to a helper that waits on the parent PID first. Two details that matter: the lock is **tested**, not inferred — `_launcher_is_locked()` asks Windows for a write handle on `gemma.exe`, because `sys.argv[0]` varies with whichever console-script launcher pip generated — and the helper waits using stdlib `ctypes`, **not `psutil`**, since a compiled extension can fail to import at runtime and the updater is the one tool that must still work when the environment is broken.
- **A skill body is executed instructions.** It is the user's own file on their own machine, so the trust model is the same as a shell script — but that is why `load_skill` is gated by `allow_model_skills` and why the docs warn about skills from other people.
- **Format is decided by content, not extension.** `doc_tools._sniff` reads magic bytes and looks *inside* ZIP containers for the marker entry (`word/document.xml`, `xl/workbook.xml`, `mimetype`), so renamed files still work. Extraction output is labelled by page/sheet/slide and capped by `max_chars` with `offset`/`limit` paging — a 400-page PDF must never be handed whole to a 32K context.

---

## Pitfalls (hard-won — do not re-break these)

### Windows / PowerShell
- **Keep `install.ps1` pure ASCII.** Windows PowerShell 5.1 reads a no-BOM UTF-8 file as cp1252, so a stray em-dash (`—`) or other non-ASCII byte desyncs the parser and the whole script fails to run. PowerShell 7 hides this (reads UTF-8), so it passes locally and breaks on 5.1. `.gitattributes` pins `*.ps1` to CRLF and `*.sh` to LF.
- **Emoji/box chars crash the legacy console.** `main.py` calls `sys.stdout/stderr.reconfigure(encoding="utf-8")` at startup so rich doesn't crash on cp1252 consoles. Keep it.
- **`ollama` isn't on PATH in terminals opened before Ollama was installed.** The installer must detect Ollama at its known install path (`%LOCALAPPDATA%\Programs\Ollama\ollama.exe`), not just via PATH — otherwise it needlessly re-downloads Ollama and the user has to open a fresh terminal.

- **Never let a `git` call abort `install.ps1`.** `git ... 2>$null` or `2>&1` inside the script wraps git's stderr in an ErrorRecord that THROWS under `$ErrorActionPreference = "Stop"` — so a merely chatty git kills the install before it does anything. This shipped and bit a real user: a clone made by another account produced `fatal: detected dubious ownership`, and the installer died at the branch-detection line having installed nothing. Every git call in the self-update block now runs with the preference relaxed and is judged by `$LASTEXITCODE`; dubious ownership is detected by name and answered with `git config --global --add safe.directory '<repo>'`. The self-update is a convenience — nothing in it may stop an install.

### Ollama / model
- **`install.ps1` hangs after installing Ollama when its output is redirected.** Observed on a clean machine (no prior Ollama, no Python) running `powershell -File install.ps1 -SkipSearch` with stdout piped to a file: Ollama installed fine (0.33.3, API answering), then the script blocked — 0% CPU, no child processes, never reaching the model pull. Cause unconfirmed; the leading suspect is `Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden` inheriting the script's stdout handle and never releasing it. It may not reproduce interactively, which would explain why it was never noticed. **Repro: run the installer with output piped to a file.** Until it is fixed, a clean install can be finished by hand: `ollama pull gemma4:12b` then `pip install <repo>`.
- **The 35% CPU spill is measured, not estimated.** On an RTX 2070 (8 GB) with `num_ctx: 32768`, `ollama ps` reports exactly `35%/65% CPU/GPU` for `gemma4:12b` — 5.89 GB in VRAM, 3.11 GB on CPU, 7714/8192 MiB used. A trivial one-line prompt takes ~2 minutes cold. This matches the estimate below.
- **winget's Ollama package lags** (was 0.31.2 when gemma4 needed ≥ 0.32). The installer downloads `OllamaSetup.exe` from ollama.com directly. `gemma4` requires Ollama ≥ 0.32.
- **8 GB VRAM cannot fully fit `gemma4:12b`.** Weights alone are ~7.6 GB; with any KV cache the footprint exceeds ~6.5 GB usable VRAM, so ~35% of layers spill to CPU (the slowness). Mitigations, in order: lower `num_ctx` (32K→16K), set `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`, close GPU-hungry apps, or switch to `gemma4:e4b` (fits fully). None make 12B 100% GPU on 8 GB — that's a hardware ceiling. Verify with `ollama ps` mid-generation (the `PROCESSOR` column) or `GET /api/ps` (`size_vram` vs `size`).
- **Small models are limited by design.** `gemma4:e4b` (~4-8B) is good only for mechanical/agent tasks (tool-calling, RAG, instruction-following), not reasoning/synthesis. 12B is the floor for logic. Don't benchmark it against frontier cloud models.

### Install failures (this bit us repeatedly)
- **A running `gemma` locks `gemma.exe`.** `pip install` then fails with `WinError 32 ... gemma.exe ... being used by another process`, and — worse — can leave a corrupted `~ocal_intelligence*.dist-info` marker in site-packages that jams every subsequent install while the package files are already gone. **Always exit all `gemma` sessions before reinstalling.** If it's already broken: kill stray `gemma` processes, delete the `~*` leftover from site-packages, then `pip install --force-reinstall .`.
- **Verify installs from OUTSIDE the repo folder.** `python -c "import gemma_cli"` run *inside* the repo imports the local source (cwd shadows site-packages) and gives a false "OK" even when the installed package is broken. Check from `C:\` (or anywhere else), or run `gemma --version`.
- **Google Drive paths can lock files mid-build** (the dev copy lives on a synced drive). Occasional pip build failures there are sync locks, not code errors.

### SearXNG (web search)
- **Newer SearXNG images 403 the JSON API.** Their shipped `settings.yml` already contains the word "format", so a naive "append `formats: [json]` if not present" check is skipped and the JSON API stays disabled → 403. The installer must **overwrite** `settings.yml` with a complete known-good file (limiter off, `formats: [html, json]`), not append.

### The live REPL / rich + prompt_toolkit (why it's off by default)
- **rich output from a background thread under `patch_stdout` is unreliable.** Symptoms seen: color codes leaking as literal `?[2m ... ?[0m`, then the answer vanishing entirely. Root cause: rich's terminal control from a non-main thread fights prompt_toolkit's screen management. The worker path now renders with plain `print(flush=True)` (`_consume_plain`), not rich. Even so, output can still tangle with the input line on some Windows consoles — hence the live REPL is opt-in and the plain REPL is the default. **Do not make the live REPL the default without validating on a real Windows terminal** (it cannot be verified from a non-TTY dev environment).
- **The installer's "git pull failed" can be a false alarm.** git writes progress to stderr; PowerShell 5.1 flags stderr as failure. If `git pull --ff-only` says "Already up to date" manually, the pull actually worked.

### Hardware
- **The target laptop (MSI GF63 Thin) has no Thunderbolt/USB4** — its USB-C is data-only USB 3.2. A plug-and-play eGPU is impossible; only an OCuLink+M.2-adapter DIY route exists. Confirm any laptop's Thunderbolt before recommending an eGPU.

---

## Current state (v0.5.0)

**Working and shipped (default `gemma go`, the plain REPL):**
- Agentic tool loop; tools: `read_file`, `read_document`, `write_file`, `edit_file`, `delete_file`, `shell`, `glob`, `grep`, `list_directory`, `web_search`, `web_fetch`, `remember`, `load_skill`, `set_plan`, `complete_step`.
- Project + global memory (`GEMMA.md` auto-load + `remember`), session save/`--resume`, backup-on-write, trash-not-delete, approval mode (`--approve`), context compaction, current-date grounding, vision (image input), local SearXNG web search.
- `/paste` (clipboard image) works in both REPLs. Installers self-update (`git pull` + reinstall, skipping big downloads).
- Document reading (`read_document`): PDF, Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, `.eml`, `.ipynb`, CSV/TSV, HTML; content-sniffed format detection, page/sheet/slide paging, LibreOffice fallback for legacy `.doc`/`.ppt`.
- Skills: markdown procedures in `skills/` (project) and `<config_dir>/skills/` (global), run as `/<name>`, captured from a transcript with `/skill new <name>`, reported by `/skills` and `gemma skills`. Index-only system-prompt injection; `load_skill` tool gated by `allow_model_skills`.
- Human-writable memory surfaced with `/memory` and `/memory global` (the files themselves predate this).
- `gemma update` from any folder: finds the checkout, pulls, reinstalls; `--check`, `--full`, `--repo`.
- 141 pytest cases (`tests/test_doc_tools.py` builds a real file per format — no mocks; `tests/test_skills.py` redirects the global skills dir into tmp so a run never touches the real config dir).

**Experimental (opt-in via `--live`):**
- Live REPL: type-ahead queue, Esc-to-cancel, live GPU/CPU/VRAM status line. Functional but can render awkwardly on some Windows consoles. Needs real-terminal validation before promotion.

**Branches:** `dev` is the default/working branch (current). `main` is the stable release branch. Feature work happens on `feat/*` / `fix/*` branches, PR'd into `dev`, then `dev` is merged to `main` when stable.

---

## Dev workflow

```bash
# from a clone
pip install -e .            # or: pip install .
pytest -q                   # run the test suite (141 cases)
gemma --version             # verify the installed entry point
gemma "what is 2+2"         # one-shot smoke test (needs Ollama running)
```

- **Branch → PR → `dev` → merge to `main`.** Don't commit straight to `dev`/`main`; open a PR.
- **Installers are the deploy path.** On a target machine: `git clone` (or ZIP) then `install.ps1` / `install.sh`. Re-running an installer self-updates. Config lives at `%APPDATA%\gemma-cli\config.yaml` (Windows) / `~/.config/gemma-cli/config.yaml` (POSIX).
- **Keep it secret-clean.** This repo is public — no credentials, private IPs, internal hostnames, or personal paths in committed files.

## Parked / roadmap
- Fix or replace the live REPL rendering (consider a `prompt_toolkit` full `Application` or a different TUI lib; validate on real Windows terminals).
- Installer: auto-stop a running `gemma` before `pip install`; fix the false "git pull failed" stderr misread.
- Optional: point `ollama_url` at a stronger networked GPU for weak-VRAM machines. MCP client, project RAG index, and model routing were scoped but deferred.
