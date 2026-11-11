#!/bin/bash
# ============================================
# 知识库事实一致性门禁（每日）
#
# 为什么存在：同一事实在不同文档给出不同取值时，**检索到哪份文档就得到哪个答案**。
# 2026-10-24 首次核对抓到 4 条（宁波→墨尔本/布里斯班 的时效两处不一致）。
#
# 设计要点（避免重演 wiki-autocommit 那个「静默假绿」）：
#   * 每次运行都写心跳 —— 心跳缺失即任务没跑，不靠退出码
#   * 退出码原样透传：0=无冲突 1=有冲突 2=路径问题，launchd 里能看出红
#   * 结果落 health/kb-consistency.json，供日报/看板消费
# ============================================
set -uo pipefail
[ -f "$HOME/.coremem/coremem.env" ] && . "$HOME/.coremem/coremem.env"
CHECKER="${KB_CHECKER:-$HOME/.coremem/scripts/kb_consistency_check.py}"
HEALTH_DIR="$HOME/.coremem/health"
HB="$HEALTH_DIR/kb-consistency.heartbeat"
OUT="$HEALTH_DIR/kb-consistency.json"
LOG="$HOME/.coremem/logs/kb-consistency.log"
TMP="$HEALTH_DIR/.kb-consistency.tmp.json"
mkdir -p "$HEALTH_DIR" "$(dirname "$LOG")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ ! -f "$CHECKER" ]; then
    echo "[$(ts)] 找不到检查脚本: $CHECKER" >>"$LOG"
    printf '%s rc=2 checker-missing\n' "$(ts)" > "$HB"
    exit 2
fi

python3 "$CHECKER" --json "$TMP" >>"$LOG" 2>&1
RC=$?

case "$RC" in
    0) SUMMARY="无冲突" ;;
    1) SUMMARY="有冲突" ;;
    *) SUMMARY="运行失败(rc=$RC)" ;;
esac

python3 - "$TMP" "$OUT" "$RC" "$SUMMARY" <<'PY' 2>/dev/null || true
import json, sys
tmp, out, rc, summary = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
try:
    d = json.load(open(tmp))
except Exception:
    d = {}
conf = d.get("conflicts")
if isinstance(conf, list):
    summary = f"{len(conf)} 条冲突" if conf else "无冲突"
d["gate"] = {"exit_code": rc, "summary": summary,
             "line": f"知识库一致性门禁 {summary}"}
open(out, "w").write(json.dumps(d, ensure_ascii=False, indent=1))
PY

# 心跳文案从 JSON 读回，保证与 health 产物一致（两处不一致=新的假绿）
SUMMARY="$(python3 -c "
import json
try: print(json.load(open('$OUT'))['gate']['summary'])
except Exception: print('$SUMMARY')
" 2>/dev/null || echo "$SUMMARY")"
printf '%s rc=%s %s\n' "$(ts)" "$RC" "$SUMMARY" > "$HB"
echo "[$(ts)] 门禁 rc=$RC $SUMMARY" >>"$LOG"
exit $RC
