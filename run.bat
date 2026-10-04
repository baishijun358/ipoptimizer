@echo off
rem 双击本文件即可启动《数码解码 IP 优选器 V1.4》
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem 优先使用 python 命令；如果没有（例如未加入 PATH），就使用默认安装位置
where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python main.py
) else (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" main.py
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo 程序退出时出现错误，详细信息请查看 logs\app.log
    pause
)