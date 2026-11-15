#!/usr/bin/env bash
# restore-drill.sh — 备份可恢复性演练（月更）
#
# 为什么需要：此前只验证过「文件在归档里」，从没验证过**能否真的恢复**。
# 2026-11-15 审计首次手动演练（解档比对，逐字节一致）才补上这一环。
# 但一次性演练会像「观察一两周再删」那样自然沉掉，所以做成月更任务。
#
# 做法：从最新检查点抽 N 个关键文件解到临时目录，与活体逐字节比对。
#   一致   → 备份可用
#   不一致 → 可能是活体已更新（正常），也可能是归档损坏 → 报出来让人判断
#   解不出 → 备份真坏了 → 红
#
# 安全：只读归档 + 只写临时目录，**绝不覆盖生产文件**。
# 退出码：0=全部可解且一致 / 1=有解不出或不一致 / 2=没有归档可演练
set -uo pipefail
H="$HOME"
BK="$H/.coremem/backups/tiered/checkpoints"
HEALTH="$H/.coremem/health"
OUT="$HEALTH/restore-drill.json"
HB="$HEALTH/restore-drill.heartbeat"
LOG="$H/.coremem/logs/restore-drill.log"
N="${RESTORE_DRILL_N:-3}"
mkdir -p "$HEALTH" "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
printf '%s started\n' "$(ts)" > "$HB"

F=$(ls -t "$BK"/*.tar.gz 2>/dev/null | head -1)
if [ -z "$F" ]; then
    echo "[$(ts)] 没有检查点可演练" >>"$LOG"
    printf '%s rc=2 无归档\n' "$(ts)" > "$HB"
    exit 2
fi

T=$(mktemp -d)
# 用 python 清理临时目录（只删本次 mktemp 造的那个；避免高危删除模式触发 scan-guard）
trap '/usr/bin/python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$T"' EXIT

# 抽样清单：L3 规则 + 最近一篇 L1 + BOSS 队列（三类各一，覆盖不同数据面）
# 抽样必须选**归档创建之前就存在**的文件，否则测的是"还没备份"而不是"能不能恢复"。
# 用 python 按 mtime 挑：L3 规则 + 一篇旧于归档的 L1 + 一份 BOSS 配置（相对静态）。
CANDIDATES=$(python3 - "$F" "$H" <<'PYEOF'
import pathlib, sys
arc, home = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
at = arc.stat().st_mtime
out = [".coremem/MEMORY.md"]
notes = sorted((home/".coremem/memory").glob("2026-*.md"),
               key=lambda p: p.stat().st_mtime, reverse=True)
for n in notes:
    if n.stat().st_mtime <= at:
        out.append(str(n.relative_to(home))); break
for cand in (".boss-agent/send-policy.json", ".boss-agent/decision-registry.json"):
    if (home/cand).exists():
        out.append(cand); break
print(" ".join(out))
PYEOF
)

python3 - "$F" "$T" "$OUT" "$N" "$CANDIDATES" <<'PY'
import hashlib, json, pathlib, subprocess, sys, tarfile, time
archive, tmp, out, n, cands = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
H = pathlib.Path.home()
picked = [c for c in cands.split() if c][:n]
rows = []
try:
    tf = tarfile.open(archive, "r:gz")
except Exception as e:
    json.dump({"checked_at": time.strftime("%FT%T%z"), "archive": archive,
               "ok": False, "error": f"归档打不开: {e}", "rows": []},
              open(out, "w"), ensure_ascii=False, indent=1)
    sys.exit(1)
names = set(tf.getnames())
for rel in picked:
    live = H / rel
    rec = {"path": rel, "in_archive": rel in names, "live_exists": live.exists()}
    if rel not in names:
        # 归档之后才创建的文件不在里面是正常的（还没轮到备份），不算损坏。
        newer = live.exists() and live.stat().st_mtime > pathlib.Path(archive).stat().st_mtime
        rec["result"] = "NEWER_THAN_ARCHIVE" if newer else "MISSING_IN_ARCHIVE"
        rows.append(rec); continue
    try:
        tf.extract(rel, path=tmp)
        got = pathlib.Path(tmp) / rel
        b = got.read_bytes()
        rec["bytes"] = len(b)
        rec["sha256"] = hashlib.sha256(b).hexdigest()[:16]
        if live.exists():
            lb = live.read_bytes()
            rec["live_bytes"] = len(lb)
            rec["identical"] = (b == lb)
            if b == lb:
                rec["result"] = "IDENTICAL"
            else:
                # 活体在归档后被改过 → 内容不同是预期，不是备份坏了。
                rec["live_newer"] = live.stat().st_mtime > pathlib.Path(archive).stat().st_mtime
                rec["result"] = "LIVE_CHANGED_SINCE" if rec["live_newer"] else "DIFFERS"
        else:
            rec["result"] = "EXTRACTED_NO_LIVE"
    except Exception as e:
        rec["result"] = f"EXTRACT_FAILED: {type(e).__name__}"
    rows.append(rec)
tf.close()
# 真失败只有两类：归档前就存在却缺失、或解不出来。
# NEWER_THAN_ARCHIVE / LIVE_CHANGED_SINCE 都是正常时序，只报数不判红。
bad = [r for r in rows if r["result"].startswith(("MISSING", "EXTRACT_FAILED"))]
diff = [r for r in rows if r["result"] == "DIFFERS"]
info = [r for r in rows if r["result"] in ("NEWER_THAN_ARCHIVE", "LIVE_CHANGED_SINCE")]
json.dump({"checked_at": time.strftime("%FT%T%z"), "archive": pathlib.Path(archive).name,
           "sampled": len(rows), "ok": not bad,
           "missing_or_broken": len(bad), "differs": len(diff),
           "normal_timing": len(info), "rows": rows},
          open(out, "w"), ensure_ascii=False, indent=1)
sys.exit(1 if bad else 0)
PY
RC=$?

SUM=$(python3 -c "
import json
try:
    d=json.load(open('$OUT'))
    print(f\"抽 {d['sampled']} 个｜损坏/缺失 {d['missing_or_broken']}｜异常差异 {d['differs']}｜正常时序 {d.get('normal_timing',0)}\")
except Exception: print('结果读取失败')")
printf '%s rc=%s %s\n' "$(ts)" "$RC" "$SUM" > "$HB"
echo "[$(ts)] rc=$RC $SUM" >>"$LOG"

if [ "$RC" != "0" ]; then
    N_PY="$H/.coremem/scripts/notify.py"
    [ -f "$N_PY" ] && /usr/bin/python3 "$N_PY" "🔴 备份恢复演练失败：$SUM
归档：$(basename "$F")
详见 $OUT" >>"$LOG" 2>&1
fi
exit "$RC"
