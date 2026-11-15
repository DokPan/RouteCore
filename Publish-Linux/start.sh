#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SITE_URL='http://localhost:8085'
readonly SITE_LABEL='Интерфейс диспетчера'
readonly DOCKER_ERROR='Не удалось запустить RouteCore. Проверьте Docker и docker compose logs.'

cd "$PROJECT_ROOT"
compose_args=(up --build --wait)
if [[ "${1:-}" == "--offline" ]]; then
    compose_args=(-f docker/compose.offline.yml up --wait)
fi
if ! docker compose "${compose_args[@]}"; then
    printf '%s\n' "$DOCKER_ERROR" >&2
    exit 1
fi
printf '%s — %s\n' "$SITE_LABEL" "$SITE_URL"
