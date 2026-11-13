#!/bin/bash
# openclaw-retire-watch.sh — 观察期到点提醒 + 闸门复查
#
# 为什么需要：2026-10-30 留过一句「用一两周确认无误后再删」，
# 但它没落成任何可检查的东西 —— 10-31/11-01/11-09 的记录里 coremem 提及为 0，
# 话题自然沉掉，旧根一直留着。这个任务把那句话变成**会自己响的闹钟**。
#
# 行为：
#   观察期内     → 只记日志，不打扰
#   到期且闸门绿 → 通知「可以退役了」，给出可回退的重命名命令
#   到期但闸门红 → 通知列出阻塞项（这才是真正有用的提醒）
set -uo pipefail
H="$HOME"; OC="$H/.openclaw"
GATE="$H/.coremem/scripts/openclaw-retire-gate.sh"
STATE="$H/.coremem/health/openclaw-retire.json"
LOG="$H/.coremem/logs/openclaw-retire.log"
HB="$H/.coremem/health/openclaw-retire.heartbeat"
START="${OC_RETIRE_START:-2026-11-12}"
DAYS="${OC_RETIRE_DAYS:-7}"
mkdir -p "$(dirname "$STATE")" "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

[ -d "$OC" ] || { printf '%s rc=0 旧根已退役\n' "$(ts)" > "$HB"; exit 0; }
[ -f "$GATE" ] || { printf '%s rc=2 闸门脚本缺失\n' "$(ts)" > "$HB"; exit 2; }

due=$(python3 -c "
import datetime
s=datetime.date.fromisoformat('$START'); d=s+datetime.timedelta(days=$DAYS)
print(d.isoformat(), (datetime.date.today()>=d))")
DUE_DATE="${due%% *}"; IS_DUE="${due##* }"

bash "$GATE" --json "$STATE" >>"$LOG" 2>&1
RC=$?
SUM=$(python3 -c "
import json
try:
    d=json.load(open('$STATE'))
    bad=[c['name'] for c in d['checks'] if c['blocking']]
    print(('阻塞: '+', '.join(bad)) if bad else '全部通过')
except Exception: print('闸门结果读取失败')")

printf '%s rc=%s 到期=%s(%s) %s\n' "$(ts)" "$RC" "$IS_DUE" "$DUE_DATE" "$SUM" > "$HB"
echo "[$(ts)] rc=$RC due=$IS_DUE($DUE_DATE) $SUM" >> "$LOG"

[ "$IS_DUE" != "True" ] && exit 0   # 未到期不打扰

NOTIFY="$H/.coremem/scripts/notify.py"
if [ "$RC" = "0" ]; then
  MSG="OpenClaw 观察期已满（$DUE_DATE），删除前置闸门全部通过。
建议先重命名观察一天，确认无异常再彻底清理：
  mv ~/.openclaw ~/.openclaw.retired
闸门：bash $GATE"
else
  MSG="OpenClaw 观察期已满（$DUE_DATE），但闸门仍有阻塞项：
$SUM
逐项处理后重跑：bash $GATE"
fi
[ -f "$NOTIFY" ] && /usr/bin/python3 "$NOTIFY" "$MSG" >>"$LOG" 2>&1
exit "$RC"
