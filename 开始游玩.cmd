@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\play_local.ps1"
if errorlevel 1 pause
