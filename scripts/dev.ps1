<#
.SYNOPSIS
    Start the FastAPI backend (port 8080) and the visualizer (port 5173) together.

.DESCRIPTION
    Both run in this console with their output interleaved. Ctrl+C stops both.
    If something already listens on 8080 it is reused instead of starting a second backend.
    The visualizer folder is gitignored; when it is missing only the backend starts.

.PARAMETER Open
    Open the visualizer in the default browser once it is up.

.PARAMETER BackendOnly
    Start only the backend.

.PARAMETER Prod
    Build the visualizer and serve the optimised build (smaller, faster) instead of the dev
    server. Use it when you only want to look at the data, not change the visualizer's code.

.EXAMPLE
    .\scripts\dev.ps1 -Open

.EXAMPLE
    .\scripts\dev.ps1 -Prod -Open
#>
[CmdletBinding()]
param(
    [switch]$Open,
    [switch]$BackendOnly,
    [switch]$Prod
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$visualizer = Join-Path $root 'visualizer'
$started = @()
$logs = @()  # [IO.StreamReader]s whose new lines are echoed here

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Stop-Tree($Process) {
    if ($Process -and -not $Process.HasExited) {
        # uvicorn --reload and npm both spawn children; /T takes the whole tree down.
        & taskkill.exe /PID $Process.Id /T /F 2>&1 | Out-Null
    }
}

foreach ($tool in @('uv', 'npm')) {
    if ($tool -eq 'npm' -and ($BackendOnly -or -not (Test-Path $visualizer))) { continue }
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is not on PATH. Install it and try again."
    }
}

# Another project's virtualenv in the shell confuses uv (see CLAUDE.md).
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue

try {
    if (Test-Port 8080) {
        Write-Host 'Backend: something already listens on 8080, reusing it.' -ForegroundColor Yellow
        Write-Host '         If it runs old code, stop it and run this script again.' -ForegroundColor Yellow
    } else {
        Write-Host 'Backend: starting on http://127.0.0.1:8080 (docs at /docs)' -ForegroundColor Cyan
        # On Windows a uvicorn reload sends Ctrl+C to every process in its console, which stopped
        # the visualizer ("Terminate batch job") and with it this script. So the backend gets its
        # own hidden console, and its output is echoed here from log files.
        $logDir = Join-Path $root 'data'
        New-Item -ItemType Directory -Force $logDir | Out-Null
        $outLog = Join-Path $logDir 'backend.out.log'
        $errLog = Join-Path $logDir 'backend.log'
        $started += Start-Process -FilePath 'uv' -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog -ArgumentList @(
                'run', 'uvicorn', 'app.main:app', '--reload', '--reload-include', '.env',
                '--host', '127.0.0.1', '--port', '8080'
            )
        Start-Sleep -Milliseconds 300
        foreach ($f in @($outLog, $errLog)) {
            $stream = [IO.File]::Open($f, 'Open', 'Read', 'ReadWrite')
            $logs += New-Object IO.StreamReader($stream)
        }
    }

    if (-not $BackendOnly) {
        if (-not (Test-Path $visualizer)) {
            Write-Host 'Visualizer: folder not found (it is gitignored), starting the backend only.' -ForegroundColor Yellow
        } elseif (Test-Port 5173) {
            Write-Host 'Visualizer: something already listens on 5173, reusing it.' -ForegroundColor Yellow
        } else {
            if (-not (Test-Path (Join-Path $visualizer 'node_modules'))) {
                Write-Host 'Visualizer: installing packages (first run)' -ForegroundColor Cyan
                Push-Location $visualizer
                try { & npm install --no-fund --no-audit } finally { Pop-Location }
                if ($LASTEXITCODE -ne 0) { throw 'npm install failed.' }
            }
            $npmScript = 'dev'
            if ($Prod) {
                Write-Host 'Visualizer: building the optimised version' -ForegroundColor Cyan
                Push-Location $visualizer
                try { & npm run build } finally { Pop-Location }
                if ($LASTEXITCODE -ne 0) { throw 'npm run build failed.' }
                $npmScript = 'preview'
            }
            Write-Host "Visualizer: starting on http://localhost:5173 ($npmScript)" -ForegroundColor Cyan
            # npm is a .cmd shim on Windows, so it has to go through cmd.exe.
            $started += Start-Process -FilePath 'cmd.exe' -WorkingDirectory $visualizer -NoNewWindow -PassThru `
                -ArgumentList '/c', "npm run $npmScript"
        }
    }

    if ($Open -and -not $BackendOnly -and (Test-Path $visualizer)) {
        for ($i = 0; $i -lt 60 -and -not (Test-Port 5173); $i++) { Start-Sleep -Milliseconds 500 }
        Start-Process 'http://localhost:5173'
    }

    if (-not $started) {
        Write-Host 'Nothing new to start; both ports were already in use.' -ForegroundColor Yellow
        return
    }

    Write-Host 'Press Ctrl+C to stop.' -ForegroundColor DarkGray
    while ($true) {
        foreach ($r in $logs) {
            while ($null -ne ($line = $r.ReadLine())) { Write-Host $line }
        }
        foreach ($p in $started) {
            if ($p.HasExited) { throw "A process exited with code $($p.ExitCode). Stopping the rest." }
        }
        Start-Sleep -Seconds 1
    }
} finally {
    foreach ($p in $started) { Stop-Tree $p }
    foreach ($r in $logs) { $r.Dispose() }
    if ($started) { Write-Host 'Stopped.' -ForegroundColor DarkGray }
}
