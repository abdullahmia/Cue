$ErrorActionPreference = "Stop"

$CueDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PsAdapter = Join-Path $CueDir "powershell\cue.ps1"

Write-Host ""
Write-Host "✦ Installing cue for Windows PowerShell..." -ForegroundColor Cyan
Write-Host "  Plugin directory: $CueDir"

$hasPy = (Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command python3 -ErrorAction SilentlyContinue) -or (Get-Command py -ErrorAction SilentlyContinue)
if (-not $hasPy) {
    Write-Warning "Python 3 was not found in PATH. Install Python 3 before using cue."
}

$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}
if (-not (Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
}

$sourceLine = ". `"$PsAdapter`""
$existing = Get-Content -Path $PROFILE -Raw -ErrorAction SilentlyContinue
if ($existing -and $existing.Contains("powershell\cue.ps1")) {
    Write-Host "  ✓ Already configured in $PROFILE" -ForegroundColor Green
} else {
    Add-Content -Path $PROFILE -Value "`n# cue - Smart AI PowerShell autocomplete & error-fix assistant`n$sourceLine"
    Write-Host "  ✓ Added cue loader to $PROFILE" -ForegroundColor Green
}

Write-Host ""
Write-Host "✓ Installation complete! Open a new PowerShell window or run: . `"$PROFILE`"" -ForegroundColor Green
Write-Host ""
