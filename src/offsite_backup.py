#!/usr/bin/env python3
"""异机副本：把**不可再生**的数据打包传到远端主机并逐字节验证。

2026-11-19 Brian「要」：此前 29G 备份全在同一台 Mac 同一块盘，
盘坏则源与备份一起没。四份独立复核里 kimi 那条正好说中：
「若没有异机或离线副本，优先补可恢复副本，而不是补监控」。

优先级依据（实测体积）：
  L1 事实源 memory/      22M   **不可再生**
  MEMORY.md              36K   人工确认的 DERIVED 规则，不可再生
  data/（账本等）        13M   可解释性依据，不可再生
  config/                4K    含凭据 → 远端目录 700
  BOSS data/            203M   业务数据，不可再生
  plugmem/（L2）        1.5G   **可从 L1 重建** → 默认不传，省带宽
  代码                  —     已在 GitHub 私有仓，无需重复

主机从环境变量 MEM_OFFSITE_SSH_HOST 取（与 config-drift-check 同一台）。
**不硬编码主机**：coremem 是公开仓，secret_scan 会拦真实 IP/主机名。
未配置时明确跳过并非零退出，不静默假装成功。

传输方式：tar over ssh（**远端不需要 rsync**）。
2026-11-19 实测远端 Alibaba Cloud Linux 只有 tar/gzip/sha256sum/python3，
rsync 不存在（rc=127）。第一版用 rsync 全部失败，fail-closed 正确拒写成功戳。
改用 tar 流式传输，零额外依赖，且能整档 sha256 逐字节核验 ——
比「数文件个数」更强，也直接对上历史教训「tar exit 1 但归档完好」的反面：
**不以退出码论成败，以产物哈希与可列出内容论成败**。

fail-closed：只有传输成功**且远端 sha256 与本地一致、归档可完整列出**才写成功戳。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HOST = os.environ.get("MEM_OFFSITE_SSH_HOST", "").strip()
REMOTE_ROOT = os.environ.get("MEM_OFFSITE_ROOT", "~/coremem-offsite")
KEEP = max(1, int(os.environ.get("MEM_OFFSITE_KEEP", "7")))
INCLUDE_L2 = os.environ.get("MEM_OFFSITE_INCLUDE_L2", "0") == "1"
STAMP = HOME / ".coremem/health/.offsite-stamp.json"

SOURCES = [
    ("memory", HOME / ".coremem/memory"),
    ("memory_md", HOME / ".coremem/MEMORY.md"),
    ("data", HOME / ".coremem/data"),
    ("config", HOME / ".coremem/config"),
    ("boss_data", HOME / ".boss-agent/data"),
]
if INCLUDE_L2:
    SOURCES.append(("plugmem", HOME / ".coremem/plugmem"))

SSH = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]


def sh(cmd: list[str], timeout=1800) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def transfer(archive: Path, remote_path: str) -> tuple[int, str]:
    """流式传输归档到远端（远端只需 cat）。抽成独立函数便于测试替身。"""
    with open(archive, "rb") as fh:
        p = subprocess.run(SSH + [HOST, f"cat > {remote_path}"],
                           stdin=fh, capture_output=True, text=True, timeout=1800)
    return p.returncode, (p.stderr or "").strip()


def sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    if not HOST:
        print("未配置 MEM_OFFSITE_SSH_HOST → 异机副本未执行（不假装成功）", file=sys.stderr)
        return 2
    stamp_day = datetime.now().strftime("%Y-%m-%d")
    dest = f"{REMOTE_ROOT}/{stamp_day}"
    t0 = time.time()

    rc, out, err = sh(SSH + [HOST, f"mkdir -p {dest} && chmod 700 {REMOTE_ROOT} {dest}"])
    if rc != 0:
        print(f"远端建目录失败 rc={rc}: {err.strip()[:200]}", file=sys.stderr)
        return 1

    # ── 本地打包（只装不可再生的源）────────────────────────────────────
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="offsite-"))
    archive = tmpdir / f"coremem-offsite-{stamp_day}.tar.gz"
    names, skipped, member_files = [], [], 0
    for name, src in SOURCES:
        if not src.exists():
            skipped.append(name)
            continue
        names.append((name, src))
        member_files += sum(1 for p in src.rglob("*") if p.is_file()) if src.is_dir() else 1
    if not names:
        print("没有任何源可传，不写成功戳", file=sys.stderr)
        return 1
    # 用 -C 逐个加入，顶层保持源的**真实 basename**（如 memory_md 逻辑名对应
    # MEMORY.md 文件），恢复时按真实名定位，不做易错的名称映射。
    # --no-xattrs：macOS bsdtar 默认写 LIBARCHIVE.xattr.* 扩展头，
    # 远端 GNU tar 会刷 "Ignoring unknown extended header keyword" 噪声。
    cmd = ["tar", "--no-xattrs", "-czf", str(archive)]
    for name, src in names:
        cmd += ["-C", str(src.parent), src.name]
    rc, out, err = sh(cmd)
    # tar 对「文件在读取时变化」会 rc=1，但归档可能完好 —— 不据退出码判死，
    # 改为验证归档能否完整列出（历史教训 B 的正确处置）。
    rc_list, out_list, _ = sh(["tar", "-tzf", str(archive)])
    # 本地判据用**源文件数**，不用 tar 列表行数：
    # 2026-11-19 实测 macOS bsdtar 与远端 GNU tar 列表口径不同（3266 vs 3274），
    # 而整档 sha256 完全一致 —— 是判据不可靠，不是数据丢失。
    # 本地也用**解包后 os.walk 计数**，与远端同一方法。
    # 不能用打包前的源文件数：记忆系统在活跃写入，实测计数与打包之间
    # 又新增了 8 个文件（3266 vs 3274），那是时序差不是数据丢失。
    # 两端同法解包计数才是可比的判据，同时也验证了本地归档可解开。
    import tarfile as _tf
    _ld = Path(tempfile.mkdtemp(prefix="offsite-verify-"))
    try:
        with _tf.open(archive, "r:gz") as _t:
            _t.extractall(_ld)
        local_members = sum(len(fs) for _, _, fs in os.walk(_ld))
    finally:
        shutil.rmtree(_ld, ignore_errors=True)
    if rc_list != 0 or local_members == 0:
        print(f"本地归档不可列出（rc={rc_list}）→ 不传输、不写成功戳", file=sys.stderr)
        return 1
    local_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    size_mb = archive.stat().st_size / 1024**2

    # ── 流式传输（远端只需 cat）──────────────────────────────────────────
    remote_path = f"{dest}/coremem-offsite-{stamp_day}.tar.gz"
    rc_t, err_t = transfer(archive, remote_path)
    if rc_t != 0:
        print(f"传输失败 rc={rc_t}: {err_t[:200]}", file=sys.stderr)
        return 1

    # ── 正向核验：整档 sha256 + 远端可列出成员数 ─────────────────────────
    rc, out, _ = sh(SSH + [HOST, f"sha256sum {remote_path} | cut -d' ' -f1"])
    remote_sha = out.strip() if rc == 0 else ""
    # 远端**真解包**再数文件 —— 这才是「以能否恢复为准」，
    # 且与 tar 实现的列表口径无关。用远端 python3（实测存在）解包到临时目录、
    # 数完用 shutil 清理，避免 shell 高危删除模式。
    _rp = (
        "import tarfile,tempfile,shutil,os,sys;"
        "d=tempfile.mkdtemp();"
        # 路径里的 ~ 在 Python 字符串内**不会被展开**（shell 只在引号外展开），
        # 必须显式 expanduser，否则 tarfile.open 报文件不存在。
        f"t=tarfile.open(os.path.expanduser('{remote_path}'),'r:gz');"
        "t.extractall(d);t.close();"
        "n=sum(len([f for f in fs]) for _,_,fs in os.walk(d));"
        "shutil.rmtree(d);print(n)"
    )
    rc2, out2, err2 = sh(SSH + [HOST, f"python3 -c \"{_rp}\""])
    remote_members = int(out2.strip().splitlines()[-1]) if rc2 == 0 and out2.strip() else -1
    bad = []
    if remote_sha != local_sha:
        bad.append(f"整档 sha 不一致 本地{local_sha[:16]}/远端{remote_sha[:16] or '取不到'}")
    if remote_members != local_members:
        bad.append(f"成员数 本地{local_members}/远端{remote_members}")
    if bad:
        print("远端核验未通过，不写成功戳：" + "；".join(bad), file=sys.stderr)
        return 1

    sent = {name: {"source": str(src), "ok": True} for name, src in names}
    checks = {
        "archive_sha256": {"local": local_sha, "remote": remote_sha, "ok": True},
        "members": {"local": local_members, "remote": remote_members, "ok": True,
                    "method": "远端真解包后 os.walk 计数"},
        "size_mb": round(size_mb, 1),
        "tar_rc": rc,  # 记录但不作判据
    }
    archive.unlink(missing_ok=True)
    tmpdir.rmdir()

    # ── 远端保留 KEEP 份（异机是最后防线，份数默认比本地多）────────────
    rc, out, _ = sh(SSH + [HOST, f"ls -1d {REMOTE_ROOT}/20* 2>/dev/null | sort"])
    dirs = [d for d in out.splitlines() if d.strip()]
    removed = []
    if rc == 0 and len(dirs) > KEEP:
        for d in dirs[:-KEEP]:
            sh(SSH + [HOST, f"rm -rf -- '{d}'"])
            removed.append(d.rsplit("/", 1)[-1])

    payload = {
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dest": dest, "sources": sent, "skipped": skipped,
        "verified": checks, "elapsed_s": round(time.time() - t0, 1),
        "archive": remote_path, "sha256": local_sha,
        "remote_keep": KEEP, "pruned": removed,
    }
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    tmp = STAMP.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, STAMP)

    total = local_members
    print(f"异机副本完成：{remote_path} | {len(sent)} 个源 / {total} 个成员 / {size_mb:.1f} MB | "
          f"{payload['elapsed_s']}s | 远端保留 {KEEP} 份"
          + (f" | 清理 {','.join(removed)}" if removed else "")
          + (f" | 跳过 {','.join(skipped)}" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
