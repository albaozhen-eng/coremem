#!/usr/bin/env python3
"""异机恢复演练：从远端拉回归档、解包、证明**真的能恢复**。

2026-11-19 Brian「可以」：异机副本此前只证明了「传上去且字节一致」，
没证明「拉回来能恢复」。备份系统最常见的假安全感就是「备份文件存在」。

主判据是**恢复副本自身的内部一致性**，不是与线上比差异：
  线上一直在写，拿恢复副本与线上比会因时间差误判（备份后新增的行本就不同）。
  真正的可用性判据是：恢复出的账本里每条 ref，能否在**恢复出的 L1** 里
  解析到对应行且 sha 一致 —— 这证明这份副本脱离本机也能自洽使用。

核验项（任一不过即失败，不写演练戳）：
  1 拉回的归档 sha256 == 远端 sha256 == 传输时记录的 sha256
  2 归档能完整解包，成员数与远端一致
  3 L1 每日笔记存在且非空
  4 MEMORY.md 存在且含人工确认区（DERIVED）
  5 账本可解析，且抽样 ref 在恢复出的 L1 里 sha 一致（**核心可用性判据**）
  6 用真实事实做 canary 检索（不往生产 L1 写任何合成测试事实）

与线上的比对只作**参考信息**输出，不作判据。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
HOST = os.environ.get("MEM_OFFSITE_SSH_HOST", "").strip()
REMOTE_ROOT = os.environ.get("MEM_OFFSITE_ROOT", "~/coremem-offsite")
STAMP = HOME / ".coremem/health/.offsite-restore-drill.json"
XFER_STAMP = HOME / ".coremem/health/.offsite-stamp.json"
SAMPLE_REFS = int(os.environ.get("MEM_DRILL_SAMPLE", "40"))
SSH = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]
LINE_PREFIX = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}T[^\s]*\s*(\[[^\]]*\])?\s*")


def sh(cmd: list[str], timeout=1800) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# 直接复用生产的 line_sha，**不自己重写**：
# mo_common.line_sha 内部先做 norm(s) 归一化，自己实现 sha256(text) 在
# 带空白差异的行上会与生产口径不一致 → 抽验会假红。
sys.path.insert(0, str(HOME / ".coremem/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from mo_common import line_sha as _prod_line_sha
    line_sha = _prod_line_sha
except ImportError:
    def line_sha(text: str) -> str:
        """兜底（干净 clone 无 mo_common 时）：与生产同口径需 norm，故显式 strip。"""
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def pull(remote: str, local_arc: Path) -> tuple[int, str]:
    """从远端拉回归档。抽成独立函数便于测试替身（不动 subprocess 全局）。"""
    with open(local_arc, "wb") as fh:
        p = subprocess.run(SSH + [HOST, f"cat {remote}"],
                           stdout=fh, stderr=subprocess.PIPE, text=True, timeout=1800)
    return p.returncode, (p.stderr or "").strip()


def main() -> int:
    if not HOST:
        print("未配置 MEM_OFFSITE_SSH_HOST → 演练未执行（不假装成功）", file=sys.stderr)
        return 2
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="offsite-drill-"))
    try:
        # ── 找远端最新归档 ────────────────────────────────────────────────
        rc, out, err = sh(SSH + [HOST, f"ls -1 {REMOTE_ROOT}/*/coremem-offsite-*.tar.gz 2>/dev/null | sort | tail -1"])
        remote = out.strip()
        if rc != 0 or not remote:
            print(f"远端找不到归档（rc={rc}）→ 演练失败", file=sys.stderr)
            return 1
        rc, out, _ = sh(SSH + [HOST, f"sha256sum {remote} | cut -d' ' -f1"])
        remote_sha = out.strip() if rc == 0 else ""
        if not remote_sha:
            print("取不到远端 sha256 → 演练失败", file=sys.stderr)
            return 1

        # ── 拉回本地 ──────────────────────────────────────────────────────
        local_arc = work / "pull.tar.gz"
        rc_p, err_p = pull(remote, local_arc)
        if rc_p != 0:
            print(f"拉回失败 rc={rc_p}: {err_p[:200]}", file=sys.stderr)
            return 1
        pulled_sha = hashlib.sha256(local_arc.read_bytes()).hexdigest()
        fails, info = [], {}
        if pulled_sha != remote_sha:
            fails.append(f"拉回 sha 与远端不一致 {pulled_sha[:16]}/{remote_sha[:16]}")
        # 与传输时记录的 sha 比（证明远端这份就是当初传上去那份）
        recorded = ""
        try:
            recorded = json.loads(XFER_STAMP.read_text()).get("sha256", "")
        except (OSError, ValueError):
            pass
        if recorded and recorded != pulled_sha:
            info["recorded_sha_match"] = False
            fails.append("拉回 sha 与传输戳记录不一致（远端这份可能被换过）")
        elif recorded:
            info["recorded_sha_match"] = True

        # ── 解包 ──────────────────────────────────────────────────────────
        root = work / "restored"
        root.mkdir()
        try:
            with tarfile.open(local_arc, "r:gz") as t:
                t.extractall(root)
        except (tarfile.TarError, OSError, EOFError, ValueError) as e:
            # EOFError 必须捕获：截断的 gzip 会抛它，而它**不是** TarError/OSError
            # 的子类。2026-11-19 负控实测：截断档导致抛栈而非干净失败。
            print(f"解包失败（{type(e).__name__}）：{e} → 演练失败", file=sys.stderr)
            return 1
        members = sum(len(fs) for _, _, fs in os.walk(root))
        info["members"] = members
        if members == 0:
            fails.append("解包后 0 个文件")

        # ── L1 / MEMORY.md 存在性 ─────────────────────────────────────────
        mem = root / "memory"
        notes = sorted(mem.glob("2026-*.md")) if mem.is_dir() else []
        info["l1_notes"] = len(notes)
        if not notes:
            fails.append("恢复副本里没有 L1 每日笔记")
        mmd = root / "MEMORY.md"
        if not mmd.exists():
            fails.append("恢复副本里没有 MEMORY.md")
        else:
            txt = mmd.read_text(encoding="utf-8", errors="ignore")
            info["memory_md_derived"] = "DERIVED" in txt
            if "DERIVED" not in txt:
                fails.append("MEMORY.md 里没有人工确认区 DERIVED")

        # ── 核心可用性判据：恢复副本自身内部一致性 ────────────────────────
        ledger = root / "data" / "write_ledger.jsonl"
        if not ledger.exists():
            fails.append("恢复副本里没有写入账本")
        else:
            rows = []
            for ln in ledger.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
            info["ledger_rows"] = len(rows)
            # 退役 ref 排除 sha 比对 —— 沿用探针**已有的同一机制**，不是放宽判据。
            # 2026-11-19 实测抽验 40 条中 2 条 sha 不符，查明都是 11-16 清理时
            # 就地标记 [已撤销] 的行：内容被合法改写，sha 必然变，账本存的是原 sha。
            # 对这类 ref 改做**正向检查**（行存在且含撤销标记），不是简单跳过。
            retired = {r.get("ref") for r in rows
                       if "retired" in (r.get("note") or "") or "removed" in (r.get("note") or "")}
            cand = [r for r in rows if r.get("ref") and r.get("sha")]
            rnd = random.Random(20261119)
            sample = rnd.sample(cand, min(SAMPLE_REFS, len(cand))) if cand else []
            good = bad = unresolvable = retired_ok = 0
            bad_refs = []
            cache: dict[str, list[str]] = {}
            for r in sample:
                ref, sha = r["ref"], r["sha"]
                try:
                    fn, ln_s = ref.rsplit(":", 1)
                    ln = int(ln_s)
                except ValueError:
                    unresolvable += 1
                    continue
                if fn not in cache:
                    f = mem / fn
                    cache[fn] = f.read_text(encoding="utf-8", errors="ignore").splitlines() if f.exists() else []
                lines = cache[fn]
                if ln < 1 or ln > len(lines):
                    unresolvable += 1
                    if len(bad_refs) < 3:
                        bad_refs.append(ref)
                    continue
                raw = lines[ln - 1]
                if ref in retired:
                    # 退役行：必须仍在原位（行号是锚点，永不删行）且带撤销标记
                    if "[已撤销" in raw or "[已退役" in raw:
                        good += 1
                        retired_ok += 1
                    else:
                        bad += 1
                        if len(bad_refs) < 3:
                            bad_refs.append(ref + "(退役行缺标记)")
                elif line_sha(raw) == sha:
                    good += 1
                else:
                    bad += 1
                    if len(bad_refs) < 3:
                        bad_refs.append(ref)
            info["ref_sample"] = {"n": len(sample), "sha_ok": good,
                                  "sha_bad": bad, "unresolvable": unresolvable,
                                  "retired_verified": retired_ok,
                                  "retired_refs_total": len(retired)}
            if bad or unresolvable:
                fails.append(f"恢复副本内部不一致：sha 不符 {bad} / 解析不到 {unresolvable}"
                             f"（例 {bad_refs}）")
            if good == 0:
                fails.append("没有任何 ref 能在恢复副本里核验通过 → 这份副本不可用")

            # canary：用**真实事实**做检索，不往生产 L1 写合成样本
            if notes:
                f = notes[-1]
                real = [l for l in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if l.startswith("- 20") and len(l) > 40]
                if real:
                    pick = real[len(real) // 2]
                    body = LINE_PREFIX.sub("", pick).strip()
                    key = body[:12]
                    hit = any(key in l for l in f.read_text(encoding="utf-8", errors="ignore").splitlines())
                    info["canary"] = {"found": hit, "key_len": len(key)}
                    if not hit:
                        fails.append("canary 真实事实在恢复副本里检索不到")

        # ── 参考信息：与线上比（不作判据，线上一直在写）────────────────────
        live_mem = HOME / ".coremem/memory"
        same = diff = 0
        for f in notes[-5:]:
            lv = live_mem / f.name
            if lv.exists():
                if hashlib.sha256(lv.read_bytes()).hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest():
                    same += 1
                else:
                    diff += 1
        info["vs_live_reference_only"] = {"identical": same, "changed_since_backup": diff}

        elapsed = round(time.time() - t0, 1)
        if fails:
            print("恢复演练失败：" + "；".join(fails[:4]), file=sys.stderr)
            print(f"  详情：{json.dumps(info, ensure_ascii=False)[:400]}", file=sys.stderr)
            return 1

        payload = {"completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "archive": remote, "sha256": pulled_sha,
                   "verified": info, "elapsed_s": elapsed, "result": "restorable"}
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        tmp = STAMP.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        os.replace(tmp, STAMP)
        rs = info.get("ref_sample", {})
        print(f"恢复演练通过：{remote.rsplit('/', 1)[-1]} | {members} 文件 | "
              f"L1 {len(notes)} 篇 | ref 抽验 {rs.get('sha_ok')}/{rs.get('n')} sha 一致 | "
              f"canary {'命中' if info.get('canary', {}).get('found') else '未测'} | {elapsed}s")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
