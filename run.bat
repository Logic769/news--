@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title News Retrieval System - Start

cd /d "%~dp0"

echo.
echo ==================================================
echo   News Retrieval System - Start
echo   URL: http://127.0.0.1:5000
echo ==================================================
echo.

rem Load .env if it exists. Lines beginning with # are ignored.
if exist ".env" (
    echo [OK] Found .env, loading environment variables...
    for /f "usebackq tokens=1,* delims== eol=#" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
) else (
    echo [WARN] .env not found. Using config.py defaults.
)

rem Select Python command.
set "PYTHON_CMD=python"
python --version >nul 2>&1
if errorlevel 1 (
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found. Please install Python 3 and add it to PATH.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=py -3"
)

rem Check MySQL port and try to start common MySQL services if needed.
set "CHECK_PORT=3306"
if defined MYSQL_PORT set "CHECK_PORT=%MYSQL_PORT%"

echo.
echo [INFO] Checking MySQL port %CHECK_PORT%...
netstat -ano | findstr /R /C:":%CHECK_PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [WARN] MySQL is not listening on port %CHECK_PORT%. Trying to start service...
    set "MYSQL_STARTED=0"

    for %%S in (MySQL MySQL80 MySQL57 mysql) do (
        if "!MYSQL_STARTED!"=="0" (
            sc query "%%S" >nul 2>&1
            if not errorlevel 1 (
                sc query "%%S" | findstr /I "RUNNING" >nul 2>&1
                if not errorlevel 1 (
                    set "MYSQL_STARTED=1"
                ) else (
                    net start "%%S" >nul 2>&1
                    if not errorlevel 1 set "MYSQL_STARTED=1"
                )
            )
        )
    )

    if "!MYSQL_STARTED!"=="0" (
        echo [ERROR] Could not start MySQL automatically.
        echo         Please start MySQL manually, then run this file again.
        pause
        exit /b 1
    )

    echo [INFO] Waiting for MySQL to become ready...
    for /l %%I in (1,1,30) do (
        netstat -ano | findstr /R /C:":%CHECK_PORT% .*LISTENING" >nul 2>&1
        if not errorlevel 1 goto mysql_ready
        timeout /t 1 /nobreak >nul
    )

    echo [ERROR] MySQL did not become ready in time.
    pause
    exit /b 1
)

:mysql_ready
echo [OK] MySQL is ready on port %CHECK_PORT%.

echo.
echo [INFO] Checking Python dependencies...
%PYTHON_CMD% -c "import flask, pymysql, bs4, flask_cors" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Missing dependencies. Installing from requirements.txt...
    %PYTHON_CMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

%PYTHON_CMD% -c "import playwright" >nul 2>&1
if not errorlevel 1 (
    %PYTHON_CMD% -m playwright install chromium >nul 2>&1
)

echo.
echo [INFO] Starting Flask application...
echo [INFO] Press Ctrl+C to stop, or run stop.bat in another CMD window.
echo.

%PYTHON_CMD% app.py

echo.
echo [INFO] Application exited.
pause
endlocal
