[CmdletBinding()]
param(
    [switch]$EnableExperimentalTorrent,
    [ValidateRange(1, 65535)][int]$MusicStreamPort = 8000
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$url = "http://127.0.0.1:$MusicStreamPort"

function Test-LocalPort([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(500) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-MusicStreamHealth {
    try {
        $health = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2
        $identity = Invoke-RestMethod -Uri "$url/" -TimeoutSec 2
        return $health.status -eq 'ok' -and $identity.project -eq 'MusicStream'
    } catch {
        return $false
    }
}

& (Join-Path $PSScriptRoot 'start-prowlarr.ps1')

if (Test-LocalPort $MusicStreamPort) {
    if (-not (Test-MusicStreamHealth)) {
        throw "Порт 127.0.0.1:$MusicStreamPort занят другим сервисом или MusicStream не отвечает."
    }
    Write-Host "MusicStream уже работает: $url/app"
    return
}

$venvPython = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw 'Не найдено виртуальное окружение .venv. Выполните uv sync в каталоге MusicStream.'
}
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    $launcher = $uv.Source
    $arguments = @('run', '--no-sync', 'uvicorn', 'musicstream.main:app',
                   '--host', '127.0.0.1', '--port', "$MusicStreamPort")
} else {
    # The project environment may have been created by uv while uv itself is
    # absent from the current PATH. Its installed Python is sufficient to run.
    $launcher = $venvPython
    $arguments = @('-m', 'uvicorn', 'musicstream.main:app',
                   '--host', '127.0.0.1', '--port', "$MusicStreamPort")
}

$logDir = Join-Path $env:LOCALAPPDATA 'MusicStream\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outLog = Join-Path $logDir "musicstream-$stamp.out.log"
$errLog = Join-Path $logDir "musicstream-$stamp.err.log"

$keys = @('MUSICSTREAM_TORRENT_HTTP_ENABLED', 'MUSICSTREAM_TORRENT_AUTO_ENABLED',
          'MUSICSTREAM_TORRENT_NETWORK_MODE')
$saved = @{}
try {
    if (-not $EnableExperimentalTorrent) {
        foreach ($key in $keys) {
            $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        }
        [Environment]::SetEnvironmentVariable('MUSICSTREAM_TORRENT_HTTP_ENABLED', 'false', 'Process')
        [Environment]::SetEnvironmentVariable('MUSICSTREAM_TORRENT_AUTO_ENABLED', 'false', 'Process')
        [Environment]::SetEnvironmentVariable('MUSICSTREAM_TORRENT_NETWORK_MODE', 'loopback', 'Process')
    }
    $started = Start-Process -FilePath $launcher -ArgumentList $arguments `
        -WorkingDirectory $project -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
} finally {
    if (-not $EnableExperimentalTorrent) {
        foreach ($key in $keys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
        }
    }
}

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if (Test-MusicStreamHealth) {
        Write-Host "MusicStream готов: $url/app"
        return
    }
    Start-Sleep -Milliseconds 400
}
if ($started.HasExited) {
    throw "MusicStream не ответил и процесс запуска завершился (код $($started.ExitCode)). Проверьте $errLog."
}
throw "MusicStream не ответил за 30 секунд на $url. Проверьте $errLog."
