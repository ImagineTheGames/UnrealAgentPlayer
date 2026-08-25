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

# Resolve a project's engine editor exe from its .uproject EngineAssociation, so a launcher
# targets the project's OWN engine (a custom fork vs stock UE differ). Returns '' if unresolved.
function Resolve-EditorExe([string]$UprojPath) {
    if (-not $UprojPath -or -not (Test-Path $UprojPath)) { return '' }
    try { $assoc = (Get-Content -Raw $UprojPath | ConvertFrom-Json).EngineAssociation } catch { return '' }
    if (-not $assoc) { return '' }
    $root = ''
    # Resolution order mirrors Epic's own (FDesktopPlatformWindows::EnumerateEngineInstallations):
    # UE_ROOT override -> Epic Games Launcher manifest -> HKCU Builds. We deliberately do NOT
    # read HKLM\SOFTWARE\EpicGames\Unreal Engine\<ver>\InstalledDirectory: the engine never
    # reads it, newer launchers no longer write it, and keys left behind by an uninstall point
    # at nothing -- resolving by a rule the engine does not share is how a script and the editor
    # end up disagreeing about which engine is "the" engine.
    if ($env:UE_ROOT -and (Test-Path (Join-Path $env:UE_ROOT 'Engine\Build\BatchFiles'))) {
        $root = $env:UE_ROOT
    }
    if (-not $root -and $assoc -match '^[0-9]+\.[0-9]+$') {
        # Epic Games Launcher installs, any drive. Engine rows are the ones whose AppName is
        # "UE_<assoc>"; sibling rows (QuixelBridge_5.7, FabPlugin_5.7) share an InstallLocation,
        # which is why this keys off AppName and never off the path.
        $manifest = Join-Path $env:ProgramData 'Epic\UnrealEngineLauncher\LauncherInstalled.dat'
        if (Test-Path $manifest) {
            try {
                $entry = (Get-Content -Raw $manifest | ConvertFrom-Json).InstallationList |
                         Where-Object { $_.AppName -ceq "UE_$assoc" } | Select-Object -First 1
                if ($entry) { $root = $entry.InstallLocation }
            } catch { }
        }
    }
    if (-not $root -and $assoc -match '^\{?[0-9A-Fa-f-]{36}\}?$') {
        # Source / per-user builds, GUID keyed.
        try {
            $builds = Get-ItemProperty 'HKCU:\SOFTWARE\Epic Games\Unreal Engine\Builds' -ErrorAction Stop
            foreach ($p in $builds.PSObject.Properties) {
                if ($p.Name -match '^PS') { continue }
                if ($p.Name.Trim('{','}') -ine $assoc.Trim('{','}')) { continue }
                $root = $p.Value; break
            }
        } catch { }
    }
    if (-not $root -and (Test-Path $assoc)) {
        $root = $assoc                                  # explicit absolute path
    } else {
        $cand = Join-Path (Split-Path $UprojPath -Parent) $assoc
        if (Test-Path $cand) { $root = (Resolve-Path $cand).Path }   # relative to the project
    }
    if (-not $root) { return '' }
    $exe = Join-Path $root 'Engine\Binaries\Win64\UnrealEditor.exe'
    if (Test-Path $exe) { return $exe } else { return '' }
}

# 1. Validate project root.
if (-not (Test-Path $ProjectRoot)) { Write-Error "ProjectRoot not found: $ProjectRoot"; exit 2 }
if (-not (Get-ChildItem -Path $ProjectRoot -Filter *.uproject -File -ErrorAction SilentlyContinue)) {
    Write-Warning "No .uproject under $ProjectRoot -- continuing anyway."
}

# 2. Sanity-check the venv on THIS machine. The generated launcher no longer bakes this
#    path -- it locates the repo and its venv at run time -- so -Python / -VenvBase are now
#    only an install-time warning, never a hard gate. An explicit -Python stays trusted
#    verbatim (callers pass paths for machines other than this one).
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

# 4. Launcher (token substitution). Only two things are baked, and NEITHER is a machine
#    path, so the generated uap.ps1 is safe to commit and works for every teammate:
#      * the project NAME, so the launcher pins UAP_PROJECT and commands target THIS
#        editor rather than another open one;
#      * a RELATIVE path from the project to this repo, which survives the whole tree
#        moving to another drive. At run time the launcher also honours $env:UAP_HOME and
#        falls back to a sibling checkout, so a teammate who clones elsewhere still works.
#    The engine exe is resolved at RUN time from the .uproject EngineAssociation, because
#    teammates keep their engines in different places (source build vs launcher install).
$uproj = Get-ChildItem -Path $ProjectRoot -Filter *.uproject -File -ErrorAction SilentlyContinue | Select-Object -First 1
$projName = if ($uproj) { $uproj.BaseName } else { Split-Path $ProjectRoot -Leaf }

# Relative path project -> this repo. Empty if they are on different drives (no relative
# path exists); the launcher then relies on UAP_HOME / sibling lookup.
$relHome = ''
try {
    $fromUri = New-Object System.Uri (( [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') ) + '\')
    $toUri   = New-Object System.Uri (( [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') ) + '\')
    if ($fromUri.Scheme -eq $toUri.Scheme) {
        $relHome = [Uri]::UnescapeDataString($fromUri.MakeRelativeUri($toUri).ToString()).Replace('/', '\').TrimEnd('\')
        if ($relHome -match '^[A-Za-z]:') { $relHome = '' }   # different drive -> not relative
    }
} catch { $relHome = '' }

$launchDest = Join-Path $ProjectRoot 'uap.ps1'
if ((Test-Path $launchDest) -and -not $Force) {
    Write-Host "[skip] $launchDest exists (use -Force to overwrite)"
} else {
    $tpl = Get-Content -Raw (Join-Path $kit 'uap.ps1.template')
    $tpl = $tpl.Replace('__UAP_PROJECT__', $projName).Replace('__UAP_HOME_RELATIVE__', $relHome)
    Write-Utf8NoBom $launchDest $tpl
    $homeNote = if ($relHome) { "repo = .\$relHome (relative)" } else { "repo = via UAP_HOME / sibling lookup" }
    Write-Host "[write] $launchDest (project = $projName; $homeNote)"
    Write-Host "        This launcher has NO machine-local paths -- commit it to source control."
    $uprojPath = if ($uproj) { $uproj.FullName } else { '' }
    $engineExe = Resolve-EditorExe $uprojPath
    if ($engineExe) {
        Write-Host "        Engine resolves on this machine to: $engineExe"
    } else {
        Write-Warning "Could not resolve this project's engine from its EngineAssociation on this machine. `uap status` still works; only game_launch needs the exe."
    }
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
