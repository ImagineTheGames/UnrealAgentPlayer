[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ProjectRoot,
    [string] $Python,
    [string] $VenvBase,
    [switch] $Force,
    [switch] $AppendAgents
)
$ErrorActionPreference = 'Stop'
$kit = Join-Path $PSScriptRoot 'agent-testing'
if (-not $VenvBase) { $VenvBase = Join-Path $PSScriptRoot 'mcp-server' }

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $enc)
}

# 1. Validate project root.
if (-not (Test-Path $ProjectRoot)) { Write-Error "ProjectRoot not found: $ProjectRoot"; exit 2 }
if (-not (Get-ChildItem -Path $ProjectRoot -Filter *.uproject -File -ErrorAction SilentlyContinue)) {
    Write-Warning "No .uproject under $ProjectRoot -- continuing anyway."
}

# 2. Resolve python. Explicit -Python is trusted verbatim (baked for the run machine).
#    Otherwise resolve the default venv under -VenvBase and require it to exist.
if ($Python) {
    $py = $Python
} else {
    $py = Join-Path $VenvBase '.venv/Scripts/python.exe'
    if (-not (Test-Path $py)) {
        Write-Error "venv python not found at '$py'. Build it: cd mcp-server; python -m venv .venv; .venv\Scripts\pip install -e '.[dev]'  (or pass -Python <path>)."
        exit 4
    }
}

# 3. Command file.
$cmdDest = Join-Path $ProjectRoot '.claude/commands/agentplayertest.md'
if ((Test-Path $cmdDest) -and -not $Force) {
    Write-Host "[skip] $cmdDest exists (use -Force to overwrite)"
} else {
    Write-Utf8NoBom $cmdDest (Get-Content -Raw (Join-Path $kit 'agentplayertest.md'))
    Write-Host "[write] $cmdDest"
}

# 4. Launcher (token substitution). Bake this project's identity so the launcher pins
#    UAP_PROJECT/UAP_UPROJECT -> commands target THIS editor, never another open one.
$uproj = Get-ChildItem -Path $ProjectRoot -Filter *.uproject -File -ErrorAction SilentlyContinue | Select-Object -First 1
$projName = if ($uproj) { $uproj.BaseName } else { Split-Path $ProjectRoot -Leaf }
$uprojPath = if ($uproj) { $uproj.FullName } else { '' }
$launchDest = Join-Path $ProjectRoot 'uap.ps1'
if ((Test-Path $launchDest) -and -not $Force) {
    Write-Host "[skip] $launchDest exists (use -Force to overwrite)"
} else {
    $tpl = Get-Content -Raw (Join-Path $kit 'uap.ps1.template')
    $tpl = $tpl.Replace('__UAP_PYTHON__', $py).Replace('__UAP_PROJECT__', $projName).Replace('__UAP_UPROJECT__', $uprojPath)
    Write-Utf8NoBom $launchDest $tpl
    Write-Host "[write] $launchDest (python = $py; project = $projName)"
}

# 5. AGENTS rule.
$snippet = Get-Content -Raw (Join-Path $kit 'AGENTS-snippet.md')
$marker = '## Verifying runtime game behavior'
if ($AppendAgents) {
    $agents = Join-Path $ProjectRoot 'AGENTS.md'
    $existing = if (Test-Path $agents) { Get-Content -Raw $agents } else { '' }
    if ($existing -match [regex]::Escape($marker)) {
        Write-Host "[skip] AGENTS.md already has the rule"
    } else {
        $sep = if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { "`n`n" } else { "`n" }
        Write-Utf8NoBom $agents ($existing + $sep + $snippet)
        Write-Host "[append] $agents"
    }
} else {
    Write-Host ""
    Write-Host "--- Paste this into your project's AGENTS.md (or re-run with -AppendAgents) ---"
    Write-Host $snippet
    Write-Host "--- end ---"
}

# 6. Next steps.
Write-Host ""
Write-Host "Next: enable UE plugins (Remote Control API, Python Editor Script Plugin, Unreal Agent Player),"
Write-Host "build the editor, then verify:  powershell -NoProfile -File uap.ps1 status"
exit 0
