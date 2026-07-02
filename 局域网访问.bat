@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动局域网监控台服务...
py serve_dashboard.py 2>nul || python serve_dashboard.py
pause
