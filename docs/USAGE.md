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
gemma --model gemma4:e4b                   # smaller/faster edge model
gemma --no-thinking                        # hide the model's reasoning
gemma --verbose                            # show full tool output
gemma go --resume                          # pick up the last session
gemma go --approve writes                  # y/N prompt before any change
```

### REPL commands

`/paste` · `/image <path> <prompt>` · `/clear` · `/model <tag>` · `/save` · `/resume [name]` ·
`/sessions` · `/help` · `/exit`

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

## Memory, sessions & safety

- **Memory** — a `GEMMA.md` in your project folder is auto-loaded into the agent's context each
  run (like a project README for the AI); it can add to it with the `remember` tool. A global
  memory file holds cross-project facts. **Both are plain markdown — edit them yourself.**
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
