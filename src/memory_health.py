#!/usr/bin/env python3
from __future__ import annotations

"""记忆系统全局心跳（W1）
单一探针：只读，不改任何现有链路。
输出 ~/.coremem/health/memory-health.json + 一行人类可读摘要。
退出码：0=全绿/黄，2=有红灯。
"""
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mo_common  # noqa: E402  （P0 新增）

HOME = Path.home()
SCRIPTS_DIR = HOME / ".coremem" / "scripts"
GOLDEN_DIR = mo_common.GOLDEN_DIR  # 与 mo_common 共用路径（支持环境变量覆盖，便于测试）
OUT_DIR = Path(os.environ.get("MEM_CHECKUP_HEALTH_DIR", HOME / ".coremem" / "health"))
OUT = OUT_DIR / "memory-health.json"

CAPTURE_LOG = HOME / ".coremem/logs/memory_capture_30m.log"
MEMORY_MD = HOME / ".coremem/MEMORY.md"
BUFFER = HOME / ".coremem/data/memory_buffer.jsonl"
PLUGMEM_ERR = HOME / ".plugmem/logs/service.err.log"
PLUGMEM_DATA = HOME / ".coremem/plugmem"
MEMORY_DIR = mo_common.MEMORY_DIR  # 统一路径源
EVAL_DIR = MEMORY_DIR

NOW = time.time()


def age_h(path: Path) -> float | None:
    try:
        return (NOW - path.stat().st_mtime) / 3600
    except OSError:
        return None


def check(cid, label, age, warn, red, detail="", unit="h"):
    level = "green"
    if age is not None and age >= red:
        level = "red"
    elif age is not None and age >= warn:
        level = "yellow"
    if age is None:
        level, age = "red", None
    return {
        "id": cid,
        "label": label,
        "level": level,
        "value": None if age is None else round(age, 1),
        "unit": unit,
        "warn_at": warn,
        "red_at": red,
        "detail": detail,
    }


def probe_capture():
    a = age_h(CAPTURE_LOG)
    return check("capture", "捕获心跳（30min job）", a, 1.5, 6, "memory_capture_30m.log 的 mtime")


def probe_l1():
    """L1 是事实 SoT：今天或昨天的笔记必须存在且在更新。"""
    today = datetime.now().astimezone()
    cands = [MEMORY_DIR / f"{today:%Y-%m-%d}.md",
             MEMORY_DIR / f"{(today - timedelta(days=1)):%Y-%m-%d}.md"]
    for p in cands:
        if p.exists():
            return check("l1", "L1 每日笔记（SoT）", age_h(p), 26, 50, p.name, unit="h")
    return check("l1", "L1 每日笔记（SoT）", None, 26, 50, "今天和昨天都没有笔记")


def probe_auto_retired():
    """AUTO 区块已于 2026-10-19 退役；若有人把它写回来，这里会变黄。"""
    try:
        text = MEMORY_MD.read_text(errors="ignore")
    except OSError as e:
        return check("auto", "AUTO 退役状态", None, 1, 2, f"读不到：{e}")
    has_stamp = bool(re.search(r"自动同步：\d{4}-\d{2}-\d{2}T", text))
    level = "yellow" if has_stamp else "green"
    return {"id": "auto", "label": "AUTO 退役状态", "level": level,
            "value": 1 if has_stamp else 0, "unit": "处",
            "warn_at": 1, "red_at": None,
            "detail": "检测到自动同步戳，AUTO 可能被写回" if has_stamp else "已退役，仅保留标记位"}


def probe_buffer():
    """P3：缓冲区积压。

    原先只看条数（warn 6 / red 30）—— 于是「1 条卡了 7 天」永远是绿的。
    现在同时看**最老条目的年龄**：正常情况 30min 内就该被重试掉。
    """
    lines = []
    try:
        lines = [l for l in BUFFER.read_text(errors="ignore").splitlines() if l.strip()]
    except OSError:
        lines = []
    n = len(lines)

    oldest_h = None
    for l in lines:
        try:
            ts = json.loads(l).get("timestamp")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            age = (datetime.now().astimezone() - dt).total_seconds() / 3600
            oldest_h = age if oldest_h is None else max(oldest_h, age)
        except (ValueError, TypeError):
            continue
    oldest_h = round(oldest_h, 1) if oldest_h is not None else None

    level = "green"
    if oldest_h is not None:
        if oldest_h >= 72:
            level = "red"
        elif oldest_h >= 24:
            level = "yellow"
    if n >= 30:
        level = "red"
    elif n >= 6 and level == "green":
        level = "yellow"

    detail = f"{n} 条积压"
    if oldest_h is not None:
        detail += f" · 最老 {oldest_h} 小时"
    return {"id": "buffer", "label": "写入缓冲区积压", "level": level, "value": n,
            "unit": "条", "warn_at": 6, "red_at": 30, "detail": detail,
            "oldest_age_hours": oldest_h}


def probe_consolidation():
    """err log 无时间戳 → 用「自上次心跳以来的新增失败数」做增量探针。
    状态存在 ~/.coremem/health/.consolidation-count，避免重扫全量日志。"""
    state_file = OUT_DIR / ".consolidation-count"
    try:
        total = 0
        with PLUGMEM_ERR.open(errors="ignore") as f:
            for line in f:
                if "failed, skipped" in line:
                    total += 1
    except OSError:
        return check("consolidation", "PlugMem 巩固（新增跳过）", None, 0, 1, "读不到 err log")

    try:
        prev = int(state_file.read_text().strip())
    except (OSError, ValueError):
        prev = total  # 首次运行只记基线，不报红
    delta = total - prev if total >= prev else 0  # 日志轮转则归零
    state_file.write_text(str(total))

    level = "green" if delta == 0 else ("red" if delta > 20 else "yellow")
    return {
        "id": "consolidation",
        "label": "PlugMem 巩固（新增 merge 跳过）",
        "level": level,
        "value": delta,
        "unit": "次/15min",
        "warn_at": 1,
        "red_at": 20,
        "detail": f"累计 {total} 次",
    }


def probe_eval():
    """评测新鲜度：优先看 weekly golden 门禁（health/golden.json），退回旧 karpathy-eval 产物。"""
    gj = HOME / ".coremem/health/golden.json"
    try:
        d = json.loads(gj.read_text())
        ts = datetime.fromisoformat(d["generated_at"]).astimezone(timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        c = check("eval", "golden 门禁最近运行", age, 192, 336,
                  f"{d.get('current')}% vs 基线 {d.get('baseline')}%（门禁 {d.get('overall')}）", unit="h")
        # 复核指出：此前只看新鲜度，门禁判红也不会让这里变红 —— 门禁形同虚设。
        # 现在把整体级别一并考虑（红/黄都要冒出来）。
        _g = d.get("overall")
        if _g == "red":
            c["level"] = "red"
            c["detail"] += f"；门禁判定：{d.get('line')}"
        elif _g == "yellow" and c["level"] == "green":
            c["level"] = "yellow"
            c["detail"] += f"；门禁判定：{d.get('line')}"
        return c
    except Exception:
        pass
    files = [p for p in EVAL_DIR.glob("memory-karpathy-eval-*.md")] if EVAL_DIR.exists() else []
    if not files:
        return check("eval", "评测最近运行", None, 192, 336, "无任何评测产物")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return check("eval", "评测最近运行", age_h(latest), 192, 336, latest.name, unit="h")


def probe_backup():
    """L2 语义图快照：~/.coremem/backups/plugmem/<YYYY-MM-DD>/，优先读 snapshot.json"""
    root = HOME / ".coremem/backups/plugmem"
    snaps = [d for d in root.iterdir() if d.is_dir()] if root.exists() else []
    if not snaps:
        return check("backup", "L2 语义图快照", None, 48, 96, f"{root} 下无快照")

    def snap_time(d: Path):
        marker = d / "snapshot.json"
        try:
            return datetime.fromisoformat(json.loads(marker.read_text())["created_at"])
        except Exception:
            try:
                return datetime.strptime(d.name, "%Y-%m-%d").astimezone()
            except ValueError:
                return datetime.fromtimestamp(d.stat().st_mtime).astimezone()

    latest = max(snaps, key=snap_time)
    ts = snap_time(latest).astimezone(timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return check("backup", "L2 语义图快照", age, 48, 96,
                 f"最新 {latest.name}（共 {len(snaps)} 份）", unit="h")


def probe_plugmem():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8089/api/v1/health", timeout=5) as r:
            d = json.loads(r.read())
        ok = all(d.get(k) for k in ("llm_available", "embedding_available", "chroma_available"))
        return {"id": "plugmem", "label": "PlugMem 服务", "level": "green" if ok else "red",
                "value": None, "unit": "", "warn_at": None, "red_at": None,
                "detail": json.dumps(d, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        return {"id": "plugmem", "label": "PlugMem 服务", "level": "red",
                "value": None, "unit": "", "warn_at": None, "red_at": None, "detail": str(e)}



# ============ P0 新增探针 ============

def _recall_l1(query: str):
    """只取 L1 层的检索结果（canary 用）。失败返回 None。"""
    try:
        p = subprocess.run(["python3", str(SCRIPTS_DIR / "memory_recall.py"), "all", query, "--json"],
                           capture_output=True, text=True, timeout=90, cwd=str(SCRIPTS_DIR))
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return (json.loads(p.stdout).get("layers") or {}).get("l1") or []
    except Exception:  # noqa: BLE001
        return None


def _canary_queries(n=5):
    """从最新的每日笔记里确定性抽 n 行，用行内稀有词做 canary 查询。"""
    files = mo_common.daily_files()
    out = []
    for f in files[:3]:
        for i, line in enumerate(mo_common.read_lines(f), 1):
            t = line.strip()
            if len(t) < 24 or t.startswith("#"):
                continue
            words = [w for w in re.findall(r"[A-Za-z0-9_.\-]{4,}|[\u4e00-\u9fff]{3,}", t)
                     if not re.match(r"^\d+$", w)]
            if len(words) < 2:
                continue
            seed = int(hashlib.sha256(f"{f.name}:{i}".encode()).hexdigest()[:8], 16)
            words.sort(key=lambda w: (-len(w), w))
            picked = words[:2]
            out.append({"ref": f"{f.name}:{i}", "sha": mo_common.line_sha(t),
                        "text": t, "query": " ".join(picked), "seed": seed})
        if len(out) >= n:
            break
    out.sort(key=lambda x: x["seed"])
    return out[:n]


def probe_provenance():
    """P0-M2：L1 层的行号回指完整性（canary 查询）。"""
    canaries = _canary_queries(5)
    if not canaries:
        return mo_common.check("provenance", "L1 行号回指（canary）", "green", None, "",
                               "无可用每日笔记，跳过", record_only=True)
    returned = with_ref = resolvable = verbatim = canary_hit = 0
    probe_error = None
    for c in canaries:
        items = _recall_l1(c["query"])
        if items is None:
            probe_error = "memory_recall 调用失败"
            break
        for r in items:
            returned += 1
            ref = r.get("file") or ""
            if not mo_common.parse_ref(ref):
                continue
            with_ref += 1
            src = mo_common.resolve(ref)
            if src is None:
                continue
            resolvable += 1
            got = r.get("content") or ""
            if mo_common.line_sha(src) == mo_common.line_sha(got):
                verbatim += 1
            elif got.rstrip().endswith("…"):
                # 2026-11-11 审计：检索层对长行截 600 字加 … 是预期行为，
                # 原探针拿截断文本比全文 sha → 长行永远「不一致」（78.9% 假警报）。
                # 改验截断部分与源行前缀逐字一致。
                prefix = mo_common.norm(got.rstrip()[:-1])
                if prefix and mo_common.norm(src).startswith(prefix):
                    verbatim += 1
        for r in items:
            if mo_common.parse_ref(r.get("file") or "") and \
               mo_common.line_sha(mo_common.resolve(r.get("file")) or "") == c["sha"]:
                canary_hit += 1
                break
    if probe_error:
        # 复核指出：最常见的故障（检索崩/超时）此前给 record_only 绿灯 = 静默失效。
        return {"id": "provenance", "label": "L1 行号回指（canary）", "level": "red",
                "value": None, "unit": "", "warn_at": None, "red_at": None,
                "detail": f"canary 检索失败，无法核验回指：{probe_error}"}
    presence = round(100 * with_ref / returned, 1) if returned else None
    resv = round(100 * resolvable / with_ref, 1) if with_ref else None
    verb = round(100 * verbatim / resolvable, 1) if resolvable else None
    level = "green"
    if presence is not None and presence < 100:
        level = "red" if presence < 98 else "yellow"
    if resv is not None and resv < 100:
        level = "red" if resv < 98 else ("yellow" if level == "green" else level)
    # 复核指出：presence/resolvability 由 L1 的 ref 格式保证，结构性不可能失败 ——
    # 于是这条探针此前永远绿。让 canary 命中率参与判级，它才真的能响。
    n_can = len(canaries)
    if n_can and canary_hit == 0:
        level = "red"
    elif n_can and canary_hit < n_can / 2 and level == "green":
        level = "yellow"
    return mo_common.check(
        "provenance", "L1 行号回指（canary）", level,
        presence, "%", 
        f"ref存在 {presence}% ({with_ref}/{returned}) · 可解析 {resv}% · 逐字一致 {verb}% · canary命中 {canary_hit}/{len(canaries)}",
        warn_at=100, red_at=98)


def probe_sot_guard():
    """P0：单一事实源守卫（第③④项；①②属 P1）。"""
    probs = []
    # ③ golden 资产不能进入任何可检索层。
    # L1 用的是 MEMORY_DIR.glob("*.md")（非递归），所以 golden/ 子目录本身安全；
    # 真正的隐患是「golden 目录里存在看起来像每日笔记的文件」——一旦 glob 变成
    # 递归、或目录被移动，它就会立刻被当成事实源扫进来。这里做绊线。
    try:
        if GOLDEN_DIR.resolve() == mo_common.MEMORY_DIR.resolve():
            probs.append("golden 目录与 L1 目录同一层，会被直接扫描")
        dated = [f.name for f in GOLDEN_DIR.glob("*.md") if mo_common.DAILY_RE.match(f.name)]
        if dated:
            probs.append("golden 目录含疑似每日笔记文件 {}（glob 一旦递归即泄漏）".format(",".join(dated[:3])))
        scanned = {x.name for x in mo_common.daily_files()}
        leak = [f.name for f in GOLDEN_DIR.glob("*.md") if f.name in scanned]
        if leak:
            probs.append("golden 文件已在 L1 扫描集中：{}".format(",".join(leak[:3])))
    except OSError:
        pass
    facts = MEMORY_DIR / "unified-facts.jsonl"
    if facts.exists():
        try:
            gj = json.loads((GOLDEN_DIR / "golden-v0.json").read_text())
            qs = [it[2] for it in gj][:40]
            blob = facts.read_text(errors="ignore")
            leak = sum(1 for q in qs if mo_common.norm(q)[:20] in mo_common.norm(blob))
            if leak:
                probs.append(f"unified-facts 含 {leak} 条 golden 题干文本")
        except Exception:  # noqa: BLE001
            pass
    # ④ AUTO 退役状态
    try:
        if re.search(r"自动同步：\d{4}-\d{2}-\d{2}T", MEMORY_MD.read_text(errors="ignore")):
            probs.append("AUTO 自动同步戳被写回")
    except OSError:
        pass
    level = "red" if probs else "green"
    return mo_common.check("sot_guard", "单一事实源守卫", level, len(probs), "处",
                           "；".join(probs) if probs else "golden 未进检索层；AUTO 仍退役")


def _anchor_search_paths(fname):
    """锚点文件可能不在 L1 目录里。MEMORY.md 就在 memory/ 的上一层
    （coremem 架构里 L3=编译缓存不在 L1 目录内），所以要多找几处。"""
    md = mo_common.MEMORY_DIR
    cands = [md / fname, md.parent / fname]
    extra = os.environ.get("MEM_MEMORY_MD")
    if extra:
        cands.append(Path(extra))
    return cands


def _file_lines(fname, cache):
    """取锚点文件的全部行（多路径查找），带缓存。空列表 = 文件真的找不到。"""
    if fname not in cache:
        lines = []
        for cand in _anchor_search_paths(fname):
            if cand.name == fname and cand.exists():
                lines = mo_common.read_lines(cand)
                break
        cache[fname] = lines
    return cache[fname]


def _line_at(fname, n, cache):
    """按 (文件, 行号) 取行文本。文件不存在或行号越界都返回 None ——
    调用方要区分这两者时用 _file_lines()。"""
    lines = _file_lines(fname, cache)
    return lines[n - 1] if 1 <= n <= len(lines) else None


def probe_anchor_drift():
    """P2-M2d：黄金集锚点完整性（行号漂移检测）。

    结构性变更（退役/迁移/改口径）或人工编辑 L1 之后，锚点的 `文件:行号` 会失效。
    README 里写着「改结构后先检查锚点是否还存在」，这条探针把它自动化。

    判定粒度是「题」而不是「单个锚点」—— 一道题列多个锚点时，任一锚点能对上即算在位。
      ok     锚点行仍在原位且含应命中 token
      drifted 原位对不上，但在 ±50 行内找得到 → 行号漂了，题目需要修
      lost    完全找不到 → 内容已消失或锚点本来就不对
    """
    golden_file = Path(os.environ.get("MEM_GOLDEN_SET", GOLDEN_DIR / "golden-v0.json"))
    try:
        items = json.loads(golden_file.read_text())
    except (OSError, ValueError) as e:
        return mo_common.check("anchor_drift", "黄金集锚点完整性", "green", None, "",
                               f"读不到黄金集：{e}", record_only=True)

    cache = {}
    ok = drifted = lost = 0
    file_missing = set()
    bad = []
    for gid, typ, _q, anchors, tokens, _kw in items:
        if typ == "R" or not anchors or not tokens:
            continue
        refs = [a for a in anchors if mo_common.parse_ref(a)]
        if not refs:
            continue                      # 锚点不是 file:line（如 filesystem:...），跳过

        def line_matches(text):
            return bool(text) and all(t.lower() in text.lower() for t in tokens)

        # 先区分「文件找不到」与「行号越界/内容不在原位」
        missing = [mo_common.parse_ref(a)[0] for a in refs
                   if not _file_lines(mo_common.parse_ref(a)[0], cache)]
        if missing:
            file_missing.update(missing)
            lost += 1
            continue
        if any(line_matches(_line_at(*mo_common.parse_ref(a), cache)) for a in refs):
            ok += 1
            continue
        # 原位对不上 → 找 ±50 行
        found = False
        for a in refs:
            f, n = mo_common.parse_ref(a)
            for d in range(1, 51):
                if any(line_matches(_line_at(f, m, cache)) for m in (n - d, n + d)):
                    found = True
                    break
            if found:
                break
        if found:
            drifted += 1
            if len(bad) < 4:
                bad.append(f"{gid}@漂移")
        else:
            lost += 1
            if len(bad) < 4:
                bad.append(f"{gid}@丢失")

    total = ok + drifted + lost
    level = "red" if lost else ("yellow" if drifted else "green")
    detail = f"{golden_file.name} 在位 {ok}/{total} · 漂移 {drifted} · 丢失 {lost}"
    if file_missing:
        detail += f" · 文件缺失 {sorted(file_missing)}"
    if bad:
        detail += f" · {','.join(bad)}"
    return mo_common.check("anchor_drift", "黄金集锚点完整性", level, ok, "题", detail)


def probe_dup_guard():
    """P3：写入落地核验（抽样最近账本）。

    P3 发现写入路径有**双向歧义**：
      B1 报失败但其实已生效 → 重试即重复
      B2 报成功但其实没落地 → 静默丢记忆（实测账本里就有 1 例）
    账本自己记的是「声明成功」，所以不能只看账本。这里按 session_id 回查实际节点数：
      count >= 1 → 已落地
      count == 0 → 声明成功却没落地（丢）
      count >  1 → 重复
    """
    # 复核指出：只看最近 10 条 → 一条 missing 在再写入 10 条后自动移出窗口，
    # **告警自己消失、无需任何确认**。改为把未确认的 missing 累积在一个状态文件里，
    # 只有维护流程显式确认才会清除。
    STATE = OUT_DIR / ".unacked-missing.json"
    rows = mo_common.read_jsonl(mo_common.LEDGER)
    sample = [r for r in rows if r.get("l2_session_id")][-10:]
    try:
        unacked = json.loads(STATE.read_text()) if STATE.exists() else {}
    except (OSError, ValueError):
        unacked = {}
    def _ret(r):
        return any(m in (r.get("note") or "") for m in ("removed", "retired"))

    # 每个 ref 最近一次确认/退役的时间（--ack-missing 追加的行也带 retired）
    ack_ts = {}
    for r in rows:
        if r.get("ref") and _ret(r) and r.get("ts", "") > ack_ts.get(r["ref"], ""):
            ack_ts[r["ref"]] = r.get("ts", "")
    for r in rows:
        if r.get("l2_status") in ("missing",) and r.get("ref") and not _ret(r):
            # seen 取该 ref **最近一次**缺失的时间，确认必须晚于它才算数
            prev = unacked.get(r["ref"])
            if prev is None or r.get("ts", "") > prev.get("seen", ""):
                unacked[r["ref"]] = {"sha": r.get("sha"), "seen": r.get("ts", "")}
    # 已被维护流程标为退役的条目移出未确认集合 —— 只认**晚于**该次缺失的确认，
    # 否则同一 ref 以后再次缺失会被旧的确认行静默掉（复核第五轮指出）
    for r in rows:
        if r.get("ref") in unacked and _ret(r) and r.get("ts", "") >= unacked[r["ref"]].get("seen", ""):
            unacked.pop(r["ref"], None)
    try:
        STATE.write_text(json.dumps(unacked, ensure_ascii=False, indent=1))
    except OSError:
        pass
    # 注意：上面这次写盘发生在「逐行回查」之前，所以现场新发现的缺失必须**再写一次**，
    # 否则本次运行新发现的条目要等到下次运行才落盘（复核第三轮指出了这一点）。
    if not sample:
        if unacked:
            return {"id": "dup_guard", "label": "写入落地核验", "level": "red",
                    "value": len(unacked), "unit": "条", "warn_at": None, "red_at": None,
                    "detail": f"尚无样本可回查，但**未确认的缺失累计 {len(unacked)} 条**"}
        return mo_common.check("dup_guard", "写入落地核验", "green", None, "",
                               "账本尚无带 session_id 的记录", record_only=True)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from plugmem_config import session_status
    except Exception as e:  # noqa: BLE001
        # 复核指出：import 失败时给 record_only 绿灯 = 静默失效。
        # 拿不到核验能力本身就是问题（例如仓库单独部署缺 plugmem_config.py）。
        return {"id": "dup_guard", "label": "写入落地核验",
                "level": "red" if unacked else "yellow",
                "value": None, "unit": "", "warn_at": None, "red_at": None,
                "detail": f"核验能力不可用（session_status 导入失败）：{e}"}

    ok = lost = dup = unknown = 0
    bad = []
    hints = []
    for r in sample:
        cnt, err = session_status(r["l2_session_id"])
        if cnt is None:
            unknown += 1
            continue
        # 曾经把这里按 l2_status == dedup_skipped 豁免（当作"预期去重"）。复核指出：
        # 客户端拿不出相似度证据，那个标签会把真实静默丢失一并掩盖。
        # 现在一律按「缺失」计并告警；如果账本带了 dup_candidates 线索，附在详情里供人判断。
        if cnt == 0 and r.get("ts", "") <= ack_ts.get(r.get("ref"), ""):
            ok += 1; continue   # 已确认（--ack-missing），不再重新加回累积集合
        if cnt == 0:
            lost += 1
            # 复核指出（第三轮）：原先只把账本里 l2_status=="missing" 的行收进累积集合，
            # 而「现场回查发现 count==0」这条路径不入 —— 恰是当初要修的那个洞。
            if r.get("ref"):
                unacked[r["ref"]] = {"sha": r.get("sha"), "seen": r.get("ts", ""), "src": "runtime"}
            hint = r.get("dup_candidates")
            if hint is not None and len(hints) < 3:
                hints.append(f"{r['ref']}:候选{hint}")
            if len(bad) < 3:
                bad.append(f"{r['ref']}@缺失")
        elif cnt > 1:
            dup += 1
            if len(bad) < 3:
                bad.append(f"{r['ref']}@重复x{cnt}")
        else:
            ok += 1
    level = "red" if (lost or dup or unacked) else "green"
    detail = f"抽样 {len(sample)} 条：落地 {ok} · 缺失 {lost} · 重复 {dup}"
    if unacked:
        detail += f" · **未确认的缺失累计 {len(unacked)} 条**（{','.join(list(unacked)[:3])}）"

    if hints:
        detail += f" · 近似候选线索 {';'.join(hints)}"
    if unknown:
        detail += f" · 未判定 {unknown}"
    if bad:
        detail += f" · {','.join(bad)}"
    # 注：missing 无法在客户端区分「被去重」与「真丢」，所以一律告警；
    # dup_candidates 只是线索，不作为豁免依据。
    try:
        STATE.write_text(json.dumps(unacked, ensure_ascii=False, indent=1))
    except OSError:
        pass
    return mo_common.check("dup_guard", "写入落地核验", level, lost + dup, "条", detail)


def probe_kis():
    """P2-M6d：KIS 自检（读 golden/reports/kis-*.json）。

    最近两份结果做 McNemar 精确检验：
      显著退化（Δ≤−3 且 p<0.05）→ 红；仅趋势（|Δ|≥3 但不显著）→ 黄；否则绿。
    另做陈旧检测：超过 8 天没跑 → 黄；超过 15 天 → 红。
    KIS 是自指集合，只做前后成对比较，不与黄金集比绝对值。
    """
    rep_dir = GOLDEN_DIR / "reports"
    files = sorted(rep_dir.glob("kis-*.json")) if rep_dir.exists() else []
    if not files:
        return mo_common.check("kis", "KIS 自检（300 题）", "green", None, "",
                               "尚无 KIS 运行结果（等第一次周更）", record_only=True)
    try:
        latest = json.loads(files[-1].read_text())
    except (OSError, ValueError) as e:
        return mo_common.check("kis", "KIS 自检（300 题）", "green", None, "",
                               f"读不到最近结果：{e}", record_only=True)

    age_days = round((time.time() - files[-1].stat().st_mtime) / 86400, 1)
    cur_l3 = latest.get("kis_recall_layer3")
    cur_g3 = latest.get("kis_recall_l1top3")
    detail = f"layer3 {cur_l3}% / l1top3 {cur_g3}%（{latest.get('n_ok')} 题，{age_days} 天前）"

    level = "green"
    if age_days >= 15:
        level = "red"
    elif age_days >= 8:
        level = "yellow"

    if len(files) >= 2:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import kis_eval as _ke
            prev = json.loads(files[-2].read_text())
            cmp_ = _ke.compare_maps(prev, latest, "hit_layer3")
            if cmp_ and "error" in cmp_:
                detail += f" · 未做比较：{cmp_['error']}"
            elif cmp_:
                b, c, p_ = cmp_["regressed"], cmp_["improved"], cmp_["p"]
                detail += f" · 对上次：退步 {b}/进步 {c}，Δ{cmp_['delta']:+}，p={p_:.3g}"
                if cmp_["delta"] <= -3 and p_ < 0.05 and level != "red":
                    level = "red"
                    detail += " → 显著退化"
                elif abs(cmp_["delta"]) >= 3 and level == "green":
                    level = "yellow"
                    detail += " → 仅趋势"
        except Exception as e:  # noqa: BLE001
            detail += f" ·（成对比较失败：{type(e).__name__}）"

    return mo_common.check("kis", "KIS 自检（300 题）", level, cur_l3, "%", detail,
                           warn_at=8, red_at=15)


def probe_write_fidelity():
    """P1-M1：写入保真（读 data/write_ledger.jsonl）。

    M1a l1_append_ok : 账本里的 ref 能否解析回 L1，且 sha 一致 —— L1 是事实源，容忍度为 0
    M1b l2_ok_rate   : 写入满 60 min 的条目里，l2_status=ok 的比例
    账本尚无数据时只记录、不告警（record_only），避免新探针刷屏。
    """
    rows = mo_common.read_jsonl(mo_common.LEDGER)
    if not rows:
        return mo_common.check("write_fidelity", "写入保真（L1 回指/L2 落地）", "green", None, "",
                               "write_ledger.jsonl 尚未产生（等下一次 remember.py 写入）",
                               record_only=True)

    def ts_of(r):
        """解析账本时间戳；无时区的历史行按本地时区处理，解析不了返回 None。
        （flush 曾写出过无时区的时间戳，与 remember 的行混在一起会做时间相减报错。）"""
        try:
            dt = datetime.fromisoformat(r.get("ts", ""))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt

    now = datetime.now(ts_of(rows[-1]).tzinfo) if ts_of(rows[-1]) else datetime.now()
    recent = [r for r in rows if ts_of(r) and (now - ts_of(r)).total_seconds() <= 86400]
    sample = recent or rows[-50:]

    checked = ok = 0
    bad = []
    retired = 0
    for r in sample[-200:]:
        ref, sha = r.get("ref"), r.get("sha")
        if not ref or not sha:
            continue
        # 维护性退役：只有维护流程（如清除误写样本）才允许打这个标记。
        # 单独计数、不当作失配 —— 否则探针会被一次清理永久钉在黄灯。
        if "removed" in (r.get("note") or "") or "retired" in (r.get("note") or ""):
            retired += 1
            continue
        checked += 1
        line = mo_common.resolve(ref)
        if line is not None and mo_common.line_sha(line) == sha:
            ok += 1
        elif len(bad) < 3:
            bad.append(ref)

    # ack 行是确认记录而非写入：不进分母；被确认过的 ref 也不再计为未落地
    acked = {r.get("ref") for r in rows if r.get("l2_status") == "ack"}
    matured = [r for r in sample if ts_of(r) and (now - ts_of(r)).total_seconds() >= 3600
               and r.get("l2_status") != "ack" and r.get("ref") not in acked]
    # 曾经把 dedup_skipped 排除出分母（当作预期行为）。复核指出这会把真实丢失一起掩盖，
    # 现在不再豁免：只要最终状态不是 ok，就算没落地。
    l2_ok = sum(1 for r in matured if r.get("l2_status") == "ok")
    l2_rate = round(100 * l2_ok / len(matured), 1) if matured else None
    l1_rate = round(100 * ok / checked, 1) if checked else None

    level = "green"
    if l1_rate is not None and l1_rate < 100:
        level = "red" if l1_rate < 98 else "yellow"
    if l2_rate is not None and l2_rate < 99 and level == "green":
        level = "red" if l2_rate < 95 else "yellow"
    detail = (f"L1 回指 {l1_rate}% ({ok}/{checked})" +
              (f" · 已退役 {retired}" if retired else "") +
              (f" · L2 落地 {l2_rate}% ({l2_ok}/{len(matured)})" if l2_rate is not None
               else " · L2 落地 待满 60min 样本") +
              (f" · 失配样例 {bad}" if bad else ""))
    return mo_common.check("write_fidelity", "写入保真（L1 回指/L2 落地）", level,
                           l1_rate, "%", detail, warn_at=100, red_at=98)


def probe_cost():
    """P0-M3a：检索侧 LLM 调用数（recall_decisions.jsonl 为 P2 产物）。"""
    rows = mo_common.read_jsonl(mo_common.DECISIONS)
    if not rows:
        return mo_common.check("cost", "检索侧成本（未接入）", "green", None, "",
                               "recall_decisions.jsonl 无任何写入方 —— 该探针当前不度量任何东西", record_only=True)
    recent = rows[-200:]
    calls = sum(r.get("llm_calls", 0) for r in recent)
    per_q = round(calls / len(recent), 3)
    level = "red" if calls > 0 else "green"
    return mo_common.check("cost", "检索侧成本", level, per_q, "次LLM调用/查询",
                           f"近 {len(recent)} 次查询共 {calls} 次 LLM 调用；中位上下文 "
                           f"{int(statistics.median([r.get('ctx_tokens', 0) for r in recent]))} tokens")



def ack_missing(refs):
    """把「已确认处理过」写成账本追加行（note 含 retired），使 dup_guard 的累积红灯可清除。

    复核指出：此前清除只能靠**手工编辑账本**，而账本是 append-only 的观测产物，
    手工改它本身就不合适 → 结果是红灯一旦出现就永远清不掉，长期会让人麻木。
    这里提供一个可执行、可审计的确认入口（只追加、不修改历史行）。
    """
    n = 0
    try:
        with open(mo_common.LEDGER, "a", encoding="utf-8") as f:
            for ref in refs:
                f.write(json.dumps({
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "ref": ref, "sha": "", "l2_status": "ack",
                    "note": "retired: 已人工确认（memory_health --ack-missing）",
                }, ensure_ascii=False) + "\n")
                n += 1
    except OSError as e:
        print(f"账本写入失败：{e}", file=sys.stderr)
        return 1
    print(f"已追加 {n} 条确认行（ref: {', '.join(refs)}）")
    return 0


def probe_retrieval():
    """检索层自检（2026-10-26 新增）。

    起因是一个真实缺陷：split_tokens 的中文扩展要求「中文 + 数字」同时出现，
    于是「中文 + 英文」的查询（硬编码空间ID、boss-sender）整条不展开，
    只剩整句一个 token —— 整句几乎不会逐字出现在正文，**facts 层直接为空**。
    这个故障不会让任何既有探针变红，只能靠"问一句、看有没有回来"发现。

      A. 混合中英文分词必须展开（纯本地，无 IO）
      B. 用 MEMORY.md 里真实存在的词提问 → facts 层必须非空
      C. 用 L1 里真实存在的词提问 → l1 层必须非空
    """
    try:
        import memory_recall as R
    except Exception as e:  # noqa: BLE001
        return mo_common.check("retrieval", "检索层自检", "yellow", None, "",
                               f"无法导入 memory_recall：{type(e).__name__}", record_only=True)

    bad, notes, probes = [], [], 0

    mixed = R.split_tokens("硬编码空间ID测试")
    if len(mixed) < 3 or not any(re.fullmatch(r"[A-Za-z]+", t) for t in mixed):
        bad.append(f"A分词未展开({len(mixed)}个)")

    try:
        if R.MEMORY_MD.exists():
            txt = R.MEMORY_MD.read_text(encoding="utf-8", errors="ignore")
            cands = [w for w in re.findall(r"[\u4e00-\u9fff]{4,8}", txt)
                     if w not in R.STOPWORDS]
            for w in cands[:2]:
                probes += 1
                if not R.search_fact_files(w, max_results=3):
                    bad.append(f"B facts空({w[:6]})")
    except Exception as e:  # noqa: BLE001
        notes.append(f"B异常({type(e).__name__})")

    try:
        md = sorted(R.MEMORY_DIR.glob("2026-*.md")) if hasattr(R, "MEMORY_DIR") else []
        if md:
            last = md[-1].read_text(encoding="utf-8", errors="ignore").splitlines()
            line = next((l for l in reversed(last)
                         if len(l) > 20 and re.search(r"[\u4e00-\u9fff]{4,}", l)), "")
            ws = re.findall(r"[\u4e00-\u9fff]{4,6}", line)
            if ws:
                probes += 1
                if not R.search_daily_logs(ws[0], max_results=3):
                    bad.append(f"C l1空({ws[0][:6]})")
    except Exception as e:  # noqa: BLE001
        notes.append(f"C异常({type(e).__name__})")

    tail = ("｜" + "；".join(notes)) if notes else ""
    if probes == 0 and not bad:
        return mo_common.check("retrieval", "检索层自检", "yellow", None, "",
                               "拿不到可验证样本（语料为空？）" + tail, record_only=True)
    if bad:
        return mo_common.check("retrieval", "检索层自检", "red", len(bad), "项",
                               "；".join(bad) + tail)
    return mo_common.check("retrieval", "检索层自检", "green", probes, "项",
                           f"{probes} 项提问均正常返回" + tail)


def probe_l2_recall():
    """语义层（L2）已知项召回（读 golden/reports/l2kis-*.json）。

    为什么需要：黄金集与 KIS 的锚点全在 L1，实测 semantic 层从未成为唯一证据来源，
    所以**任何改动只要动的是语义层，这两套基准都看不见**（复核 2026-10-26 指出）。
    l2_kis.py 直接拿 L2 节点出题，才测得到这一层。

    判级：与上一份比，derived 召回掉 ≥5 点 → 红；掉 2~5 点 → 黄；
    超过 8 天没跑 → 黄。
    """
    rep = GOLDEN_DIR / "reports"
    files = sorted(rep.glob("l2kis-*.json")) if rep.exists() else []
    if not files:
        return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "yellow", None, "",
                               "尚无 l2_kis 运行结果（先跑 scripts/l2_kis.py --n 200）",
                               record_only=True)
    def load(p):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        return d
    cur = load(files[-1])
    if cur is None:
        return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "yellow", None, "",
                               f"{files[-1].name} 解析失败", record_only=True)
    def recall(d, key):
        # 报告结构：{"summary": {"derived": {"n":200,"hit":184,"rate":92.0}, ...}}
        v = (d.get("summary") or {}).get(key) or {}
        if v.get("rate") is not None:
            return float(v["rate"])
        n, hit = v.get("n") or 0, v.get("hit") or 0
        return (100.0 * hit / n) if n else None
    cd = recall(cur, "derived")
    if cd is None:
        return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "yellow", None, "",
                               "报告缺 derived 统计", record_only=True)
    age = (datetime.now().timestamp() - files[-1].stat().st_mtime) / 86400
    prev = load(files[-2]) if len(files) >= 2 else None
    pd_ = recall(prev, "derived") if prev else None
    detail = f"derived {cd:.1f}%"
    if pd_ is not None:
        detail += f"（上次 {pd_:.1f}%，Δ{cd - pd_:+.1f}）"
    if age > 8:
        detail += f"｜{age:.0f} 天未更新"
    if pd_ is not None and cd - pd_ <= -5:
        return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "red", round(cd, 1), "%",
                               detail, warn_at=pd_ - 2, red_at=pd_ - 5)
    if (pd_ is not None and cd - pd_ <= -2) or age > 8:
        return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "yellow", round(cd, 1), "%",
                               detail, warn_at=pd_ - 2 if pd_ else None, red_at=pd_ - 5 if pd_ else None)
    return mo_common.check("l2_recall", "语义层召回（L2 已知项）", "green", round(cd, 1), "%", detail)


def main():
    # 先建输出目录：probe_consolidation 会写 .consolidation-count，
    # 目录不存在时会在探针内抛 FileNotFoundError（生产目录恰好存在，故此前未暴露）。
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 逐个跑并**隔离异常**：任何单个探针崩掉都不能拖垮整块看板。
    # 起因（复核发现）：flush 写的账本行时间戳没带时区，与 remember 写的行混在一起，
    # probe_write_fidelity 做时间相减时抛 TypeError，main() 没有保护 →
    # **15 个探针全部停止产出**，而且每 15 分钟崩一次。
    # 崩掉的探针会变成一条 red，而不是消失。
    _probes = [
        ("plugmem", probe_plugmem), ("capture", probe_capture), ("l1", probe_l1),
        ("auto", probe_auto_retired), ("buffer", probe_buffer),
        ("consolidation", probe_consolidation), ("eval", probe_eval), ("backup", probe_backup),
        ("provenance", probe_provenance), ("sot_guard", probe_sot_guard),
        ("write_fidelity", probe_write_fidelity), ("kis", probe_kis),
        ("anchor_drift", probe_anchor_drift), ("dup_guard", probe_dup_guard),
        # 2026-11-11 审计 P2：cost 探针移除——recall_decisions.jsonl 无任何写入方，
        # 检索链路纯确定性零 LLM，探针不度量任何东西（假绿）。函数保留，
        # 将来若引入 LLM 检索再挂回。
        ("retrieval", probe_retrieval),
        ("l2_recall", probe_l2_recall),
    ]
    checks = []
    for _cid, _fn in _probes:
        try:
            checks.append(_fn())
        except Exception as _e:  # noqa: BLE001
            checks.append({"id": _cid, "label": f"{_cid}（探针自身异常）", "level": "red",
                           "value": None, "unit": "", "warn_at": None, "red_at": None,
                           "detail": f"探针抛出 {type(_e).__name__}: {_e}"[:180]})
    reds = [c for c in checks if c["level"] == "red"]
    yellows = [c for c in checks if c["level"] == "yellow"]
    overall = "red" if reds else ("yellow" if yellows else "green")

    def fmt(c):
        v = "?" if c["value"] is None else f"{c['value']}{c['unit']}"
        return f"{c['label']}={v}"

    problems = reds or yellows
    line = ("记忆系统 " + overall.upper() + "：" +
            "；".join(fmt(c) for c in problems)) if problems else "记忆系统全部正常"

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall": overall,
        "line": line,
        "checks": checks,
    }
    # 2026-11-11 审计 P0-3：RED/YELLOW 必须告警，不能只写盘。
    # health 每日 9:00 跑一次 → 每天至多一条，无需额外去重。
    # 开关：MEM_HEALTH_NOTIFY=0 关闭；notify 配置缺失/发送失败只降级记录，不拖垮看板。
    alert = "skipped"
    if overall != "green":
        if os.environ.get("MEM_HEALTH_NOTIFY", "1") == "0":
            alert = "disabled"
        else:
            try:
                import notify
                ok = notify.send(line, title=f"记忆系统 {overall.upper()}")
                alert = "sent" if ok else "api_rejected"
            except Exception as e:  # noqa: BLE001
                alert = f"failed: {e}"[:200]
                print(f"[health] 告警发送失败：{e}", file=sys.stderr)
    payload["alert"] = alert
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, OUT)
    print(line)
    return 2 if reds else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--ack-missing":
        if len(sys.argv) < 3:
            print("用法：memory_health.py --ack-missing <ref> [<ref> ...]", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(ack_missing(sys.argv[2:]))
    raise SystemExit(main())
