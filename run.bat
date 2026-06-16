@echo off
chcp 65001 >nul
title 南昌航空大学新闻系统

:: 检查 .env 文件
if exist .env (
    echo [√] 已找到 .env 配置文件
) else (
    echo [!] 未找到 .env，使用 config.py 默认配置
    echo     如需自定义，请复制 .env.example 为 .env 并修改
    echo.
)

:: 检查依赖
echo [*] 检查 Python 依赖...
python -c "import flask, pymysql, bs4" 2>nul
if %errorlevel% neq 0 (
    echo [!] 缺少依赖，正在安装...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [×] 依赖安装失败
        pause
        exit /b 1
    )
)

:: 加载 .env
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        set %%a=%%b
    )
)

echo.
echo ==================================================
echo   南昌航空大学新闻系统
echo   访问地址: http://127.0.0.1:5000
echo ==================================================
echo.

python app.py
pause
