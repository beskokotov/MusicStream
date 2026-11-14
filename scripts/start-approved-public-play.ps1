[CmdletBinding()]
param(
    [Parameter(Mandatory, ParameterSetName='Approved')][ValidatePattern('^sha256:[0-9a-fA-F]{64}$')]
    [string]$ReleaseFingerprint,
    [Parameter(Mandatory, ParameterSetName='Approved')][ValidatePattern('^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$')]
    [string]$InfoHash,
    [Parameter(Mandatory, ParameterSetName='Catalog')][switch]$CatalogMode,
    [Parameter(Mandatory)][string]$BindIp,
    [string[]]$IndexerPriority = @('The Pirate Bay', 'NoNaMe Club'),
    [ValidateRange(1, 65535)][int]$MusicStreamPort = 8000
)

$ErrorActionPreference = 'Stop'
if (-not $IndexerPriority -or @($IndexerPriority | Where-Object { -not $_.Trim() }).Count -gt 0) {
    throw 'IndexerPriority requires one or more indexer names.'
}
$address = [Net.IPAddress]::Parse($BindIp)
if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    [Net.IPAddress]::IsLoopback($address) -or $address.Equals([Net.IPAddress]::Any)) {
    throw 'BindIp must be a local IPv4 address on the selected network interface.'
}

$port = [Net.Sockets.TcpClient]::new()
try {
    $port.Connect('127.0.0.1', $MusicStreamPort)
    throw "Port 127.0.0.1:$MusicStreamPort is occupied. Stop the existing MusicStream first."
} catch [Net.Sockets.SocketException] {
    # Connection refused means the port is available for the local launcher.
} finally {
    $port.Dispose()
}

$config = Join-Path $env:LOCALAPPDATA 'MusicStream\Prowlarr\data\config.xml'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw 'Local Prowlarr configuration was not found.'
}
[xml]$settings = Get-Content -LiteralPath $config -Raw
$key = [string]$settings.Config.ApiKey
if (-not $key) { throw 'Local Prowlarr API key is missing.' }

$values = @{
    MUSICSTREAM_TORRENT_HTTP_ENABLED = 'true'
    MUSICSTREAM_TORRENT_AUTO_ENABLED = 'true'
    MUSICSTREAM_TORRENT_NETWORK_MODE = 'public_discovery'
    MUSICSTREAM_TORRENT_PUBLIC_BIND_IP = $BindIp
    MUSICSTREAM_TORRENT_PUBLIC_APPROVED_INFO_HASHES =
        $(if ($CatalogMode) { '[]' } else { '["' + $InfoHash.ToLowerInvariant() + '"]' })
    MUSICSTREAM_TORRENT_APPROVED_SOURCE_IDS =
        $(if ($CatalogMode) { '[]' } else { '["' + $ReleaseFingerprint.ToLowerInvariant() + '"]' })
    MUSICSTREAM_TORRENT_ALLOW_UNLISTED_PUBLIC =
        $(if ($CatalogMode) { 'true' } else { 'false' })
    MUSICSTREAM_TORRENT_INDEXER_PRIORITY = $(ConvertTo-Json -InputObject $IndexerPriority -Compress)
    MUSICSTREAM_TORRENT_ALLOWED_INDEXER_IDS = $null
    MUSICSTREAM_PROWLARR_API_KEY = $key
    MUSICSTREAM_TORRENT_AUTO_TEST_SOURCE = $null
    MUSICSTREAM_TORRENT_HTTP_SOURCE = $null
    MUSICSTREAM_TORRENT_HTTP_FILE_INDEXES = $null
    MUSICSTREAM_TORRENT_AUTO_READ_TIMEOUT = '45'
    MUSICSTREAM_TORRENT_PEER_CONNECT_TIMEOUT = '12'
    MUSICSTREAM_TORRENT_CACHE_MAX_BYTES = '16777216'
}
$previous = @{}
try {
    foreach ($name in $values.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
    }
    & (Join-Path $PSScriptRoot 'start-local.ps1') -EnableExperimentalTorrent `
        -MusicStreamPort $MusicStreamPort
} finally {
    foreach ($name in $values.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
