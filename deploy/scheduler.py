#!/usr/bin/env python3
"""deploy/scheduler.py — 给 coremem 装调度器（macOS launchd / Linux cron）。

为什么需要：coremem 只提供脚本，不提供调度。没有调度器，采集/整点重放/简报/
夜间编译/自检/备份都不会自己跑，记忆就不长。此前每个新机器都要手写 plist。

用法：
  python3 deploy/scheduler.py install [--prefix DIR] [--python PY] [--dry-run]
  python3 deploy/scheduler.py status
  python3 deploy/scheduler.py uninstall

  --prefix   脚本目录（默认 ~/.coremem/scripts，与 deploy.sh 默认一致）
  --python   解释器（默认自动探测，优先 3.10+）
  --dry-run  只打印将生成的任务，不写入、不加载

六个任务（都只用本机文件，不联网）：
  capture       每 30 分钟   扫描最近 1h 会话 → 抽取事实 → 写入记忆
  buffer-flush  每 60 分钟   重放失败写入（网断/限流/进程被杀都不丢）
  briefing      每天 08:00   生成当日简报
  nightly       每天 03:00   语义索引 → MEMORY.md 编译
  health        每天 09:00   自检，异常可接告警
  backup        每天 04:00   L2 语义图快照
"""
from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL_PREFIX = "com.coremem"

# (name, argv, interval_seconds | None, calendar {"Hour":h,"Minute":m} | None, stdout_name)
def _jobs():
    """任务表（改夜间任务用途见下）。"""
    import os
    # 夜间：给统一事实池建 FTS5 增量索引（让 L1 可召回）。
    # 这里曾经跑 memory_insight_scan --sync-compiled-truth，但该功能 2026-10-19 已显式退役
    # （AUTO 区块不再写 MEMORY.md）：每天只打印退役提示并非零退出，是噪声不是工作。
    # 确需恢复旧行为：MEM_NIGHTLY=insight-scan（还需 MEMORY_AUTO_ENABLE=1）。
    nightly_args = (["memory_insight_scan.py", "--sync-compiled-truth"]
                    if os.environ.get("MEM_NIGHTLY") == "insight-scan"
                    else ["memory_index.py"])
    return [
        ("capture", ["memory_capture.py", "--hours", "1"], 1800, None, "memory_capture_30m.log"),
        ("buffer-flush", ["memory_write_buffer.py", "flush"], 3600, None,
         f"{LABEL_PREFIX}.buffer-flush.log"),
        ("briefing", ["morning_briefing.py"], None, {"Hour": 8, "Minute": 0},
         f"{LABEL_PREFIX}.briefing.log"),
        ("nightly", nightly_args, None, {"Hour": 3, "Minute": 0},
         f"{LABEL_PREFIX}.nightly.log"),
        ("health", ["memory_health.py"], None, {"Hour": 9, "Minute": 0},
         f"{LABEL_PREFIX}.health.log"),
        ("backup", ["plugmem_backup.py"], None, {"Hour": 4, "Minute": 0},
         f"{LABEL_PREFIX}.backup.log"),
        # 2026-11-11 审计 P0-3：golden 门禁周更（周一 5:30），让 health 的 eval 探针
        # （新鲜度阈 192h/336h）有持续产物；需要 golden/golden-v0.json + 已 accept 基线。
        ("golden-gate", ["golden_eval.py", "--gate", "v2"], None,
         {"Hour": 5, "Minute": 30, "Weekday": 1}, f"{LABEL_PREFIX}.golden-gate.log"),
    ]


JOBS = _jobs()


def pick_python(explicit: str | None) -> str:
    if explicit:
        return str(Path(explicit).expanduser())
    for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3", shutil.which("python3")):
        if not cand or not Path(cand).exists():
            continue
        try:
            out = subprocess.run([cand, "-c", "import sys;print(sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=10)
            major, minor = eval(out.stdout.strip()) if out.stdout.strip() else (0, 0)
            if (major, minor) >= (3, 9):
                return cand
        except Exception:
            continue
    sys.exit("✗ 找不到 python3 (>=3.9)，请用 --python 指定")


def paths(prefix: Path | None):
    prefix = prefix or Path(os.environ.get("MEM_SCRIPTS_DIR", Path.home() / ".coremem" / "scripts"))
    logs = Path(os.environ.get("MEM_LOGS_DIR", Path.home() / ".coremem" / "logs"))
    return prefix, logs


def macos_install(prefix: Path, logs: Path, python: str, dry: bool) -> int:
    agents = Path.home() / "Library" / "LaunchAgents"
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "LANG": os.environ.get("LANG", "en_US.UTF-8")}
    rc = 0
    for name, argv, interval, cal, out_name in JOBS:
        label = f"{LABEL_PREFIX}.{name}"
        d = {
            "Label": label,
            "ProgramArguments": [python, *argv],
            "WorkingDirectory": str(prefix),
            "EnvironmentVariables": env,
            "StandardOutPath": str(logs / out_name),
            "StandardErrorPath": str(logs / (out_name.replace(".log", ".err.log"))),
            "RunAtLoad": False,
        }
        if interval:
            d["StartInterval"] = interval
        if cal:
            d["StartCalendarInterval"] = cal
        plist_path = agents / f"{label}.plist"
        if dry:
            print(f"[dry] {label}: {python} {' '.join(argv)} → {plist_path}")
            continue
        agents.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(d))
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                           capture_output=True, text=True)
        print(f"{'✓' if r.returncode == 0 else '✗'} {label}"
              + ("" if r.returncode == 0 else f" — {r.stderr.strip()[:80]}"))
        rc |= r.returncode
    return rc


def linux_install(prefix: Path, logs: Path, python: str, dry: bool) -> int:
    """Linux 走用户 crontab：与 launchd 语义对齐（interval + 固定钟点）。"""
    lines = ["# >>> coremem scheduler (deploy/scheduler.py) >>>",
             f"# scripts={prefix} logs={logs}"]
    for name, argv, interval, cal, out_name in JOBS:
        cmd = f"cd {prefix} && {python} {' '.join(argv)} >> {logs / out_name} 2>&1"
        if interval:
            lines.append(f"*/{interval // 60} * * * * {cmd}")
        else:
            lines.append(f"{cal['Minute']} {cal['Hour']} * * * {cmd}")
    lines.append("# <<< coremem scheduler <<<")
    block = "\n".join(lines)
    if dry:
        print(block)
        return 0
    logs.mkdir(parents=True, exist_ok=True)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    import re
    cur = re.sub(r"# >>> coremem scheduler.*?# <<< coremem scheduler <<<\n?", "", cur, flags=re.S)
    new = (cur.rstrip() + "\n" + block + "\n").lstrip("\n")
    p = subprocess.run(["crontab", "-"], input=new, text=True)
    print("✓ crontab 已更新" if p.returncode == 0 else "✗ crontab 写入失败")
    return p.returncode


def status() -> int:
    if platform.system() == "Darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        rows = [l for l in out.splitlines() if LABEL_PREFIX in l]
        if not rows:
            print("未安装 coremem 调度任务")
            return 1
        for l in sorted(rows, key=lambda s: s.split()[-1]):
            pid, last, label = l.split("\t")[0], l.split("\t")[1], l.split()[-1]
            print(f"{label:32s} pid={pid:>6s} last_exit={last}")
        return 0
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    has = "coremem scheduler" in out
    print("已安装（crontab）" if has else "未安装 coremem 调度任务")
    return 0 if has else 1


def uninstall() -> int:
    if platform.system() == "Darwin":
        uid = os.getuid()
        for name, *_ in JOBS:
            label = f"{LABEL_PREFIX}.{name}"
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            p = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if p.exists():
                p.rename(p.with_suffix(".plist.removed"))
            print(f"✓ 移除 {label}")
        return 0
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    import re
    new = re.sub(r"# >>> coremem scheduler.*?# <<< coremem scheduler <<<\n?", "", cur, flags=re.S)
    subprocess.run(["crontab", "-"], input=new, text=True)
    print("✓ 已从 crontab 移除 coremem 任务")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("install", "status", "uninstall"):
        s = sub.add_parser(cmd)
        if cmd == "install":
            s.add_argument("--prefix", type=Path, default=None)
            s.add_argument("--python", default=None)
            s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "status":
        return status()
    if a.cmd == "uninstall":
        return uninstall()

    prefix, logs = paths(getattr(a, "prefix", None))
    if not (prefix / "remember.py").exists():
        sys.exit(f"✗ {prefix} 里没有 remember.py —— 先跑 deploy/deploy.sh 再装调度器")
    python = pick_python(getattr(a, "python", None))
    print(f"scripts={prefix}\nlogs={logs}\npython={python}\nplatform={platform.system()}")
    if platform.system() == "Darwin":
        return macos_install(prefix, logs, python, a.dry_run)
    return linux_install(prefix, logs, python, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
