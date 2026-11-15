@echo off
setlocal
cd /d "%~dp0.." || goto :failed

docker compose -f "%~dp0docker-compose.yml" --project-directory "%~dp0.." up --build --wait --remove-orphans
if errorlevel 1 goto :failed

start "" "http://localhost:8085"
exit /b 0

:failed
echo Docker Compose failed. Make sure Docker Desktop is running and port 8085 is free.
pause
exit /b 1
