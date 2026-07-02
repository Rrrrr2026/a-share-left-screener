@echo off
rem A-share left-side screener: fetch data, publish to GitHub Pages.
rem Used by both manual double-click and the daily scheduled task.
rem ASCII-only + full python path for reliable unattended runs.
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0data" mkdir "%~dp0data"
set "LOG=%~dp0data\update.log"
set "PYEXE=C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYEXE%" set "PYEXE=py"

echo ============================================================
echo   A-share screener - update and publish   %date% %time%
echo ============================================================
echo ============ %date% %time% START ============ >> "%LOG%"

echo [1/3] Fetch data and score (about 10-15 min) ...
"%PYEXE%" run_pipeline.py >> "%LOG%" 2>&1

echo [2/3] Copy result to docs/ ...
copy /Y dashboard\index.html docs\index.html >nul
copy /Y dashboard\dashboard_data.js docs\dashboard_data.js >nul

echo [3/3] Push to GitHub Pages ...
git add docs >> "%LOG%" 2>&1
git commit -m "auto update data %date% %time%" >> "%LOG%" 2>&1
rem 推送前先拉取(万一远端被 GitHub Desktop 等手动改动过, 避免推送被拒)
git pull --rebase origin main >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

echo ============ %date% %time% DONE ============ >> "%LOG%"
echo.
echo Done. Wait 1-2 min, then refresh:
echo   https://rrrrr2026.github.io/a-share-left-screener/
echo (log: data\update.log)

rem When launched by the scheduler with "auto", do not pause.
if /I "%~1"=="auto" goto :eof
echo.
pause
