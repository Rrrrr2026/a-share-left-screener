#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看门狗: 以子进程跑 run_pipeline.py, 子进程"有进展"时才更新 data/heartbeat.txt
(进展 = 扫描/基本面/档案循环每完成一只 或 任意一条日志), 心跳 25 分钟不动就
判定卡死 -> 杀掉并重试一次; 重试只在距任务 4 小时上限还有足够时间时才做。
心跳带本轮令牌: 孤儿进程或手动运行写的心跳不会被误认为是被监控子进程的。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
HB = os.path.join(ROOT, "data", "heartbeat.txt")
STALE_SEC = 25 * 60
BUDGET_SEC = 3 * 3600 + 50 * 60      # 调度任务 ExecutionTimeLimit 为 4h, 留 10 分钟给发布步骤
MIN_RETRY_SEC = 100 * 60             # 一轮完整流水线约 1.5-2.3h; 剩余不足则不重试
MAX_RUNS = 2


def _hb_is_mine(token):
    try:
        with open(HB, "r", encoding="utf-8") as f:
            return f.read().strip().split("|")[0] == token
    except OSError:
        return False


def _run_once(extra_args, token):
    os.makedirs(os.path.dirname(HB), exist_ok=True)
    try:
        os.remove(HB)
    except OSError:
        pass
    env = dict(os.environ, LS_HB_TOKEN=token)
    cmd = [sys.executable, "-X", "utf8", os.path.join(ROOT, "run_pipeline.py")] + extra_args
    p = subprocess.Popen(cmd, cwd=ROOT, env=env)
    start = time.time()
    while True:
        rc = p.poll()
        if rc is not None:
            return rc
        time.sleep(30)
        last = os.path.getmtime(HB) if (os.path.exists(HB) and _hb_is_mine(token)) else start
        if time.time() - last > STALE_SEC:
            print(f"[watchdog] no progress for {int(time.time() - last)}s -> killing pipeline", flush=True)
            p.kill()
            try:
                p.wait(60)
            except Exception:
                pass
            return -9


def main():
    extra = sys.argv[1:]
    t0 = time.time()
    for i in range(1, MAX_RUNS + 1):
        token = f"{os.getpid()}-{int(time.time())}-{i}"
        rc = _run_once(extra, token)
        print(f"[watchdog] run {i} exited with {rc}", flush=True)
        if rc == 0:
            sys.exit(0)
        remaining = BUDGET_SEC - (time.time() - t0)
        if remaining < MIN_RETRY_SEC:
            print(f"[watchdog] only {int(remaining / 60)} min left before the task limit; not retrying", flush=True)
            break
    sys.exit(1)


if __name__ == "__main__":
    main()
