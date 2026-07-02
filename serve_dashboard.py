#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网访问监控台 (LAN server)
=============================
在本机起一个小型 HTTP 服务, 让**同一 WiFi/路由器**下的手机/其他电脑直接用浏览器看结果。
    python serve_dashboard.py
然后按提示在别的设备浏览器里打开显示的 http://<本机IP>:8765/ 。
(首次运行 Windows 防火墙可能弹窗, 选择"允许访问"即可。)
"""
from __future__ import annotations
import os
import sys
import socket
import http.server
import socketserver

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

PORT = int(os.environ.get("ASHARE_PORT", "8765"))
DASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")


def lan_ip() -> str:
    """获取本机在局域网里的 IPv4 (不真正发包)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH_DIR, **k)

    def log_message(self, fmt, *args):  # 安静一点
        pass


def main():
    if not os.path.exists(os.path.join(DASH_DIR, "index.html")):
        print("找不到 dashboard/index.html, 请先运行 python run_pipeline.py 生成结果。")
        return
    ip = lan_ip()
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("=" * 56)
        print("  A股左侧监控台 · 局域网服务已启动")
        print("  本机打开:      http://127.0.0.1:%d/" % PORT)
        print("  其他设备打开:  http://%s:%d/   (需连同一WiFi/路由器)" % (ip, PORT))
        print("  停止: 按 Ctrl + C")
        print("=" * 56)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


if __name__ == "__main__":
    main()
