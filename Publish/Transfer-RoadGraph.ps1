param(
    [Parameter(Mandatory)]
    [ValidateSet('Export', 'Import')]
    [string]$Mode,

    [string]$ArchiveDirectory,

    [string]$VolumeName,

    [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TransferFailed = 'Не удалось перенести дорожный граф.'
$ArchiveMissing = 'Архив road-graph.tar.gz не найден.'
$VolumeExists = 'Том для импорта уже существует. Укажите новое имя через -VolumeName.'

if (!$ArchiveDirectory) { $ArchiveDirectory = $ProjectRoot }
$ArchiveDirectory = (Resolve-Path -LiteralPath $ArchiveDirectory).Path
if (!$VolumeName) {
    $VolumeName = if ($Mode -eq 'Export') { 'routecore_road_data' } else { 'routecore_road_import' }
}
$Archive = Join-Path $ArchiveDirectory 'road-graph.tar.gz'
$ExistingVolumes = docker volume ls --format '{{.Name}}'
if ($LASTEXITCODE -ne 0) { throw $TransferFailed }

if ($Mode -eq 'Export') {
    if ($VolumeName -notin $ExistingVolumes) { throw $TransferFailed }
    docker run --rm -v "${VolumeName}:/roads:ro" --mount "type=bind,source=$ArchiveDirectory,target=/transfer" python:3.11-slim tar -czf /transfer/road-graph.tar.gz -C /roads .
    if ($LASTEXITCODE -ne 0) { throw $TransferFailed }
    Write-Output $Archive
} else {
    if (!(Test-Path -LiteralPath $Archive -PathType Leaf)) { throw $ArchiveMissing }
    if ($VolumeName -in $ExistingVolumes) { throw $VolumeExists }
    docker volume create $VolumeName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw $TransferFailed }
    docker run --rm -v "${VolumeName}:/roads" --mount "type=bind,source=$ArchiveDirectory,target=/transfer,readonly" python:3.11-slim tar -xzf /transfer/road-graph.tar.gz -C /roads
    if ($LASTEXITCODE -ne 0) { throw $TransferFailed }
    if (!$SkipStart) {
        & (Join-Path $PSScriptRoot 'Start.ps1') -RoadVolume $VolumeName
    }
}
