@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Transfer-RoadGraph.ps1" -Mode Export
pause
