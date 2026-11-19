#!/usr/bin/env python3
"""异机副本 fail-closed 回归（2026-11-19，Brian「要」）。

背景：29G 备份此前全在同一台 Mac 同一块盘，盘坏则源与备份一起没。
复核原话：「若没有异机或离线副本，优先补可恢复副本，而不是补监控」。

锁住：**核验失败绝不写成功戳**（防假绿），且不以退出码论成败。
只替身 transfer() 与 sh()，**不触真实远端、不动 subprocess 全局**
（第一版全局替换 subprocess.run 把本地 tar 也假掉了，测试自己崩了）。

运行：python3 tests/test_offsite_backup.py
"""
import hashlib
import importlib.util
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
spec = importlib.util.spec_from_file_location("ob", SRC / "offsite_backup.py")
ob = importlib.util.module_from_spec(spec)
sys.modules["ob"] = ob
spec.loader.exec_module(ob)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


ORIG_SH, ORIG_TRANSFER = ob.sh, ob.transfer
TMP = pathlib.Path(tempfile.mkdtemp(prefix="offsite-test-"))
ob.STAMP = TMP / ".offsite-stamp.json"

print("=== 配置与优先级 ===")
text = (SRC / "offsite_backup.py").read_text()
check("主机来自环境变量", "MEM_OFFSITE_SSH_HOST" in text)
check("源码无真实主机（公开仓）", "root@" not in text)
names = [n for n, _ in ob.SOURCES]
check("含 L1 事实源", "memory" in names)
check("含 MEMORY.md", "memory_md" in names)
check("含账本 data", "data" in names)
check("默认不传可重建的 L2", "plugmem" not in names)

# 造一个最小源，让打包/解包真实发生（不假掉本地 tar）
fake = TMP / "memory"
fake.mkdir(parents=True, exist_ok=True)
(fake / "2026-11-19.md").write_text("- 2026-11-19T10:00:00+08:00 事实\n")
(fake / "2026-11-18.md").write_text("- 2026-11-18T10:00:00+08:00 事实\n")
ob.SOURCES = [("memory", fake)]

print("=== 负控：未配置主机 → 退出 2 且不写戳 ===")
ob.HOST = ""
ob.STAMP.unlink(missing_ok=True)
check("退出码 2", ob.main() == 2)
check("无成功戳", not ob.STAMP.exists())

ob.HOST = "fake@host"
ob.transfer = lambda a, r: (0, "")          # 传输假装成功，核验由 sh 决定


def sh_with(sha=None, n=None, mkdir_rc=0):
    """sha=None 表示回真实本地 sha（让 sha 校验通过）；n 为远端解包计数。"""
    def _sh(cmd, timeout=1800):
        j = " ".join(str(c) for c in cmd)
        if "mkdir -p" in j:
            return mkdir_rc, "", "refused" if mkdir_rc else ""
        if "ssh" in j and "sha256sum" in j:
            return 0, (sha if sha is not None else _local_sha()) + "\n", ""
        if "ssh" in j and "tarfile" in j:
            return 0, f"{n if n is not None else 2}\n", ""
        if "ls -1d" in j:
            return 0, "", ""
        return ORIG_SH(cmd, timeout)
    return _sh


_LAST = {}


def _local_sha():
    return _LAST.get("sha", "0" * 64)


# 记录本地归档真实 sha：包一层 transfer
def transfer_spy(archive, remote_path):
    _LAST["sha"] = hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest()
    return 0, ""


ob.transfer = transfer_spy

print("=== 负控：远端建目录失败 → 不写戳 ===")
ob.sh = sh_with(mkdir_rc=1)
ob.STAMP.unlink(missing_ok=True)
check("退出码 1", ob.main() == 1)
check("无成功戳", not ob.STAMP.exists())

print("=== 负控：整档 sha 不一致 → 不写戳 ===")
ob.sh = sh_with(sha="0" * 64, n=2)
ob.STAMP.unlink(missing_ok=True)
check("退出码 1", ob.main() == 1)
check("无成功戳", not ob.STAMP.exists())

print("=== 负控：远端解包成员数不符 → 不写戳 ===")
ob.sh = sh_with(sha=None, n=999)
ob.STAMP.unlink(missing_ok=True)
check("退出码 1", ob.main() == 1)
check("无成功戳", not ob.STAMP.exists())

print("=== 负控：远端解包失败（返回 -1）→ 不写戳 ===")
def sh_extract_fail(cmd, timeout=1800):
    j = " ".join(str(c) for c in cmd)
    if "ssh" in j and "tarfile" in j:
        return 1, "", "extract error"
    return sh_with()(cmd, timeout)
ob.sh = sh_extract_fail
ob.STAMP.unlink(missing_ok=True)
check("退出码 1", ob.main() == 1)
check("无成功戳", not ob.STAMP.exists())

print("=== 负控：传输失败 → 不写戳 ===")
ob.sh = sh_with()
ob.transfer = lambda a, r: (1, "broken pipe")
ob.STAMP.unlink(missing_ok=True)
check("退出码 1", ob.main() == 1)
check("无成功戳", not ob.STAMP.exists())

print("=== 正向：全部核验通过才写戳 ===")
ob.transfer = transfer_spy
ob.sh = sh_with(sha=None, n=2)
ob.STAMP.unlink(missing_ok=True)
rc = ob.main()
check("退出码 0", rc == 0, f"实得 {rc}")
check("成功戳已写", ob.STAMP.exists())
if ob.STAMP.exists():
    import json
    d = json.loads(ob.STAMP.read_text())
    check("戳里记录 sha 一致", d["verified"]["archive_sha256"]["ok"] is True)
    check("戳里成员数两端相等",
          d["verified"]["members"]["local"] == d["verified"]["members"]["remote"] == 2,
          f"{d['verified']['members']}")

ob.sh, ob.transfer = ORIG_SH, ORIG_TRANSFER
print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
