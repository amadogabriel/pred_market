param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$Launcher = Join-Path $ScriptDir "start_pm_system.ps1"

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "Missing launcher script: $Launcher"
}

$StartupDir = [Environment]::GetFolderPath("Startup")
if (-not $StartupDir) {
    throw "Could not resolve the current user's Windows Startup folder."
}

$ShortcutPath = Join-Path $StartupDir "pm-system.lnk"
if ((Test-Path -LiteralPath $ShortcutPath) -and -not $Force) {
    Write-Host "Startup shortcut already exists: $ShortcutPath"
    Write-Host "Use -Force to replace it."
    exit 0
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShell
$Shortcut.Arguments = $Arguments
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Start pm-system engine, monitor, and dashboard"
$Shortcut.IconLocation = "$PowerShell,0"
$Shortcut.WindowStyle = 7
$Shortcut.Save()

Write-Host "Installed Windows startup shortcut:"
Write-Host $ShortcutPath
Write-Host "Target: $PowerShell $Arguments"
