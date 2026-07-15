#!/usr/bin/env bash
# LOCAL-Intelligence installer for Linux/macOS.
# Installs Ollama + the Gemma model, the `gemma` CLI, a default config,
# and (optionally) a local SearXNG container for web search.
#
# Usage: ./install.sh [--model gemma4:12b] [--skip-model] [--skip-search] [--skip-update]
# Safe to re-run: self-updates from git, skips large downloads already present.
set -euo pipefail

MODEL="gemma4:12b"
SKIP_MODEL=0
SKIP_SEARCH=0
SKIP_UPDATE=0
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --skip-model) SKIP_MODEL=1; shift ;;
    --skip-search) SKIP_SEARCH=1; shift ;;
    --skip-update) SKIP_UPDATE=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

info() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  OK %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  !! %s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo; echo "LOCAL-Intelligence installer"; echo "----------------------------"

# 0. Self-update from the repo's branch, re-exec if the script itself changed.
if [ "$SKIP_UPDATE" -eq 0 ] && have git && [ -d "$REPO_DIR/.git" ]; then
  info "Updating code from git ($(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null))"
  before="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo x)"
  git -C "$REPO_DIR" pull --ff-only || warn "git pull failed (continuing with local code)"
  after="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo y)"
  if [ "$before" != "$after" ]; then
    info "Code updated - re-running the new installer"
    exec bash "$REPO_DIR/install.sh" --skip-update --model "$MODEL" \
      $([ "$SKIP_MODEL" -eq 1 ] && echo --skip-model) \
      $([ "$SKIP_SEARCH" -eq 1 ] && echo --skip-search)
  fi
fi

# 1. Ollama  (need >= 0.32 for gemma4)
MIN_OLLAMA="0.32.0"
ollama_ver() { ollama --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; }
info "Checking Ollama (need >= $MIN_OLLAMA for gemma4)"
if have ollama; then
  cur="$(ollama_ver || true)"
  if [ -n "$cur" ] && [ "$(printf '%s\n%s\n' "$MIN_OLLAMA" "$cur" | sort -V | head -1)" = "$MIN_OLLAMA" ]; then
    ok "ollama $cur present"
  else
    warn "ollama $cur too old for gemma4 - reinstalling latest"
    curl -fsSL https://ollama.com/install.sh | sh
    ok "ollama now $(ollama_ver)"
  fi
else
  info "Installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh
  have ollama && ok "ollama installed ($(ollama_ver))" || { warn "ollama not on PATH; re-run after opening a new shell."; exit 1; }
fi

info "Ensuring Ollama server is running"
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve >/dev/null 2>&1 &
  sleep 3
fi
curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 && ok "Ollama server responding" || warn "Ollama server not responding yet."

# 2. Model
if [ "$SKIP_MODEL" -eq 1 ]; then
  warn "Skipping model pull (--skip-model)"
else
  info "Pulling model $MODEL (multi-GB on first run)"
  if ollama list 2>/dev/null | grep -q "$MODEL"; then ok "$MODEL already present"; else ollama pull "$MODEL"; ok "$MODEL ready"; fi
fi

# 3. Python
info "Checking Python 3.10+"
PY=""
for cand in python3 python; do
  if have "$cand"; then
    v="$("$cand" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    if [ -n "$v" ] && [ "$(printf '%s\n3.10\n' "$v" | sort -V | head -1)" = "3.10" ]; then PY="$cand"; break; fi
  fi
done
[ -n "$PY" ] || { warn "Python 3.10+ required. Install it and re-run."; exit 1; }
ok "Using Python: $PY"

# 4. Install CLI
info "Installing the gemma CLI (pip install .)"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install "$REPO_DIR"
have gemma && ok "gemma is on PATH" || warn "gemma installed; ensure your pip scripts dir is on PATH."

# 5. Default config
info "Writing default config"
"$PY" -m gemma_cli.main --setup-config

# 6. Web search (optional)
if [ "$SKIP_SEARCH" -eq 1 ]; then
  warn "Skipping SearXNG setup (--skip-search)."
elif have docker; then
  info "Setting up local SearXNG container"
  if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^searxng$'; then
    docker run -d --name searxng -p 8899:8080 -v searxng-data:/etc/searxng --restart unless-stopped searxng/searxng >/dev/null
    sleep 8
  fi
  # Write a known-good settings.yml (JSON API on, limiter off). Newer images ship
  # a settings.yml already containing "format", so a naive append is skipped and
  # the JSON API returns 403; overwriting is reliable across image versions.
  printf 'use_default_settings: true\nserver:\n  secret_key: "localintelligence-searxng"\n  limiter: false\n  image_proxy: true\nsearch:\n  formats:\n    - html\n    - json\n' \
    | docker exec -i searxng sh -c 'cat > /etc/searxng/settings.yml' 2>/dev/null || true
  docker restart searxng >/dev/null; sleep 6
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8899/search?q=test&format=json" 2>/dev/null || echo 0)"
  [ "$code" = "200" ] && ok "SearXNG responding (JSON API) on :8899" || warn "SearXNG not ready yet (HTTP $code); retry in a minute."
else
  warn "Docker not found - skipping web search. Install Docker and re-run to enable it."
fi

# 7. Smoke test
info "Smoke test"
gemma -p "Reply with exactly: LOCAL-Intelligence ready." 2>/dev/null || "$PY" -m gemma_cli.main -p "Reply with exactly: LOCAL-Intelligence ready." || warn "Smoke test could not run."

echo; ok "Done. Start chatting with:  gemma"
