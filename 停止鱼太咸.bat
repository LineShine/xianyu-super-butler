@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\windows\Yutaixian.ps1" -Action Stop
set YTX_EXIT=%ERRORLEVEL%
if not "%YTX_EXIT%"=="0" pause
exit /b %YTX_EXIT%
