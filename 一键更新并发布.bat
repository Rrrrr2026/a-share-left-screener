@echo off
rem 手动一键更新: 直接双击本文件即可。实际逻辑在 auto_update.bat。
cd /d "%~dp0"
call auto_update.bat %*
