@echo off
setlocal
cd /d "%~dp0"

echo ======================================================================
echo   Запуск сервиса умной диспетчеризации Ростелеком (Локально)
echo ======================================================================
echo.
echo Открытие веб-интерфейса: http://localhost:8085
timeout /t 2 /nobreak >nul
start "" "http://localhost:8085"

python run_server.py
pause
