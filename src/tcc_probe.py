#!/usr/bin/env python3
"""TCC 写入探针：证明 launchd 上下文能否写 ~/Documents 下的 Obsidian Vault。

2026-11-19：harvest-audit1/2 rc=1 的真因是
`PermissionError: [Errno 1] Operation not permitted` 写
`~/Documents/Obsidian Vault/BOSS招聘/知识库/复盘/错过人才复盘.md`。
交互 shell 里可读可写，但 launchd 派生的进程被 TCC 拒 ——
这正是当初 wiki 迁出 Documents 的同一根因（非对称性）。

用法：
  直接跑（交互上下文）：python3 tcc_probe.py
  launchd 上下文：由 com.coremem.tcc-probe 任务跑，看日志

判据：真去写一个探针文件再删掉，**不以「目录可 ls」当可写**。
授权对象是 /usr/bin/python3（launchd 里 exec 的就是它）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
TARGET = Path(os.environ.get("MEM_TCC_TARGET") or
              (HOME / "Documents/Obsidian Vault/BOSS招聘/知识库/复盘"))
STAMP = HOME / ".coremem/health/tcc-probe.json"


def main() -> int:
    info = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": str(TARGET),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "executable": sys.executable,
        # launchd 派生的进程没有 TERM_PROGRAM/SSH_TTY 等交互标记
        "context": "launchd" if not os.environ.get("TERM_PROGRAM") else "interactive?",
    }
    exists = TARGET.is_dir()
    info["dir_exists"] = exists
    if not exists:
        info["result"] = "target_missing"
        print(f"目标目录不存在：{TARGET}")
        _write(info)
        return 1
    try:
        info["listable"] = len(os.listdir(TARGET)) >= 0
    except OSError as e:
        info["listable"] = False
        info["list_error"] = f"{type(e).__name__}: {e}"

    probe = TARGET / f".tcc-probe-{os.getpid()}"
    try:
        probe.write_text(f"tcc probe {time.time()}\n", encoding="utf-8")
        info["writable"] = True
        probe.unlink(missing_ok=True)
        info["result"] = "writable"
        print(f"✅ 可写（{info['context']}）：{TARGET}")
        rc = 0
    except OSError as e:
        info["writable"] = False
        info["write_error"] = f"{type(e).__name__}: {e}"
        info["result"] = "blocked"
        print(f"🔴 写入被拒（{info['context']}）：{type(e).__name__}: {e}")
        print(f"   需给 {sys.executable} 授「完全磁盘访问权限」"
              f"（系统设置 → 隐私与安全性 → 完全磁盘访问权限）")
        rc = 1
    _write(info)
    return rc


def _write(info: dict) -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        tmp = STAMP.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, STAMP)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
