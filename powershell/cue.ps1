# powershell/cue.ps1 — Windows PowerShell & pwsh adapter for cue

$script:CueVersion = "0.0.1"
$script:CuePluginDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $env:CUE_HOME) {
    $primaryCue = Join-Path $HOME ".cue"
    $fallbackCue = Join-Path $HOME ".cuee"
    $isManagedPrimary = (Test-Path $primaryCue -PathType Container) -and (
        (Test-Path (Join-Path $primaryCue ".cue_home")) -or
        (Test-Path (Join-Path $primaryCue "config.json"))
    )
    $isManagedFallback = (Test-Path $fallbackCue -PathType Container) -and (
        (Test-Path (Join-Path $fallbackCue ".cue_home")) -or
        (Test-Path (Join-Path $fallbackCue "config.json"))
    )
    if ($isManagedPrimary) {
        $env:CUE_HOME = $primaryCue
    } elseif ($isManagedFallback) {
        $env:CUE_HOME = $fallbackCue
    } elseif (Test-Path $primaryCue) {
        $env:CUE_HOME = $fallbackCue
    } else {
        $env:CUE_HOME = $primaryCue
    }
}

foreach ($sub in @("projects", "cache", "logs")) {
    $dir = Join-Path $env:CUE_HOME $sub
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
$markerFile = Join-Path $env:CUE_HOME ".cue_home"
if (-not (Test-Path $markerFile)) {
    Set-Content -Path $markerFile -Value "cue" -ErrorAction SilentlyContinue
}

function Get-CuePython {
    if ($env:CUE_PYTHON -and (Get-Command $env:CUE_PYTHON -ErrorAction SilentlyContinue)) {
        return $env:CUE_PYTHON
    }
    foreach ($cand in @("python", "python3", "py")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { return $cand }
    }
    return $null
}

function Invoke-CueCli {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs)
    $py = Get-CuePython
    if (-not $py) {
        Write-Warning "cue: Python 3 not found in PATH."
        return
    }
    $prevPyPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($prevPyPath) { "$script:CuePluginDir$([IO.Path]::PathSeparator)$prevPyPath" } else { $script:CuePluginDir }
    try {
        if ($py -eq "py") {
            & py -3 -m engine.cli @CliArgs
        } else {
            & $py -m engine.cli @CliArgs
        }
    } finally {
        $env:PYTHONPATH = $prevPyPath
    }
}

Set-Alias -Name cue -Value Invoke-CueCli -Scope Global

$script:CueLastDir = ""
$script:CueLastHistId = -1
$script:CueOriginalPrompt = $function:prompt

function Start-CueBackgroundAnalyze {
    param([string]$TargetDir)
    $py = Get-CuePython
    if (-not $py) { return }
    $argList = if ($py -eq "py") { "-3 -m engine.cli analyze --dir `"$TargetDir`" --quiet" } else { "-m engine.cli analyze --dir `"$TargetDir`" --quiet" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $py
    $psi.Arguments = $argList
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    $psi.EnvironmentVariables["PYTHONPATH"] = $script:CuePluginDir
    $psi.EnvironmentVariables["CUE_HOME"] = $env:CUE_HOME
    try { [System.Diagnostics.Process]::Start($psi) | Out-Null } catch {}
}

function Show-CueDropdown {
    $line = ""
    $cursor = 0
    [Microsoft.PowerShell.PSConsoleReadLine]::GetBufferState([ref]$line, [ref]$cursor)

    $jsonOut = Invoke-CueCli suggest --dir "$PWD" --input "$line" --limit 10 --json 2>$null | Out-String
    if (-not $jsonOut) { return }
    try {
        $parsed = $jsonOut | ConvertFrom-Json
    } catch {
        return
    }
    $items = @($parsed.suggestions)
    if ($items.Count -eq 0) { return }

    $idx = 0
    $maxShow = [Math]::Min($items.Count, 8)
    $items = $items[0..($maxShow - 1)]

    $draw = {
        param($selectedIdx)
        Write-Host ""
        Write-Host "╭─ cue · ↑/↓ select · Enter fill · Esc cancel ───────────────────────╮" -ForegroundColor DarkGray
        for ($i = 0; $i -lt $items.Count; $i++) {
            $it = $items[$i]
            $ptr = if ($i -eq $selectedIdx) { "❯" } else { " " }
            $badge = $it.source.ToUpper().PadRight(4).Substring(0, 4)
            $cmdText = $it.cmd
            if ($cmdText.Length -gt 34) { $cmdText = $cmdText.Substring(0, 33) + "…" }
            $rsnText = $it.reason
            if ($rsnText.Length -gt 24) { $rsnText = $rsnText.Substring(0, 23) + "…" }
            $row = " $ptr [$badge] $($cmdText.PadRight(34)) $($rsnText.PadRight(24)) "
            if ($i -eq $selectedIdx) {
                Write-Host $row -BackgroundColor DarkCyan -ForegroundColor White
            } else {
                Write-Host $row -ForegroundColor Gray
            }
        }
        Write-Host "╰────────────────────────────────────────────────────────────────────╯" -ForegroundColor DarkGray
    }

    & $draw $idx
    while ($true) {
        $key = [System.Console]::ReadKey($true)
        if ($key.Key -eq [System.ConsoleKey]::Enter) {
            [Microsoft.PowerShell.PSConsoleReadLine]::Replace(0, $line.Length, $items[$idx].cmd)
            break
        }
        if ($key.Key -eq [System.ConsoleKey]::Escape) {
            break
        }
        if ($key.Key -eq [System.ConsoleKey]::UpArrow) {
            $idx = ($idx - 1 + $items.Count) % $items.Count
            & $draw $idx
        } elseif ($key.Key -eq [System.ConsoleKey]::DownArrow) {
            $idx = ($idx + 1) % $items.Count
            & $draw $idx
        }
    }
    [Microsoft.PowerShell.PSConsoleReadLine]::InvokePrompt()
}

# Migrate legacy ~/.tawh config if ~/.cue/config.json doesn't exist yet
$cueCfg = Join-Path $env:CUE_HOME "config.json"
$legacyCfg = Join-Path (Join-Path $HOME ".tawh") "config.json"
if ((-not (Test-Path $cueCfg)) -and (Test-Path $legacyCfg)) {
    Invoke-CueCli status | Out-Null
}

# Onboarding check on first interactive session
if (($env:CUE_NO_ONBOARD -ne "1") -and (-not (Test-Path $cueCfg))) {
    Invoke-CueCli onboard
}

function global:prompt {
    $lastSuccess = $?
    $lastExit = $LASTEXITCODE
    $curDir = (Get-Location).Path

    if ($curDir -ne $script:CueLastDir) {
        $script:CueLastDir = $curDir
        Start-CueBackgroundAnalyze -TargetDir $curDir
    }

    $hist = Get-History -Count 1 -ErrorAction SilentlyContinue
    if ($hist -and ($hist.Id -ne $script:CueLastHistId)) {
        $script:CueLastHistId = $hist.Id
        $cmdLine = $hist.CommandLine.Trim()
        if ($cmdLine -and (-not $cmdLine.StartsWith("cue"))) {
            $code = if ($lastSuccess -and ($lastExit -eq 0 -or $null -eq $lastExit)) { 0 } elseif ($lastExit) { [int]$lastExit } else { 1 }
            Invoke-CueCli record --dir "$curDir" --exit-code $code --no-ai -- "$cmdLine" | Out-Null
            if ($code -ne 0) {
                Invoke-CueCli fix --dir "$curDir" --exit-code $code --no-ai "$cmdLine"
            }
        }
    }

    if ($script:CueOriginalPrompt) {
        & $script:CueOriginalPrompt
    } else {
        "PS $curDir> "
    }
}

if (Get-Module -ListAvailable -Name PSReadLine) {
    Set-PSReadLineKeyHandler -Chord "Ctrl+Spacebar" -ScriptBlock { Show-CueDropdown }
    Set-PSReadLineKeyHandler -Chord "F2" -ScriptBlock { Show-CueDropdown }
}
