<#
.SYNOPSIS
  LOCAL-Intelligence installer / updater for Windows.
  Installs Ollama + the Gemma model, the `gemma` CLI, a default config,
  and (optionally) a local SearXNG container for web search.

  Safe to re-run: it self-updates from the repo's branch (git pull), then
  reinstalls the CLI. Large downloads (Ollama, the model) are skipped when
  already present, so re-running is cheap and acts as an updater.

.USAGE
  powershell -ExecutionPolicy Bypass -File install.ps1
  Options: -Model gemma4:12b  -SkipModel  -SkipSearch  -SkipUpdate
#>
[CmdletBinding()]
param(
    [string]$Model = "gemma4:12b",
    [switch]$SkipModel,
    [switch]$SkipSearch,
    [switch]$SkipUpdate
)

$ErrorActionPreference = "Stop"
$RepoDir = $PSScriptRoot

# Minimum Ollama version that can pull gemma4.
$MinOllama = [version]"0.32.0"

function Info($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  OK $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  !! $m" -ForegroundColor Yellow }
function Have($n)  { return [bool](Get-Command $n -ErrorAction SilentlyContinue) }

function Find-OllamaExe {
    # Prefer PATH; fall back to the known install location so a terminal opened
    # BEFORE Ollama was installed still finds it (and we never re-download).
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Get-OllamaVersion($exe) {
    if (-not $exe) { return $null }
    try {
        $line = (& $exe --version 2>&1 | Select-Object -First 1)
        if ($line -match "(\d+\.\d+\.\d+)") { return [version]$Matches[1] }
    } catch {}
    return $null
}

function Install-Ollama {
    # winget's Ollama package lags behind (was 0.31.2 when gemma4 needed 0.32),
    # so pull the latest installer straight from ollama.com.
    $dl = Join-Path $env:TEMP "OllamaSetup.exe"
    Info "Downloading latest Ollama from ollama.com"
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $dl -UseBasicParsing
    Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Info "Running Ollama installer (silent)"
    Start-Process -FilePath $dl -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "LOCAL-Intelligence installer" -ForegroundColor White
Write-Host "----------------------------" -ForegroundColor White

# 0. Self-update from the repo's branch -----------------------------------
if (-not $SkipUpdate) {
    if ((Have "git") -and (Test-Path (Join-Path $RepoDir ".git"))) {
        $branch = (git -C $RepoDir rev-parse --abbrev-ref HEAD 2>$null)
        Info "Updating code from git ($branch)"
        $scriptFile = Join-Path $RepoDir "install.ps1"
        $before = (Get-FileHash $scriptFile -ErrorAction SilentlyContinue).Hash
        try {
            git -C $RepoDir pull --ff-only 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
        } catch { Warn "git pull failed (continuing with local code)" }
        $after = (Get-FileHash $scriptFile -ErrorAction SilentlyContinue).Hash
        if ($before -and $after -and ($before -ne $after)) {
            Info "Installer itself changed - re-running the updated version"
            $fwd = @{ SkipUpdate = $true; Model = $Model }
            if ($SkipModel) { $fwd.SkipModel = $true }
            if ($SkipSearch) { $fwd.SkipSearch = $true }
            & $scriptFile @fwd
            exit $LASTEXITCODE
        }
    } else {
        Info "Not a git checkout (or git missing) - skipping self-update"
    }
}

# 1. Ollama ---------------------------------------------------------------
Info "Checking Ollama (need >= $MinOllama for gemma4)"
$OllamaExe = Find-OllamaExe
$ver = Get-OllamaVersion $OllamaExe
if ($ver -and $ver -ge $MinOllama) {
    Ok "ollama $ver present"
} elseif ($ver) {
    Warn "ollama $ver is too old for gemma4 - upgrading"
    Install-Ollama
    $OllamaExe = Find-OllamaExe
    $ver = Get-OllamaVersion $OllamaExe
    if ($ver -and $ver -ge $MinOllama) { Ok "ollama upgraded to $ver" } else { Warn "Upgrade may need a new terminal; re-run if the model pull fails." }
} else {
    Install-Ollama
    $OllamaExe = Find-OllamaExe
    if ($OllamaExe) { Ok "ollama installed ($(Get-OllamaVersion $OllamaExe))" } else { Warn "ollama not found after install; open a new terminal and re-run."; exit 1 }
}
# Ensure Ollama's folder is on PATH for this session (gemma calls ollama).
if ($OllamaExe) {
    $odir = Split-Path $OllamaExe
    if ($env:Path -notlike "*$odir*") { $env:Path = "$env:Path;$odir" }
}

# Make sure the Ollama server is reachable (the app/service usually auto-starts).
Info "Ensuring Ollama server is running"
$ollamaUp = $false
for ($i = 0; $i -lt 10; $i++) {
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; $ollamaUp = $true; break }
    catch { if ($i -eq 0 -and $OllamaExe) { Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2 }
}
if ($ollamaUp) { Ok "Ollama server responding" } else { Warn "Ollama server not responding yet; it may need a moment or a manual 'ollama serve'." }

# 2. Model ----------------------------------------------------------------
if ($SkipModel) {
    Warn "Skipping model pull (-SkipModel)"
} else {
    Info "Pulling model $Model (multi-GB the first time; skipped if already present)"
    $have = $false
    try { $have = ((& $OllamaExe list) -join "`n") -match [regex]::Escape($Model) } catch {}
    if ($have) { Ok "$Model already present" } else { & $OllamaExe pull $Model; Ok "$Model ready" }
}

# 3. Python ---------------------------------------------------------------
Info "Checking Python 3.10+"
$py = $null
foreach ($cand in @("python", "py")) {
    if (Have $cand) {
        $vraw = (& $cand --version 2>&1) | Out-String
        if ($vraw -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 10)) { $py = $cand; break }
        }
    }
}
if (-not $py) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        $wa = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
        if (Test-Path $wa) { $winget = $wa }
    }
    if ($winget) {
        Info "Installing Python 3.12 via winget"
        & $winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        $py = "python"
    } else {
        Warn "Python 3.10+ not found and winget unavailable. Install from https://python.org then re-run."
        exit 1
    }
}
Ok "Using Python: $py"

# 4. Install / upgrade the CLI --------------------------------------------
Info "Installing the gemma CLI (pip install --upgrade .)"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install --upgrade "$RepoDir"
if (Have "gemma") {
    Ok "gemma is on PATH ($(& gemma --version 2>&1))"
} else {
    $scripts = & $py -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    Warn "gemma installed but its folder isn't on PATH: $scripts"
    Warn "Add it to PATH (or open a new terminal) to use 'gemma' directly."
}

# 5. Default config -------------------------------------------------------
Info "Writing default config (kept if it already exists)"
& $py -m gemma_cli.main --setup-config

# 6. Web search (optional) ------------------------------------------------
if ($SkipSearch) {
    Warn "Skipping SearXNG setup (-SkipSearch). Web search will be unavailable until configured."
} elseif (Have "docker") {
    Info "Setting up local SearXNG container for web search"
    $exists = (docker ps -a --format "{{.Names}}" 2>$null) -match "^searxng$"
    if (-not $exists) {
        docker run -d --name searxng -p 8899:8080 -v searxng-data:/etc/searxng --restart unless-stopped searxng/searxng | Out-Null
        Start-Sleep -Seconds 8
    }
    # Write a known-good settings.yml: enable the JSON API and disable the limiter.
    # Newer SearXNG images ship a settings.yml that already contains the word
    # "format", so a naive append is skipped and the JSON API returns 403.
    # Overwriting with a complete file is reliable across image versions.
    $settings = @'
use_default_settings: true
server:
  secret_key: "localintelligence-searxng"
  limiter: false
  image_proxy: true
search:
  formats:
    - html
    - json
'@
    $settings | docker exec -i searxng sh -c 'cat > /etc/searxng/settings.yml' 2>$null
    docker restart searxng | Out-Null
    Start-Sleep -Seconds 6
    $code = 0
    try { $code = (Invoke-WebRequest -Uri "http://localhost:8899/search?q=test&format=json" -TimeoutSec 10 -UseBasicParsing).StatusCode } catch {}
    if ($code -eq 200) { Ok "SearXNG responding (JSON API enabled) on http://localhost:8899" }
    else { Warn "SearXNG started but the JSON API isn't ready yet (HTTP $code); give it a minute and retry." }
} else {
    Warn "Docker not found - skipping web search setup."
    Warn "To enable search later: install Docker Desktop (winget install Docker.DockerDesktop), then re-run this script."
    Warn "File, shell and vision tools work fine without it."
}

# 7. Smoke test -----------------------------------------------------------
Info "Smoke test"
try {
    if (Have "gemma") { gemma -p "Reply with exactly: LOCAL-Intelligence ready." }
    else { & $py -m gemma_cli.main -p "Reply with exactly: LOCAL-Intelligence ready." }
} catch { Warn "Smoke test could not run automatically: $_" }

Write-Host ""
Ok "Done. Start chatting with:  gemma go"
Write-Host "     One-shot:  gemma `"list the files in my home folder`"" -ForegroundColor DarkGray
Write-Host "     Update:    re-run this script anytime to pull the latest and reinstall" -ForegroundColor DarkGray
Write-Host "     Config:    $env:APPDATA\gemma-cli\config.yaml" -ForegroundColor DarkGray
