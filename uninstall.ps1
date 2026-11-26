param(
    [switch]$Purge,
    [switch]$KeepData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$CueDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue) ?? (Get-Command python3 -ErrorAction SilentlyContinue) ?? (Get-Command py -ErrorAction SilentlyContinue)
if (-not $py) {
    Write-Error "Python 3 not found."
    exit 1
}

$argsList = @("-m", "engine.cli", "uninstall")
if ($Purge) { $argsList += "--purge" }
if ($KeepData) { $argsList += "--keep-data" }
if ($Yes) { $argsList += "--yes" }

$prev = $env:PYTHONPATH
$env:PYTHONPATH = if ($prev) { "$CueDir$([IO.Path]::PathSeparator)$prev" } else { $CueDir }
try {
    if ($py.Name -eq "py.exe" -or $py.Name -eq "py") {
        & py -3 @argsList
    } else {
        & $py @argsList
    }
} finally {
    $env:PYTHONPATH = $prev
}
