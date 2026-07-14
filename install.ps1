<#
.SYNOPSIS
  LOCAL-Intelligence installer for Windows.
  Installs Ollama + the Gemma model, the `gemma` CLI, a default config,
  and (optionally) a local SearXNG container for web search.

.USAGE
  powershell -ExecutionPolicy Bypass -File install.ps1
  Options: -Model gemma4:12b  -SkipModel  -SkipSearch
#>
[CmdletBinding()]
param(
    [string]$Model = "gemma4:12b",
    [switch]$SkipModel,
    [switch]$SkipSearch
)

$ErrorActionPreference = "Stop"
$RepoDir = $PSScriptRoot

# Minimum Ollama version that can pull gemma4.
$MinOllama = [version]"0.32.0"

function Info($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  OK $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  !! $m" -ForegroundColor Yellow }
function Have($n)  { return [bool](Get-Command $n -ErrorAction SilentlyContinue) }

function Get-OllamaVersion {
    try {
        $line = (ollama --version 2>&1 | Select-Object -First 1)
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
    $ollamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $ollamaExe) {
        $dir = Split-Path $ollamaExe
        if ($env:Path -notlike "*$dir*") { $env:Path = "$env:Path;$dir" }
    }
}

Write-Host ""
Write-Host "LOCAL-Intelligence installer" -ForegroundColor White
Write-Host "----------------------------" -ForegroundColor White

# 1. Ollama ---------------------------------------------------------------
Info "Checking Ollama (need >= $MinOllama for gemma4)"
$ver = Get-OllamaVersion
if ($ver -and $ver -ge $MinOllama) {
    Ok "ollama $ver present"
} elseif ($ver) {
    Warn "ollama $ver is too old for gemma4 - upgrading"
    Install-Ollama
    $ver = Get-OllamaVersion
    if ($ver -and $ver -ge $MinOllama) { Ok "ollama upgraded to $ver" } else { Warn "Upgrade may need a new terminal; re-run if the model pull fails." }
} else {
    Install-Ollama
    if (Have "ollama") { Ok "ollama installed ($(Get-OllamaVersion))" } else { Warn "ollama not on PATH yet; open a new terminal and re-run."; exit 1 }
}

# Make sure the Ollama server is reachable (the app/service usually auto-starts).
Info "Ensuring Ollama server is running"
$ollamaUp = $false
for ($i = 0; $i -lt 10; $i++) {
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; $ollamaUp = $true; break }
    catch { if ($i -eq 0) { Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2 }
}
if ($ollamaUp) { Ok "Ollama server responding" } else { Warn "Ollama server not responding yet; it may need a moment or a manual 'ollama serve'." }

# 2. Model ----------------------------------------------------------------
if ($SkipModel) {
    Warn "Skipping model pull (-SkipModel)"
} else {
    Info "Pulling model $Model (this is a multi-GB download the first time)"
    $have = $false
    try { $have = ((ollama list) -join "`n") -match [regex]::Escape($Model) } catch {}
    if ($have) { Ok "$Model already present" } else { ollama pull $Model; Ok "$Model ready" }
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

# 4. Install the CLI ------------------------------------------------------
Info "Installing the gemma CLI (pip install .)"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install "$RepoDir"
if (Have "gemma") {
    Ok "gemma is on PATH"
} else {
    $scripts = & $py -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    Warn "gemma installed but its folder isn't on PATH: $scripts"
    Warn "Add it to PATH (or open a new terminal) to use 'gemma' directly."
}

# 5. Default config -------------------------------------------------------
Info "Writing default config"
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
    # Enable JSON output format (idempotent-ish: append once).
    $settings = (docker exec searxng cat /etc/searxng/settings.yml 2>$null) | Out-String
    if ($settings -and ($settings -notmatch "format")) {
        docker exec searxng sh -c 'printf "\nsearch:\n  formats:\n    - html\n    - json\n" >> /etc/searxng/settings.yml'
        docker restart searxng | Out-Null
        Start-Sleep -Seconds 6
    }
    try {
        Invoke-RestMethod -Uri "http://localhost:8899/search?q=test&format=json" -TimeoutSec 10 | Out-Null
        Ok "SearXNG responding on http://localhost:8899"
    } catch { Warn "SearXNG container started but not responding yet; give it a minute." }
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
Ok "Done. Start chatting with:  gemma"
Write-Host "     One-shot:  gemma -p `"list the files in my home folder`"" -ForegroundColor DarkGray
Write-Host "     Config:    $env:APPDATA\gemma-cli\config.yaml" -ForegroundColor DarkGray
