# LOCAL-Intelligence

A fully local, offline command-line AI agent. It runs a Gemma model on your own
machine via [Ollama](https://ollama.com) and can actually **do things** — read,
write, and delete files, run shell commands, search the web, and understand
images — with no cloud, no API keys, and no data leaving your computer.

Think of it as a local Claude/Codex CLI: you chat in the terminal, and it uses
tools to work on your real filesystem.

```
› what's taking up space in my Downloads folder?

🔧 shell  Get-ChildItem $HOME\Downloads | Sort Length -Desc | Select -First 5
   installer.exe  (2.1 GB)  (+4 lines)

The five largest items in your Downloads folder are… [summary]
```

## What it can do

| Tool | Purpose |
|------|---------|
| `read_file` / `write_file` | Read and create/overwrite files (writes fenced to allowed roots) |
| `shell` | Run **PowerShell** (Windows) or **bash** (Linux/macOS) commands |
| `glob` / `grep` / `list_directory` | Find files, search contents, browse folders |
| `web_search` / `web_fetch` | Search the web via a local SearXNG instance and read pages |
| vision | Attach an image and ask about it (Gemma is multimodal) |

## Requirements

- **Windows 10/11** or Linux/macOS
- ~10 GB free disk for the model (`gemma4:12b`)
- A GPU helps a lot (an 8 GB card runs `gemma4:12b` well); CPU-only works but is slower
- The installer handles Ollama, Python deps, and (optionally) SearXNG for you

## Install

### Windows

```powershell
git clone https://github.com/jaysettle/LOCAL-Intelligence
cd LOCAL-Intelligence
powershell -ExecutionPolicy Bypass -File install.ps1
```

No git? Download the repo ZIP from GitHub (**Code ▸ Download ZIP**), extract it,
then run the same `install.ps1` line from inside the folder.

### Linux / macOS

```bash
git clone https://github.com/jaysettle/LOCAL-Intelligence
cd LOCAL-Intelligence
./install.sh
```

The installer will: install Ollama if missing → pull `gemma4:12b` → install the
`gemma` CLI → write a default config → set up a local SearXNG search container
(if Docker is present) → run a smoke test.

Installer flags: `-Model gemma4:e4b`, `-SkipModel`, `-SkipSearch` (PowerShell) /
`--model`, `--skip-model`, `--skip-search` (bash).

## Use

```bash
gemma                                  # interactive chat
gemma -p "summarize ~/notes/todo.md"   # one-shot, prints and exits
gemma -i photo.jpg -p "what is this?"  # attach an image
gemma --model gemma4:e4b               # use the smaller/faster edge model
gemma --no-thinking                    # hide the model's reasoning
gemma --verbose                        # show full tool output
```

REPL commands: `/clear` (reset), `/model <tag>` (switch), `/image <path> <prompt>`,
`/help`, `/exit`.

## Configuration

A config file is created on first install:

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
allowed_write_roots:       # the agent may only write under these paths
  - C:\Users\you
  - C:\Users\you\AppData\Local\Temp
```

Any setting can be overridden by an environment variable (`GEMMA_MODEL`,
`GEMMA_NUM_CTX`, `GEMMA_OLLAMA_URL`, `GEMMA_SEARXNG_URL`, …) or a CLI flag.

## Web search

Search is powered by a local [SearXNG](https://github.com/searxng/searxng)
container (no API keys, no quotas). The installer sets it up automatically if
Docker is available. Without it, every other tool still works — search just
reports that it's unavailable. To add it later: install Docker Desktop and re-run
the installer.

## Safety

The agent runs with **your** user privileges — that's the point; it's your
machine. Guard rails:

- **Writes** are restricted to `allowed_write_roots` (your home + temp by default).
- The **shell** tool blocks obviously destructive commands (drive formatting,
  registry-hive deletion, shutdown, `rm -rf /`, etc.). This is a guard rail, not
  a sandbox — review what you ask it to do.

## How it works

`gemma` calls Ollama's `/api/chat` with function-calling tool definitions. When
the model requests a tool, the CLI executes it locally, feeds the result back,
and loops until the model produces a final answer — all on your hardware.

## License

MIT
