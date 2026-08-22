#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看门狗: 以子进程跑 run_pipeline.py, 流水线每 60 秒更新 data/heartbeat.txt;
心跳 20 分钟不动就判定卡死 -> 杀掉子进程并重试一次。
背景: 2026-08-13/17/18/19 四次定时任务卡死在无超时的网络读上, 调度器 4 小时
后才杀任务且留下孤儿进程, 当天不出榜。socket 默认超时已加, 这里再兜一层底。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
HB = os.path.join(ROOT, "data", "heartbeat.txt")
STALE_SEC = 20 * 60
MAX_RUNS = 2


def _run_once(extra_args):
    try:
        os.remove(HB)
    except OSError:
        pass
    cmd = [sys.executable, "-X", "utf8", os.path.join(ROOT, "run_pipeline.py")] + extra_args
    p = subprocess.Popen(cmd, cwd=ROOT)
    start = time.time()
    while True:
        rc = p.poll()
        if rc is not None:
            return rc
        time.sleep(30)
        last = os.path.getmtime(HB) if os.path.exists(HB) else start
        if time.time() - last > STALE_SEC:
            print(f"[watchdog] heartbeat stale {int(time.time() - last)}s -> killing pipeline",
                  flush=True)
            p.kill()
            try:
                p.wait(60)
            except Exception:
                pass
            return -9


def main():
    extra = sys.argv[1:]
    for i in range(1, MAX_RUNS + 1):
        rc = _run_once(extra)
        print(f"[watchdog] run {i} exited with {rc}", flush=True)
        if rc == 0:
            sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
