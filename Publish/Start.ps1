param(
    [switch]$Local,
    [switch]$Offline,
    [string]$RoadVolume
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SiteUrl = 'http://localhost:8085'
$DockerError = 'Docker Compose завершился с ошибкой. Проверьте Docker Desktop и docker compose logs.'
$LocalError = 'Локальный сервер завершился с ошибкой.'
$MissingPython = 'Python не найден. Установите Python и зависимости проекта.'
$MissingDocker = 'Docker не найден. Запустите Docker Desktop.'
$LocalAddress = 'Интерфейс диспетчера'

$PreviousRoadVolume = $env:ROUTECORE_ROAD_VOLUME
Push-Location -LiteralPath $ProjectRoot
try {
    if ($Local) {
        if (!(Get-Command python -ErrorAction SilentlyContinue)) { throw $MissingPython }
        Write-Host "$LocalAddress — $SiteUrl"
        python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8085 --reload
        if ($LASTEXITCODE -ne 0) {
            throw $LocalError
        }
    } else {
        if (!(Get-Command docker -ErrorAction SilentlyContinue)) { throw $MissingDocker }
        $ComposeArguments = @('up', '--build', '--wait')
        if ($Offline) {
            $ComposeArguments = @('-f', 'docker/compose.offline.yml', 'up', '--wait')
        }
        if ($RoadVolume) { $env:ROUTECORE_ROAD_VOLUME = $RoadVolume }
        docker compose @ComposeArguments
        if ($LASTEXITCODE -ne 0) {
            throw $DockerError
        }
        Start-Process $SiteUrl
    }
} finally {
    $env:ROUTECORE_ROAD_VOLUME = $PreviousRoadVolume
    Pop-Location
}
