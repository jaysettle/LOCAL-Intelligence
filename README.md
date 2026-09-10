# LOCAL-Intelligence

A CLI AI agent that runs **entirely on your machine**. No cloud, no API keys, no data leaving your computer.

It reads and writes your files, runs shell commands, reads PDFs and Office documents, searches the web, and looks at images.

---

## Before you start

- **Windows 10/11**, Linux, or macOS
- **~10 GB** free disk (for the model)
- A GPU helps. CPU-only works, just slower.

The installer handles everything else.

---

## Install

### Windows

```powershell
git clone https://github.com/jaysettle/LOCAL-Intelligence
cd LOCAL-Intelligence
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Linux / macOS

```bash
git clone https://github.com/jaysettle/LOCAL-Intelligence
cd LOCAL-Intelligence
./install.sh
```

**No git?** Download the ZIP (**Code ▸ Download ZIP**), extract, then run the same install line inside the folder.

Takes 10-20 min on first run — most of it is the model download.

---

## Start it

```powershell
cd C:\any\project\folder
gemma go
```

That's it. It works in whatever folder you launch it from.

---

## Update

From any folder, any terminal:

```
gemma update
```

That's the whole thing. It pulls the latest and reinstalls.

```
gemma update --check    # is there an update? install nothing
gemma update --full     # also re-check Ollama, the model and web search
```

---

## Installer flags

| PowerShell | bash | Does |
|---|---|---|
| `-Model gemma4:e4b` | `--model gemma4:e4b` | Edge model — fits an 8 GB GPU fully (bigger download, 9 GB) |
| `-SkipModel` | `--skip-model` | Don't download the model |
| `-SkipSearch` | `--skip-search` | Don't set up web search |
| `-SkipUpdate` | `--skip-update` | Don't `git pull` first |

---

## If it breaks

**`gemma` not found** → open a new terminal.

**Install fails, mentions `gemma.exe`** → close every running `gemma` session, then re-run.

**"Can't reach Ollama"** → start Ollama (the app, or `ollama serve`).

**Web search unavailable** → needs Docker Desktop running. Everything else still works without it.

---

## Next

- **[How to use it →](docs/USAGE.md)** — commands, config, tools, safety
- Config file lives at `%APPDATA%\gemma-cli\config.yaml` (Windows) or `~/.config/gemma-cli/config.yaml`

MIT licensed.
