[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$install = Join-Path $env:LOCALAPPDATA 'MusicStream\Prowlarr'
$exe = Join-Path $install 'app\Prowlarr\Prowlarr.exe'
$data = Join-Path $install 'data'
$config = Join-Path $data 'config.xml'
$database = Join-Path $data 'prowlarr.db'
$url = 'http://127.0.0.1:18746'

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

function Test-ProwlarrHealth {
    try {
        $status = Invoke-RestMethod -Uri "$url/api/v1/system/status" -TimeoutSec 2 `
            -Headers @{ 'X-Api-Key' = $apiKey }
        return [string]::Equals([string]$status.appData, $data,
                                [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Prowlarr.exe не найден: $exe. Проверьте постоянную установку."
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf) -or
    -not (Test-Path -LiteralPath $database -PathType Leaf)) {
    throw "База Prowlarr неполная: $data. Запуск без действующего -data запрещён."
}

[xml]$settings = Get-Content -LiteralPath $config -Raw
if ([string]$settings.Config.BindAddress -ne '127.0.0.1' -or
    [string]$settings.Config.Port -ne '18746') {
    throw 'Остановлено: config.xml должен содержать BindAddress=127.0.0.1 и Port=18746.'
}
$apiKey = [string]$settings.Config.ApiKey
if (-not $apiKey) {
    throw 'В действующей конфигурации Prowlarr отсутствует API-ключ.'
}

$processes = @(Get-Process -Name Prowlarr -ErrorAction SilentlyContinue)
if ($processes.Count -gt 1) {
    throw 'Найдено несколько процессов Prowlarr. Остановите лишние копии вручную.'
}
if ($processes.Count -eq 1) {
    $runningPath = $processes[0].Path
    if (-not $runningPath -or
        -not [string]::Equals($runningPath, $exe, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Уже работает Prowlarr из другого каталога. Не запускаю вторую копию с этой базой.'
    }
    if (-not (Test-LocalPort 18746) -or -not (Test-ProwlarrHealth)) {
        throw 'Prowlarr уже запущен, но 127.0.0.1:18746 не отвечает. Проверьте его журнал.'
    }
    Write-Host "Prowlarr уже работает: $url"
    return
}

if (Test-LocalPort 18746) {
    throw 'Порт 127.0.0.1:18746 занят другим процессом. Prowlarr не запущен.'
}

$arguments = @("-data=`"$data`"", '-nobrowser')
$started = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory (Split-Path $exe) `
    -WindowStyle Hidden -PassThru
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if ($started.HasExited) {
        throw "Prowlarr завершился при запуске (код $($started.ExitCode)). Проверьте $data\logs."
    }
    if (Test-ProwlarrHealth) {
        Write-Host "Prowlarr готов: $url (PID $($started.Id), data=$data)"
        return
    }
    Start-Sleep -Milliseconds 400
}
throw "Prowlarr не ответил за 30 секунд на $url. Проверьте $data\logs."
