param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogsDir = Join-Path $ProjectRoot "logs"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python.exe" }

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Get-AppProcess {
    param([Parameter(Mandatory=$true)][string]$ScriptPath)

    $escapedPath = [regex]::Escape($ScriptPath)
    $scriptName = [IO.Path]::GetFileName($ScriptPath)
    $escapedName = [regex]::Escape($scriptName)
    $escapedRoot = [regex]::Escape($ProjectRoot)

    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and (
            $_.CommandLine -match $escapedPath -or
            ($_.CommandLine -match $escapedName -and $_.CommandLine -match $escapedRoot)
        )
    }
}

function Stop-AppProcess {
    param([Parameter(Mandatory=$true)][string]$ScriptPath)

    $procs = @(Get-AppProcess -ScriptPath $ScriptPath)
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "stopped $([IO.Path]::GetFileName($ScriptPath)) pid=$($proc.ProcessId)"
        } catch {
            Write-Warning "failed to stop pid=$($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Start-AppProcess {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$ScriptPath
    )

    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        throw "Missing app entrypoint: $ScriptPath"
    }

    if ($Restart) {
        Stop-AppProcess -ScriptPath $ScriptPath
        Start-Sleep -Milliseconds 300
    }

    $running = @(Get-AppProcess -ScriptPath $ScriptPath)
    if ($running.Count -gt 0) {
        $pids = ($running | ForEach-Object { $_.ProcessId }) -join ","
        Write-Host "$Name already running pid=$pids"
        return
    }

    $stdout = Join-Path $LogsDir "$Name.out.log"
    $stderr = Join-Path $LogsDir "$Name.err.log"
    $quotedScriptPath = "`"$ScriptPath`""
    Start-Process `
        -FilePath $Python `
        -ArgumentList $quotedScriptPath `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden | Out-Null
    Write-Host "started $Name"
}

$apps = @(
    @{ Name = "engine"; Script = Join-Path $ProjectRoot "engine.py" },
    @{ Name = "monitor"; Script = Join-Path $ProjectRoot "monitor.py" },
    @{ Name = "dashboard"; Script = Join-Path $ProjectRoot "dashboard.py" }
)

foreach ($app in $apps) {
    Start-AppProcess -Name $app.Name -ScriptPath $app.Script
}

Write-Host "dashboard: http://127.0.0.1:8787/"
