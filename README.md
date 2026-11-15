# Запуск RouteCore

## Быстрый старт

Первый запуск со скачиванием карты и подготовкой OSRM может занять 5–15 минут или дольше. Это ориентир, время зависит от сети и компьютера. При повторном старте Docker использует готовый граф из тома.

Скачайте ветку и откройте корень проекта, где лежит `docker-compose.yml`.

```sh
git clone --branch main --single-branch https://github.com/DokPan/RouteCore.git
cd RouteCore
```

### Windows, автоматический запуск

1. Запустите Docker Desktop и дождитесь готовности движка.
2. В папке Publish проекта откройте двойным щелчком `Start.cmd` или в папке docker `Запустить_или_обновить_Docker.cmd`. Оба файла запускают Docker Compose, дожидаются готовности сервисов и открывают браузер. После обновления файлов проекта можно снова открыть любой из них.
3. Если браузер не открылся, перейдите на [localhost:8085](http://localhost:8085).

### Linux, автоматический запуск

Из корня проекта выполните:

```sh
bash Publish-Linux/start.sh
```

После сообщения о готовности откройте [localhost:8085](http://localhost:8085).

## Что понадобится

- **Windows.** Docker Desktop с Linux-контейнерами и WSL 2. Docker указывает минимум 8 ГБ оперативной памяти для Windows. Для первого построения графа OSRM удобнее иметь 16 ГБ на компьютере и около 20 ГБ свободного диска. Это ориентир с запасом под образы, временные файлы и кэш. [Требования Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
- **Linux.** Docker Engine и плагин Docker Compose v2. Используйте поддерживаемый вашим Docker Engine дистрибутив, например Ubuntu. [Установка Docker Engine на Ubuntu](https://docs.docker.com/engine/install/ubuntu/) · [Установка Compose](https://docs.docker.com/compose/install/linux/)
- Для скачивания репозитория нужен Git или архив ветки с GitHub. Первый запуск с OSRM требует доступа к реестрам Docker и Geofabrik.
- В простое сервер приложения использовал около **140 МБ** оперативной памяти, OSRM — около **480 МБ**. Дорожный граф занимал около **1,15 ГБ** на диске. Во время сборки графа и расчёта потребление памяти выше и зависит от версии карты и нагрузки.

## Windows и Linux, ручной запуск в консоли

Откройте PowerShell в Windows или терминал в Linux и выполните из корня проекта:

```sh
docker info
docker compose version
docker compose up --build --wait
docker compose ps
```

На первом запуске Compose скачает карту Уральского федерального округа и построит дорожный граф. Готовый граф повторно используется из тома Docker. Сервисы загрузки и подготовки карты завершаются после выполнения, а `backend` и `osrm` остаются запущены. Откройте [диспетчерскую](http://localhost:8085) или проверьте [состояние API](http://localhost:8085/api/v1/health).

```sh
docker compose logs --tail 80 osm-download osrm-prepare osrm backend
docker compose down
```

Первая команда показывает последние сообщения сервисов. Вторая останавливает проект и сохраняет тома. После обновления файлов снова выполните `docker compose up --build --wait`.

## Запуск без Docker

Этот режим подходит для проверки интерфейса и расчёта по доступной матрице. Для дорожных линий на карте нужен работающий OSRM.

**Windows, PowerShell**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8085
```

Если Python и зависимости уже доступны через команду `python`, можно открыть `Запустить_без_Docker.cmd`.

**Linux, Bash**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8085
```

Для остановки консольного сервера нажмите `Ctrl+C`. Для этого режима сначала освободите порт 8085, если на нём уже работает Docker.

<details>
<summary>Если Docker Desktop не запускается в Windows</summary>

Откройте PowerShell и проверьте состояние WSL:

```powershell
wsl --status
wsl -l -v
```

Если в списке есть Ubuntu, проверьте её запуск командой `wsl -d Ubuntu -- echo OK`. Если дистрибутив называется иначе, подставьте его имя из списка. При ошибке сохраните точный вывод команд и сообщение Docker Desktop.

Если Docker завис, сохраните работу в WSL и закройте Docker Desktop через значок в трее. Выполните `wsl --shutdown`, затем снова откройте Docker Desktop. Команда останавливает все запущенные дистрибутивы WSL. Когда Docker станет готов, повторите запуск RouteCore. [Команды WSL](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

</details>
