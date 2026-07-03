@echo off
cd /d "%~dp0"
if not exist "data" mkdir "data"
set "LOG=%~dp0data\update.log"
set "PYEXE=C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYEXE%" set "PYEXE=py"

echo ==================================================
echo   A-share screener: update and publish
echo ==================================================
echo ==== START ==== >> "%LOG%"

echo [1/3] Fetch data and score (about 10-15 min) ...
"%PYEXE%" run_pipeline.py >> "%LOG%" 2>&1

echo [2/3] Copy result to docs ...
copy /Y "dashboard\index.html" "docs\index.html" >nul
copy /Y "dashboard\dashboard_data.js" "docs\dashboard_data.js" >nul

echo [3/3] Publish to GitHub Pages ...
git add docs >> "%LOG%" 2>&1
git commit -m "auto update data" >> "%LOG%" 2>&1
git pull --rebase origin main >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

echo ==== DONE ==== >> "%LOG%"
echo.
echo Done. Wait 1-2 minutes, then open the "A-share dashboard" desktop shortcut.
echo (log file: data\update.log)
if /I "%~1"=="auto" goto :eof
echo.
pause
