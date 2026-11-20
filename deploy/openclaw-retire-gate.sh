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

# ── 2. 已部署脚本是否还引用旧根（阻塞）+ 仓库副本（仅提示）──
#   2026-11-15 审计补两点：
#   ① 原来只扫 2 个目录，漏掉 skills-openclaw 与 projects；
#   ② 但把它们一律算阻塞会造出大量噪音——那些是仓库副本/历史备份，不是活体。
#      噪音大的闸门会被人忽略，等于没有闸门。
#   所以分两级：**已部署目录**里的引用才阻塞；仓库/技能副本只报数供人判断。
#   两级都排除 .backup/.bak/node_modules/.git/dist，且只看 .py/.sh
#   （.json 里的旧路径是历史数据记录，删旧根不影响）。
_scan_refs() {   # $1=根目录 → 输出命中文件名（每行一个）
    find "$1" -maxdepth 4 \( -name node_modules -o -name .git -o -name dist \
         -o -name '.backup*' -o -name '*.bak*' \) -prune -o \
         \( -name '*.py' -o -name '*.sh' -o -name '*.md' \) -type f -print 2>/dev/null |
    while IFS= read -r f; do
        b=$(basename "$f")
        case "$b" in
            tiered-backup.sh|openclaw_archive.py) continue;;
            openclaw-retire-gate.sh|openclaw-retire-watch.sh) continue;;
        esac
        # 2026-11-19 窄豁免：行内显式标注 OC-AUDIT-REF 的引用是**审计用途**
        # （例如 boss-regression.py 要检查旧副本是否已标「已弃用副本」），
        # 不是活依赖。只豁免带该标注的行，且单独报数可见 —— 不整体放宽扫描。
        if grep -vE '^[[:space:]]*#' "$f" 2>/dev/null | grep -v 'OC-AUDIT-REF' | grep -q "\.openclaw"; then
            echo "$b"
        fi
    done
}

n=0; hits=""
# 2026-11-20 补扫 ~/.pi/agent/skills：原来只扫 boss-agent/skills，
# 漏了 pi 技能目录 —— task-status-briefing 技能里写相对路径 workspace/data/...
# 导致 agent 一直往旧根写决策册，闸门却报「已部署脚本引用 0」。
for d in "$H/.coremem/scripts" "$H/.boss-agent/scripts" "$H/.boss-agent/skills" "$H/.pi/agent/skills"; do
    [ -d "$d" ] || continue
    while IFS= read -r b; do
        [ -n "$b" ] || continue
        n=$((n+1)); hits="$hits $b"
    done < <(_scan_refs "$d")
done
add "已部署脚本引用" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个${hits:+ →$hits}" \
    "活体目录里的硬编码旧根路径"
# 审计引用单独报数（不阻塞，但可见，避免豁免变成黑洞）
aud=0; audhits=""
for d in "$H/.coremem/scripts" "$H/.boss-agent/scripts"; do
    [ -d "$d" ] || continue
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        aud=$((aud+1)); audhits="$audhits $(basename "$f")"
    done < <(grep -rl 'OC-AUDIT-REF' "$d" --include='*.py' --include='*.sh' 2>/dev/null | grep -v '\.bak')
done
[ "$aud" -gt 0 ] && add "审计用途旧根引用(参考)" 0 "$aud 个${audhits}" \
    "行内标 OC-AUDIT-REF，仅用于核查旧副本，不参与运行"

# 仓库/技能副本：只报数，不阻塞（它们不参与运行；真要清理是另一件事）
rn=0
for d in "$H/projects" "$H/.agents/skills-openclaw"; do
    [ -d "$d" ] || continue
    c=$(_scan_refs "$d" | wc -l | tr -d ' ')
    rn=$((rn+c))
done
printf '  ℹ️  %-28s %s 个（仓库/技能副本，不参与运行，不阻塞）\n' "仓库副本引用" "$rn"

# ── 3. 是否有进程仍持有旧根文件句柄 ──
#   改 plist 不影响**已在运行**的常驻进程 —— 10-30 那次就漏了这点。
n=$(lsof +D "$OC" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')
who=$(lsof +D "$OC" 2>/dev/null | tail -n +2 | awk '{print $1"(pid "$2")"}' | sort -u | head -3 | tr '\n' ' ')
add "进程持有旧根句柄" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个 ${who}" \
    "需重启这些进程才会释放"

# ── 4. 近 N 天是否仍有写入（排除迁移备份与已知冷目录）──
# 2026-11-15 审计补：固定 3 天窗口会漏掉周更/月更任务。
# 改为「自迁移完成时刻起」——marker 文件存在就用它，否则回落 DAYS 天。
MARKER="$H/.coremem/health/openclaw-migration-done"
DAYS="${OC_RETIRE_QUIET_DAYS:-3}"
if [ -f "$MARKER" ]; then
    SINCE_REF="$MARKER"; SINCE_DESC="自迁移完成($(date -r "$MARKER" '+%m-%d %H:%M'))"
    n=$(find "$OC" -maxdepth 3 -newer "$MARKER" -type f 2>/dev/null | grep -v premigrate | wc -l | tr -d ' ')
else
    SINCE_DESC="近 ${DAYS} 天"
    n=$(find "$OC" -maxdepth 3 -newermt "-${DAYS} days" -type f 2>/dev/null | grep -v premigrate | wc -l | tr -d ' ')
fi
tops=$(find "$OC" -maxdepth 3 ${SINCE_REF:+-newer "$SINCE_REF"} ${SINCE_REF:--newermt "-${DAYS} days"} -type f 2>/dev/null \
    | grep -v premigrate | sed "s|$OC/||;s|/.*||" | sort | uniq -c | sort -rn \
    | head -3 | awk '{printf "%s(%s) ",$2,$1}')
# 2026-11-19 修判据：迁移以来累计写入数**永远无法归零**（mtime 不会消失），
# 拿它当阻塞项等于设一个不可能通过的闸门。目标是「旧根不再被写入」，
# 所以按**最近一次解耦修复时点**计量；历史累计数照样报出来，只是不阻塞。
# 这不是放宽判据：修复前的写入属预期，修复后的写入才说明仍有活跃依赖。
FIXMARK="$H/.coremem/health/openclaw-pathfix-done"
hist_n="$n"
if [ -f "$FIXMARK" ]; then
    n2=$(find "$OC" -maxdepth 3 -newer "$FIXMARK" -type f 2>/dev/null | grep -v premigrate | wc -l | tr -d ' ')
    tops2=$(find "$OC" -maxdepth 3 -newer "$FIXMARK" -type f 2>/dev/null \
        | grep -v premigrate | sed "s|$OC/||;s|/.*||" | sort | uniq -c | sort -rn \
        | head -3 | awk '{printf "%s(%s) ",$2,$1}')
    add "路径修复($(date -r "$FIXMARK" '+%m-%d %H:%M'))后写入" \
        "$([ "$n2" -gt 0 ] && echo 1 || echo 0)" "$n2 个文件 ${tops2}" \
        "修复后仍有写入说明还有活跃依赖"
    add "${SINCE_DESC}累计写入(参考)" 0 "$hist_n 个文件 ${tops}" \
        "历史累计，不阻塞；仅供回溯"
else
    add "${SINCE_DESC}写入" "$([ "$n" -gt 0 ] && echo 1 || echo 0)" "$n 个文件 ${tops}" \
        "仍有写入说明还有活跃依赖"
fi

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
bad=""; nbad=0; nrun=0; running=""; nbiz=0; biz=""
for p in "$LA"/*.plist.bak-full* "$LA"/*.plist.bak-sweep* "$LA"/*.plist.bak-wd*; do
    [ -f "$p" ] || continue
    L=$(basename "$p" | sed 's/\.plist\.bak.*//')
    line=$(launchctl list 2>/dev/null | awk -v l="$L" '$3==l{print $1" "$2}')
    [ -z "$line" ] && continue
    pid="${line%% *}"; rc="${line##* }"
    [ "$rc" = "0" ] && continue
    case " $KNOWN_BAD " in *" $L "*) continue;; esac
    # 2026-11-19 修判据：last exit code 是**历史**记录，不代表当前健康。
    # 实测 4 个 tunnel rc=255 来自 11-18 一次网络抖动，但此刻 state=running，
    # 且端到端验过（本地 18086/19221 → HTTP 401 服务活着、8096 → 200、
    # 远端 18089/18080 在听）。拿历史码当阻塞项会把已恢复的任务永远钉红。
    # 现在：**在跑（PID 是数字）→ 只报参考不阻塞**；没跑且 rc≠0 才阻塞。
    case "$pid" in
        ''|'-')
            # 2026-11-20 再修判据：rc≠0 有两种完全不同的含义 ——
            #   ① 迁移把任务搞坏了（Traceback / rc=127 找不到文件 / 路径错）→ 该阻塞
            #   ② 任务跑完了、如实报告「业务不健康」→ **不是迁移问题**
            # 实测 sender-health rc=1 是 render() 的健康判定（昨日成功率 43/163=26%），
            # 它正确完成并打印了报告；把它当迁移故障是又一次「代理信号代替真判据」。
            # 判据：看日志有无崩溃特征，而不是只看退出码。
            _log=$(launchctl print "gui/$(id -u)/$L" 2>/dev/null \
                   | awk '/stdout path|stderr path/{print $NF; exit}')
            _crash=0
            if [ "$rc" = "127" ] || [ "$rc" = "126" ]; then
                _crash=1     # 命令找不到/不可执行 = 典型迁移遗漏
            elif [ -n "$_log" ] && [ -f "$_log" ]; then
                if tail -40 "$_log" 2>/dev/null | grep -qE 'Traceback|No such file|not found|PermissionError|ModuleNotFound'; then
                    _crash=1
                fi
            else
                _crash=1     # 拿不到日志无法证明是业务判定 → 保守当阻塞
            fi
            if [ "$_crash" = "1" ]; then
                nbad=$((nbad+1)); bad="$bad $L(rc=$rc,崩溃/配置)"
            else
                nbiz=$((nbiz+1)); biz="$biz $L(rc=$rc,业务判定)"
            fi
            ;;
        *) nrun=$((nrun+1)); running="$running $L(历史rc=$rc,在跑)";;
    esac
done
add "迁移任务健康" "$([ "$nbad" -gt 0 ] && echo 1 || echo 0)" "${bad:-无当前失败}" \
    "排除迁移前即失败；在跑的历史非0码不阻塞"
[ "$nrun" -gt 0 ] && add "历史非0但在跑(参考)" 0 "$nrun 个${running}" \
    "已恢复运行；历史退出码仅供回溯"
[ "$nbiz" -gt 0 ] && add "非0但属业务判定(参考)" 0 "$nbiz 个${biz}" \
    "任务正常完成并如实报告业务不健康，非迁移故障"

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
