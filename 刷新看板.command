#!/bin/bash
# =====================================================================
#  A股左侧抄底监视器 —— 一键手动刷新 + 打开本地看板
#  用法: 双击本文件即可 (首次若提示"无法验证开发者" → 右键→打开)。
#  它只在本地跑, 不发邮件、不推送网上; 跑完自动用浏览器打开看板。
# =====================================================================
cd "/Users/rogerluo/Desktop/美股Claude Boosting/a-share-left-screener" || {
    echo "❌ 找不到项目目录"; read -n1 -p "按任意键关闭..."; exit 1; }

# 防并发: 已有一轮在跑就不重复启动(否则两个管线抢同一个数据库)
if pgrep -f run_pipeline.py >/dev/null 2>&1; then
    echo "⚠️ 已有一轮刷新正在运行, 不重复启动 —— 等它跑完即可。"
    echo "   先打开现有看板供查看..."
    open dashboard/index.html
    echo "此窗口可以关闭。"
    exit 0
fi

echo "=================================================="
echo "   A股左侧抄底监视器 · 手动刷新"
echo "   开始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=================================================="
echo "正在刷新全市场数据, 约 30-60 分钟 (取决于网络快慢)。"
echo "请勿关闭此窗口, 跑完会自动打开看板。"
echo

.venv/bin/python run_pipeline.py
STATUS=$?

echo
if [ $STATUS -eq 0 ]; then
    echo "✅ 刷新完成! 数据日期见页面顶部。正在打开本地看板..."
else
    echo "⚠️ 刷新中途报错 (退出码 $STATUS), 仍打开现有看板供查看。"
fi
open dashboard/index.html
echo "结束: $(date '+%Y-%m-%d %H:%M:%S')  —— 此窗口可以关闭了。"
