#!/usr/bin/env python3
"""异机恢复演练回归（2026-11-19，Brian「可以」）。

异机副本此前只证明「传上去且字节一致」，没证明「拉回来能恢复」。
备份系统最常见的假安全感就是「备份文件存在」。

主判据是**恢复副本自身内部一致性**（账本 ref 在恢复出的 L1 里 sha 一致），
不与线上比差异 —— 线上一直在写，比差异会因时间差误判。

本测试用本地 fixture 假装远端，**不触真实主机**。锁住：
  截断归档 / 无归档 / 内部不一致 / 缺 MEMORY.md → 一律失败且不写演练戳
  退役行（就地标记 [已撤销]）sha 必然变 → 走正向检查而非误判失败
  EOFError 必须被捕获（截断 gzip 抛它，不是 TarError 子类）

运行：python3 tests/test_offsite_restore_drill.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import tarfile
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
spec = importlib.util.spec_from_file_location("od", SRC / "offsite_restore_drill.py")
od = importlib.util.module_from_spec(spec)
sys.modules["od"] = od
spec.loader.exec_module(od)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("=== 判据设计 ===")
text = (SRC / "offsite_restore_drill.py").read_text()
check("复用生产 line_sha（不自己重写归一化）", "from mo_common import line_sha" in text)
check("捕获 EOFError（截断 gzip）", "EOFError" in text)
check("退役行走正向检查", "retired_verified" in text)
check("与线上比只作参考", "vs_live_reference_only" in text)
check("源码无真实主机", "root@" not in text)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="drill-test-"))
od.STAMP = TMP / ".drill-stamp.json"
od.XFER_STAMP = TMP / ".xfer-stamp.json"
od.HOST = "fake@host"


def build_fixture(*, corrupt=False, drop_mmd=False, break_sha=False, retired=False):
    """造一个最小可恢复副本并打成 tar.gz，返回归档路径。"""
    root = TMP / f"fx-{abs(hash((corrupt, drop_mmd, break_sha, retired)))}"
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    line = "- 2026-11-19T10:00:00+08:00 [自动捕获] 这是一条用于演练的真实事实内容样本"
    if retired:
        line = "- 2026-11-19T10:00:00+08:00 [自动捕获] [已撤销] 退化重复输出，非事实"
    (root / "memory" / "2026-11-19.md").write_text(line + "\n")
    if not drop_mmd:
        (root / "MEMORY.md").write_text("# 记忆\n## DERIVED\n- 规则一\n")
    sha = od.line_sha(line)
    if break_sha:
        sha = "0" * 16
    row = {"ts": "2026-11-19T10:00:00+08:00", "ref": "2026-11-19.md:1",
           "sha": sha, "l2_status": "ok"}
    if retired:
        row["note"] = "retired: 就地标记撤销"
        row["sha"] = "f" * 16          # 原 sha，与标记后不同 → 必须走正向检查
    (root / "data" / "write_ledger.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    arc = root.with_suffix(".tar.gz")
    with tarfile.open(arc, "w:gz") as t:
        for p in root.rglob("*"):
            t.add(p, arcname=str(p.relative_to(root)))
    if corrupt:
        arc.write_bytes(arc.read_bytes()[:300])
    return arc


ORIG_SH, ORIG_PULL = od.sh, od.pull


def install(arc: pathlib.Path | None):
    """让 sh 报出归档路径与 sha；pull 直接把 fixture 拷到目标。"""
    def _sh(cmd, timeout=1800):
        j = " ".join(str(c) for c in cmd)
        if "ls -1" in j and "tar.gz" in j:
            return (0, str(arc) + "\n", "") if arc else (0, "", "")
        if "sha256sum" in j:
            return (0, hashlib.sha256(arc.read_bytes()).hexdigest() + "\n", "") if arc else (1, "", "")
        return ORIG_SH(cmd, timeout)

    def _pull(remote, local_arc):
        if arc is None:
            return 1, "no archive"
        pathlib.Path(local_arc).write_bytes(arc.read_bytes())
        return 0, ""
    od.sh, od.pull = _sh, _pull


print("=== 正向：完整副本 → 通过并写戳 ===")
install(build_fixture())
od.STAMP.unlink(missing_ok=True)
od.XFER_STAMP.unlink(missing_ok=True)
rc = od.main()
check("退出码 0", rc == 0, f"实得 {rc}")
check("演练戳已写", od.STAMP.exists())
if od.STAMP.exists():
    d = json.loads(od.STAMP.read_text())
    check("结论 restorable", d.get("result") == "restorable")
    check("ref 抽验通过", d["verified"]["ref_sample"]["sha_ok"] >= 1)

print("=== 负控：截断归档 → 失败且不写戳 ===")
install(build_fixture(corrupt=True))
od.STAMP.unlink(missing_ok=True)
check("退出码 1", od.main() == 1)
check("无演练戳", not od.STAMP.exists())

print("=== 负控：无归档 → 失败且不写戳 ===")
install(None)
od.STAMP.unlink(missing_ok=True)
check("退出码 1", od.main() == 1)
check("无演练戳", not od.STAMP.exists())

print("=== 负控：内部 sha 不一致 → 失败（副本不可用）===")
install(build_fixture(break_sha=True))
od.STAMP.unlink(missing_ok=True)
check("退出码 1", od.main() == 1)
check("无演练戳", not od.STAMP.exists())

print("=== 负控：缺 MEMORY.md → 失败 ===")
install(build_fixture(drop_mmd=True))
od.STAMP.unlink(missing_ok=True)
check("退出码 1", od.main() == 1)
check("无演练戳", not od.STAMP.exists())

print("=== 退役行：sha 变了但带撤销标记 → 应通过（不误判）===")
install(build_fixture(retired=True))
od.STAMP.unlink(missing_ok=True)
rc = od.main()
check("退出码 0", rc == 0, f"实得 {rc}")
if od.STAMP.exists():
    d = json.loads(od.STAMP.read_text())
    check("计入 retired_verified", d["verified"]["ref_sample"]["retired_verified"] >= 1,
          f"{d['verified']['ref_sample']}")

print("=== 负控：未配置主机 → 退出 2 且不写戳 ===")
od.sh, od.pull = ORIG_SH, ORIG_PULL
od.HOST = ""
od.STAMP.unlink(missing_ok=True)
check("退出码 2", od.main() == 2)
check("无演练戳", not od.STAMP.exists())

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
