@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "LOG=%~dp0data\update.log"
if not exist "%~dp0data" mkdir "%~dp0data"

echo ============================================================
echo   A股左侧监控台 - 一键更新并发布
echo   开始: %date% %time%
echo ============================================================
echo ============ %date% %time% START ============ >> "%LOG%"

echo [1/3] 抓取当天行情并打分 (约 10-15 分钟, 请耐心等待) ...
where py >nul 2>&1
if errorlevel 1 (
  "C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe" run_pipeline.py >> "%LOG%" 2>&1
) else (
  py run_pipeline.py >> "%LOG%" 2>&1
)

echo [2/3] 复制结果到 docs/ ...
copy /Y dashboard\index.html docs\index.html >nul
copy /Y dashboard\dashboard_data.js docs\dashboard_data.js >nul

echo [3/3] 推送到 GitHub Pages ...
git add docs >> "%LOG%" 2>&1
git commit -m "自动更新数据 %date% %time%" >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

echo ============ %date% %time% DONE ============ >> "%LOG%"
echo.
echo 完成! 稍等 1-2 分钟, 刷新网页看最新数据:
echo   https://rrrrr2026.github.io/a-share-left-screener/
echo (详细运行日志: data\update.log)

rem 被定时任务以 "auto" 参数调用时不暂停; 手动双击时暂停好让你看结果
if /I "%~1"=="auto" goto :eof
echo.
pause
