# Using LOCAL-Intelligence

Everything past installation. For install steps see the [README](../README.md).

---

## Running it

`gemma` is project-aware: `cd` into a folder and it orients itself there — it sees the files,
relative paths resolve there, the shell runs there, and it can edit files there.

```powershell
cd C:\path\to\my-project
gemma go                                   # interactive chat, anchored here
gemma go C:\path\to\other-project          # or point it at a folder
```

Other forms:

```bash
gemma                                      # same as `gemma go`
gemma "what changed in this repo today?"   # one-shot, prints and exits
gemma -p "summarize todo.md"               # one-shot (explicit flag form)
gemma -i photo.jpg -p "what is this?"      # attach an image (vision)
gemma --model gemma4:e4b                   # edge model; fits an 8 GB GPU fully
gemma --no-thinking                        # hide the model's reasoning
gemma --verbose                            # show full tool output
gemma go --resume                          # pick up the last session
gemma go --approve writes                  # y/N prompt before any change
```

### Updating

```
gemma update            # pull the latest source and reinstall
gemma update --check    # report whether new commits exist; install nothing
gemma update --full     # run the full installer too (Ollama, model, SearXNG)
gemma update --repo C:\path\to\LOCAL-Intelligence   # if it can't find your checkout
```

It finds your source checkout in this order: `--repo`, the path remembered in `config.yaml`, the
package's own location (editable installs), the current folder and its parents, then
`~/LOCAL-Intelligence`. The first successful run records the location so later ones are instant.
If there's no checkout at all — a ZIP download, say — it offers to clone one.

**On Windows the install happens in a second window.** Windows holds `gemma.exe` open while it
runs, so the update pulls the source, then hands the install to a helper that waits for `gemma` to
exit first. Doing it in-place is what leaves a corrupt `~ocal_intelligence*.dist-info` behind and
jams the *next* install. The output is also written to `%APPDATA%\gemma-cli\last_update.log`.

If an update ever does fail with `WinError 32`, a `gemma` session is still running somewhere —
close every one and re-run.

### REPL commands

`/paste` · `/image <path> <prompt>` · `/clear` · `/model <tag>` · `/save` · `/resume [name]` ·
`/sessions` · `/memory [global]` · `/skills` · `/skill new <name>` · `/<skill-name>` · `/help` · `/exit`

### Live REPL (opt-in)

`gemma go --live` keeps the input live while the model answers:

- **Type-ahead queue** — start typing your next prompt while it's still responding; Enter queues it.
- **Esc** — stops the current response (queued prompts still run).
- **Alt+V** — pastes an image from the clipboard (snip with Win+Shift+S, then Alt+V).
- **Status line** — working folder plus live GPU / VRAM / CPU while it works (needs `nvidia-smi`).

It is experimental and can render awkwardly on some Windows consoles, which is why the plain
line-by-line reader is the default.

---

## What it can do

| Tool | Purpose |
|------|---------|
| `read_file` / `write_file` | Read files; write whole new files (fenced to allowed roots) |
| `read_document` | Read PDF, Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, email, notebooks, CSV |
| `edit_file` | Change part of a file by exact string replacement (safe for edits) |
| `delete_file` | Delete to the OS trash / Recycle Bin (recoverable) |
| `shell` | Run **PowerShell** (Windows) or **bash** (Linux/macOS) commands |
| `glob` / `grep` / `list_directory` | Find files, search contents, browse folders |
| `web_search` / `web_fetch` | Search the web via a local SearXNG instance and read pages |
| `remember` | Persist a durable fact to project or global memory |
| `load_skill` | Pull in a saved procedure when the request matches it |
| `set_plan` / `complete_step` | Keep a checklist for multi-step tasks |
| vision | Attach an image and ask about it (Gemma is multimodal) |

---

## Reading documents

`read_document` extracts text from formats `read_file` cannot decode. Ask in plain language —
"summarise contract.pdf", "what's in the Q3 sheet of budget.xlsx" — and the agent picks the tool.

| Format | Notes |
|---|---|
| **PDF** | Per-page text. Scanned/image-only PDFs report that they need OCR rather than returning nothing. |
| **Word** `.docx` | Paragraphs, headings, tables. |
| **Excel** `.xlsx` `.xls` | Per sheet, as rows. Caps at 200 rows/sheet. |
| **PowerPoint** `.pptx` | Per slide, including tables and speaker notes. |
| **OpenDocument** `.odt` `.ods` `.odp` | Standard library only, no extra dependency. |
| **RTF, EPUB, `.eml`, `.ipynb`, CSV/TSV, HTML** | Supported. EPUB reads in spine order. |
| **Legacy** `.doc` `.ppt` | 97-2003 formats have no pure-Python reader. Converted automatically if LibreOffice is installed; otherwise re-save as `.docx`/`.pptx`. |

Two behaviours worth knowing:

- **Content beats the extension.** A `.docx` renamed to `.doc` is still read correctly — the file's
  magic bytes decide, and you get a note saying so.
- **Long documents page.** Output is capped (default 20,000 chars) and labelled by page/sheet/slide.
  The agent is told to fetch more with `offset` only if it actually needs it — this keeps a 400-page
  PDF from blowing the context window.

`.msg` (Outlook) is deliberately unsupported: the only maintained reader pulls in 20+ packages
including GPL-licensed ones. Export to `.eml` instead.

---

## Memory — teach it things that stick

Two plain markdown files. **Both are yours to edit**; the agent also appends to them itself with
the `remember` tool.

| | File | Holds |
|---|---|---|
| **Project** | `GEMMA.md` in the project folder | Conventions, gotchas, who's who — anything specific to this codebase. Commit it. |
| **Global** | `%APPDATA%\gemma-cli\memory.md` | Facts about you and your machine that apply everywhere. |

```
/memory            # open the project GEMMA.md in your editor
/memory global     # open the global one
```

Both are loaded into the system prompt at every launch, so **edits apply on the next run**, not
mid-conversation. Keep them short — every line costs context on every single turn. A few dozen
lines of real conventions beats a hundred lines of history.

---

## Skills — teach it *how you do things*

A skill is a procedure saved as markdown and run with a slash command. The point: you work through
something fiddly once, save it, and next time it's one command.

```
/skill new deploy-api     # right after doing it — writes the procedure up from the conversation
/deploy-api               # next time
/deploy-api skip the smoke tests    # anything after the name is extra context for this run
/skills                   # what exists, and how often each gets used
```

Skills live in markdown files you can edit or hand-write:

```
<project>/skills/<name>.md          project skills — commit these
%APPDATA%\gemma-cli\skills\<name>.md   global skills — every project
```

A project skill shadows a global one with the same name.

```markdown
---
name: deploy-api
description: Deploy the API to staging and verify it
when: the user asks to deploy or ship the API
---
1. Run the test suite with the shell tool. Stop if anything fails.
2. Push to the staging remote.
3. Curl the health endpoint and confirm it returns 200.
```

`description` shows in the index and in `/skills`. `when` tells the model when a request should
trigger it — **write it as a trigger condition, not a summary**, or a small model will reach for
the skill constantly.

### How the model sees them

Only the **index** — names and one-line descriptions — goes into the system prompt. Twenty skills
cost a few hundred tokens; the full bodies would cost a third of the context window before you
typed anything. A skill body loads only when it actually runs.

The model can also pull a skill in itself via the `load_skill` tool when your request clearly
matches a `when` line. If your model over-triggers, turn that off — `/<name>` keeps working:

```yaml
allow_model_skills: false   # in config.yaml
```

### Capturing a skill

`/skill new <name>` sends the current conversation back to the model and asks it to write up the
procedure, generalised — replacing this run's specific values with notes about what varies, and
keeping what went wrong and how you fixed it. It saves the file and opens it.

**Read what it wrote.** The model only saw the transcript; it will occasionally generalise the
wrong thing. It's plain markdown — fix it. The new skill is available on the next launch.

`/skill edit <name>` and `/skill delete <name>` do what they say (delete goes to the recycle bin).

Usage counts are recorded in `.gemma/skill_usage.json`, project-local and gitignored. `gemma skills`
prints the same report without starting a session.

**One caution:** a skill body is instructions the agent executes. Treat a skill file from someone
else the way you'd treat a shell script from someone else.

---

## Sessions & safety

- **Sessions** — conversations autosave per folder; `gemma go --resume` picks up where you left off.
- **Recoverable by default** — edits back up the prior version to `.gemma/backups/`; deletes go to
  the Recycle Bin.
- **Approval mode** — `gemma go --approve writes` prompts y/N before any file change or shell command.

### Safety model

The agent runs with **your** user privileges — that's the point; it's your machine. Guard rails:

- **Writes** are restricted to `allowed_write_roots` (your home + temp + the launch folder).
- The **shell** tool blocks obviously destructive commands (drive formatting, registry-hive
  deletion, shutdown, `rm -rf /`). This is a guard rail, not a sandbox — review what you ask it to do.

---

## Configuration

- **Windows:** `%APPDATA%\gemma-cli\config.yaml`
- **Linux/macOS:** `~/.config/gemma-cli/config.yaml`

```yaml
model: gemma4:12b
num_ctx: 32768              # context window; raise if you have VRAM headroom
ollama_url: http://localhost:11434
searxng_url: http://localhost:8899
keep_alive: 30m
max_tool_iterations: 25
show_thinking: true
allow_model_skills: true    # let the model load skills itself; /<name> works either way
allowed_write_roots:        # the agent may only write under these paths
  - C:\Users\you
  - C:\Users\you\AppData\Local\Temp
```

Any setting can be overridden by an environment variable (`GEMMA_MODEL`, `GEMMA_NUM_CTX`,
`GEMMA_OLLAMA_URL`, `GEMMA_SEARXNG_URL`, …) or a CLI flag.

**Borrowing a stronger GPU:** point `ollama_url` at another machine's Ollama and a weak laptop
runs the big model over the network.

---

## Web search

Search is powered by a local [SearXNG](https://github.com/searxng/searxng) container — no API keys,
no quotas. The installer sets it up when Docker is available. Without it every other tool still
works; search just reports that it's unavailable. To add it later: install Docker Desktop and
re-run the installer.

---

## How it works

`gemma` calls Ollama's `/api/chat` with function-calling tool definitions. When the model requests a
tool, the CLI executes it locally, feeds the result back, and loops until the model produces a final
answer — all on your hardware.
