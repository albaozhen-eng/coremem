#!/usr/bin/env bash
# openclaw-retire-gate.sh — 删除 ~/.openclaw 的前置闸门
#
# 为什么需要它：2026-10-30 和 11-03 两次都宣布"解耦完成"，但都没删掉旧根。
# 复盘发现四个原因（见 coremem/docs/OPENCLAW-RETIRE.md）：
#   ① 把"记忆系统解耦"当成"OpenClaw 可删除"（旧根还住着 BOSS 与 25 个项目）
#   ② 验证只覆盖自己改的部分（"21 个文件一致"），没有一条检查是"全机还剩多少引用"
#   ③ launchd 只改 1 个就宣布完成
#   ④ "用一两周再删"没落成任何可检查的东西，话题自然沉掉
#
# 所以这个闸门只问一件事：**现在删掉它，会不会坏？**
# 每项都是可穷举、可复查的判据，不看"我觉得改完了"。
#
# 用法：
#   bash openclaw-retire-gate.sh            # 人读报告
#   bash openclaw-retire-gate.sh --json f   # 机读
# 退出码：0=可以删 / 1=还有阻塞项 / 2=检查自身出错

set -uo pipefail
H="$HOME"
OC="$H/.openclaw"
LA="$H/Library/LaunchAgents"
JSON=""
[ "${1:-}" = "--json" ] && JSON="${2:-}"

BLOCK=0
declare -a NAMES VALUES DETAILS

add() {  # add <名称> <是否阻塞 0|1> <值> <说明>
    NAMES+=("$1"); VALUES+=("$2"); DETAILS+=("$4|$3")
    [ "$2" = "1" ] && BLOCK=1
    if [ "$2" = "1" ]; then printf '  🔴 %-28s %s\n' "$1" "$3"
    else printf '  ✅ %-28s %s\n' "$1" "$3"; fi
}

echo "═══ OpenClaw 删除前置闸门 ═══"
[ -d "$OC" ] || { echo "  旧根已不存在，无需检查"; exit 0; }

# ── 1. 活跃 launchd 是否还引用旧根（含 WorkingDirectory / 日志重定向）──
n=0; hits=""
for p in "$LA"/*.plist; do
    [ -f "$p" ] || continue
    if grep -q "$OC" "$p" 2>/dev/null; then n=$((n+1)); hits="$hits $(basename "$p" .plist)"; fi
done
add "活跃 plist 引用" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个${hits:+ →$hits}" \
    "任一非 0 即删后静默失效"

# ── 2. 活体脚本是否还引用旧根（白名单：有意保留的）──
#   tiered-backup.sh   备份旧根（删前保护，删后该源自动跳过）
#   openclaw_archive.py 注释里提到旧根（实际默认已指 coremem/legacy）
n=0; hits=""
for d in "$H/.coremem/scripts" "$H/.boss-agent/scripts"; do
    [ -d "$d" ] || continue
    for f in "$d"/*.py "$d"/*.sh; do
        [ -f "$f" ] || continue
        b=$(basename "$f")
        case "$b" in
            # 有意保留：备份旧根 / 读历史归档（默认已指 coremem/legacy）
            tiered-backup.sh|openclaw_archive.py) continue;;
            # 本闸门与到期提醒自身以旧根为检查目标，必然引用它，不算阻塞
            openclaw-retire-gate.sh|openclaw-retire-watch.sh) continue;;
        esac
        # 只算非注释行
        if grep -vE '^\s*#' "$f" 2>/dev/null | grep -q "\.openclaw"; then
            n=$((n+1)); hits="$hits $b"
        fi
    done
done
add "活体脚本引用" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个${hits:+ →$hits}" \
    "白名单外的硬编码路径"

# ── 3. 是否有进程仍持有旧根文件句柄 ──
#   改 plist 不影响**已在运行**的常驻进程 —— 10-30 那次就漏了这点。
n=$(lsof +D "$OC" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')
who=$(lsof +D "$OC" 2>/dev/null | tail -n +2 | awk '{print $1"(pid "$2")"}' | sort -u | head -3 | tr '\n' ' ')
add "进程持有旧根句柄" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个 ${who}" \
    "需重启这些进程才会释放"

# ── 4. 近 N 天是否仍有写入（排除迁移备份与已知冷目录）──
DAYS="${OC_RETIRE_QUIET_DAYS:-3}"
n=$(find "$OC" -maxdepth 3 -newermt "-${DAYS} days" -type f 2>/dev/null \
    | grep -v "premigrate" | wc -l | tr -d ' ')
tops=$(find "$OC" -maxdepth 3 -newermt "-${DAYS} days" -type f 2>/dev/null \
    | grep -v premigrate | sed "s|$OC/||;s|/.*||" | sort | uniq -c | sort -rn \
    | head -3 | awk '{printf "%s(%s) ",$2,$1}')
add "近 ${DAYS} 天写入" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个文件 ${tops}" \
    "仍有写入说明还有活跃依赖"

# ── 5. 新家数据是否齐全（删旧根不能丢东西）──
miss=0; detail=""
for pair in "$H/.coremem/memory:L1日记" "$H/.coremem/MEMORY.md:L3规则" \
            "$H/.coremem/plugmem/chroma:L2语义图" "$H/.boss-agent/data:BOSS数据" \
            "$H/.coremem/legacy/agents:历史归档"; do
    p="${pair%%:*}"; label="${pair##*:}"
    if [ ! -e "$p" ]; then miss=$((miss+1)); detail="$detail $label缺失"; fi
done
add "新家数据齐全" "$([ "$miss" -gt 0 ] && echo 1 || echo 0)" "${detail:-全部就位}" \
    "L1/L2/L3/BOSS/归档"

# ── 6. 关键服务与任务是否健康 ──
hp=$(curl -s -m 8 http://127.0.0.1:8089/api/v1/health 2>/dev/null | head -c 200)
ok=0; echo "$hp" | grep -q '"status": *"ok"' && ok=1
add "PlugMem 服务" "$([ "$ok" = 1 ] && echo 0 || echo 1)" "$([ "$ok" = 1 ] && echo ok || echo 异常)" \
    "L2 读写依赖"

# 迁移过的任务：非 0 且不在"迁移前即失败"白名单里的，算阻塞
KNOWN_BAD="com.user.mem0-daemon ai.boss.wechat-watch com.lealway.wecom-oauth com.jicheng.finance-ocr com.lealway.llama-tunnel com.lealway.wecom-tunnel com.lealway.kb-consistency com.lealway.omlx-server ai.pi.smart-router"
bad=""; nbad=0
for p in "$LA"/*.plist.bak-full* "$LA"/*.plist.bak-sweep* "$LA"/*.plist.bak-wd*; do
    [ -f "$p" ] || continue
    L=$(basename "$p" | sed 's/\.plist\.bak.*//')
    rc=$(launchctl list 2>/dev/null | awk -v l="$L" '$3==l{print $2}')
    [ -z "$rc" ] && continue
    [ "$rc" = "0" ] && continue
    case " $KNOWN_BAD " in *" $L "*) continue;; esac
    nbad=$((nbad+1)); bad="$bad $L(rc=$rc)"
done
add "迁移任务健康" "$([ "$nbad" -gt 0 ] && echo 1 || echo 0)" "${bad:-无新增失败}" \
    "排除迁移前即存在的失败"

echo
if [ "$BLOCK" = "0" ]; then
    echo "  ✅ 全部通过 —— 可以删除 $OC"
else
    echo "  🔴 存在阻塞项 —— 不要删除。逐项处理后再跑本闸门。"
fi

if [ -n "$JSON" ]; then
    {
        printf '{\n  "checked_at": "%s",\n  "can_delete": %s,\n  "checks": [\n' \
               "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$([ "$BLOCK" = 0 ] && echo true || echo false)"
        for i in "${!NAMES[@]}"; do
            [ "$i" -gt 0 ] && printf ',\n'
            printf '    {"name": "%s", "blocking": %s, "value": "%s"}' \
                   "${NAMES[$i]}" "$([ "${VALUES[$i]}" = 1 ] && echo true || echo false)" \
                   "$(echo "${DETAILS[$i]}" | sed 's/"/\\"/g')"
        done
        printf '\n  ]\n}\n'
    } > "$JSON"
fi
exit "$BLOCK"
