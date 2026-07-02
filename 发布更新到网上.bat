@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo == 把最新结果发布到 GitHub Pages ==
echo 复制最新网页与数据到 docs/ ...
copy /Y dashboard\index.html docs\index.html >nul
copy /Y dashboard\dashboard_data.js docs\dashboard_data.js >nul
echo 提交并推送 ...
git add docs
git commit -m "更新数据 %date% %time%"
if errorlevel 1 echo (没有变化或提交失败, 若提示 nothing to commit 属正常)
git push
echo.
echo 完成! 稍等 1-2 分钟, 刷新你的 GitHub Pages 网址即可看到最新数据。
pause
