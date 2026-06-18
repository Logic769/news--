@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title News Retrieval System - Stop

cd /d "%~dp0"

set "PORT=5000"
if not "%~1"=="" set "PORT=%~1"

echo.
echo ==================================================
echo   News Retrieval System - Stop
echo   Port: %PORT%
echo ==================================================
echo.

set "FOUND=0"

echo [INFO] Searching for process listening on port %PORT%...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set "FOUND=1"
    echo [INFO] Stopping PID %%P...
    taskkill /F /T /PID %%P >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to stop PID %%P. Try running CMD as administrator.
    ) else (
        echo [OK] Stopped PID %%P.
    )
)

echo.
if "%FOUND%"=="0" (
    echo [WARN] No process is listening on port %PORT%.
) else (
    echo [OK] Stop command completed.
)

echo.
pause
endlocal
