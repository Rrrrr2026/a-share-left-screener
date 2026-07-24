#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易日收盘后:自动刷新数据 → 发布到 GitHub Pages → 发摘要邮件。
由 launchd (com.ashare.leftscreener) 在收盘后的多个时刻触发, 全流程幂等:

  · 周末 / 未收盘 → 直接退出;
  · 当天已完整跑成一轮 (data/.cycle_done_<BJ日期>) → 退出;
  · 已有当天数据 (dashboard_data.js 的 run_date == 今天) → 跳过~35min管线,
    只做发布 + 邮件 (供手动已跑完后补发, 或邮件失败后轻量重试);
  · 邮件成功(或当天已发过)才写 cycle_done → 邮件失败下一轮自动重试/补发,
    与涨停系统同样的"网络一通即补发"韧性。

单独手动补跑:  .venv/bin/python auto_refresh.py [--force]
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from ashare.config import DATA_DIR, DASHBOARD_DIR, DASHBOARD_DATA_JS  # noqa: E402
from ashare import mailer  # noqa: E402

LOG_PATH = os.path.join(DATA_DIR, "auto_refresh.log")
PIPELINE_LOG = os.path.join(DATA_DIR, "pipeline_run.log")
LOCK_PATH = os.path.join(DATA_DIR, ".auto_refresh.lock")
DOCS_DIR = os.path.join(ROOT, "docs")
BJ = dt.timezone(dt.timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("auto_refresh")


def bj_now() -> dt.datetime:
    return dt.datetime.now(BJ)


def data_run_date() -> str | None:
    try:
        p = mailer.load_payload_from_js(DASHBOARD_DATA_JS)
        return (p.get("meta") or {}).get("run_date")
    except Exception:
        return None


def _run(cmd, timeout, **kw):
    """跑子进程, 回传 (returncode, stdout+stderr 尾部)。失败不抛。"""
    try:
        r = subprocess.run(cmd, cwd=ROOT, timeout=timeout, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        return r.returncode, (r.stdout or "")[-800:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def external_pipeline_running() -> bool:
    """是否有另一个 run_pipeline.py 在跑 (手动补跑时避免并发双开管线, 防 SQLite 争用)。"""
    try:
        out = subprocess.run(["pgrep", "-f", "run_pipeline.py"], text=True,
                             stdout=subprocess.PIPE).stdout
        return any(x.strip().isdigit() and int(x) != os.getpid()
                   for x in out.split())
    except Exception:
        return False


def run_pipeline() -> bool:
    log.info("启动数据管线 run_pipeline.py (约 35 分钟, 慢时更久) ...")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    with open(PIPELINE_LOG, "w", encoding="utf-8") as f:
        try:
            r = subprocess.run([sys.executable, "run_pipeline.py"], cwd=ROOT,
                               stdout=f, stderr=subprocess.STDOUT, env=env,
                               timeout=3 * 3600)
            ok = r.returncode == 0
        except Exception as e:  # noqa: BLE001
            log.error("管线异常: %s", e)
            ok = False
    log.info("管线结束: %s (日志见 %s)", "成功" if ok else "失败", PIPELINE_LOG)
    return ok


def publish() -> bool:
    """复制 dashboard → docs, 提交并推送 (与 发布更新到网上.bat 一致, 幂等)。"""
    import shutil
    for name in ("index.html", "dashboard_data.js"):
        src = os.path.join(DASHBOARD_DIR, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(DOCS_DIR, name))
    rd = data_run_date() or bj_now().date().isoformat()
    _run(["git", "add", "docs"], 60)
    code, _ = _run(["git", "commit", "-m", f"auto update {rd}"], 60)
    if code != 0:
        log.info("git commit 无变化 (docs 已是最新), 跳过推送")
        return True   # 已是最新也算发布成功
    _run(["git", "pull", "--rebase", "--autostash", "origin", "main"], 120)
    code, out = _run(["git", "push", "origin", "main"], 120)
    if code == 0:
        log.info("已推送到 GitHub Pages (%s)", rd)
        return True
    log.error("git push 失败: %s", out)
    return False


def cycle_done_marker(date_str: str) -> str:
    return os.path.join(DATA_DIR, f".cycle_done_{date_str}")


def main() -> int:
    force = "--force" in sys.argv
    now = bj_now()
    today = now.date().isoformat()

    if not force:
        if now.weekday() >= 5:                       # 周六=5 周日=6
            log.info("[%s] 周末, A股休市, 退出", today)
            return 0
        if now.hour < 15 or (now.hour == 15 and now.minute < 5):
            log.info("[%s] %02d:%02d 未收盘, 退出", today, now.hour, now.minute)
            return 0
        if os.path.exists(cycle_done_marker(today)):
            log.info("[%s] 今日已完整跑过一轮, 退出", today)
            return 0

    # 防重入锁 (上一轮 35min 管线还没跑完时, 新触发直接退出)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        # 陈旧锁 (>4h) 视为残留, 清掉重来
        try:
            age = dt.datetime.now().timestamp() - os.path.getmtime(LOCK_PATH)
            if age > 4 * 3600:
                os.remove(LOCK_PATH); log.warning("清理陈旧锁, 重试")
                return main()
        except OSError:
            pass
        log.info("[%s] 另一轮正在运行(锁存在), 退出", today)
        return 0

    try:
        if data_run_date() == today and not force:
            log.info("[%s] 已有当天数据, 跳过管线, 直接发布+发信", today)
        elif external_pipeline_running():
            log.info("[%s] 检测到外部 run_pipeline.py 正在运行, 本轮跳过管线 (下次重试)",
                     today)
            return 1
        else:
            if not run_pipeline():
                log.error("[%s] 管线失败, 本轮放弃 (下一触发重试)", today)
                return 1

        publish()                                    # 幂等, 失败也继续尝试发信
        ok = mailer.send_summary_email(force=force)  # 按 run_date 幂等
        if ok:
            try:
                open(cycle_done_marker(today), "w").write("ok\n")
            except OSError:
                pass
            log.info("[%s] ✅ 本轮完成 (刷新+发布+邮件)", today)
            return 0
        log.warning("[%s] 邮件未发出 (网络?), 不写完成标记, 下轮重试/补发", today)
        return 1
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
