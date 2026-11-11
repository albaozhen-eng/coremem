#!/bin/bash
# OpenClaw 分级备份方案
# 每天增量 + 每两天检查 + 每周全量 + 每月归档
# 作者: OpenClaw Assistant
# 创建日期: 2026-03-12

set -e

# 配置
BACKUP_ROOT="${HOME}/.coremem/backups/tiered"
LOG_DIR="${HOME}/.coremem/logs"
WORKSPACE_DIR="${HOME}/.openclaw/workspace"
SCRIPTS_DIR="${HOME}/.openclaw/scripts"
KNOWLEDGE_DIR="${HOME}/knowledge-base"
DATE=$(date +%Y%m%d)
DATETIME=$(date +%Y%m%d-%H%M%S)
DAY_OF_WEEK=$(date +%u)  # 1=周一, 7=周日
DAY_OF_MONTH=$(date +%d)

# 确保目录存在
mkdir -p "${BACKUP_ROOT}"/{daily,weekly,monthly,checkpoints}
mkdir -p "${LOG_DIR}"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${LOG_DIR}/backup-${DATE}.log"
}

# 错误处理
# 2026-11-11 审计修：tar 只要有一个源不存在就 exit 1，配合 `|| error_exit`
# 会把「归档其实已生成、只是某个源缺失」判成整体失败（实测 .coremem/config 不存在，
# 于是 checkpoint 每次 rc=1，而 3.3GB 归档是好的）。这是「假失败」。
# 现在先过滤掉不存在的源，并在归档后校验产物非空。
# 注意：本函数 stdout 会被进程替换捕获成数组，日志必须走 stderr，
# 否则日志行会变成一个"源"传给 tar（实测过）。
existing_sources() {
    local out=()
    for p in "$@"; do
        if [ -e "${HOME}/${p}" ]; then out+=("$p"); else log "  ⏭️ 跳过不存在的源: $p" >&2; fi
    done
    printf '%s\n' "${out[@]}"
}

# 归档后校验：文件存在且 > 1KB 才算成功（不只看 tar 退出码）
assert_archive() {
    local f="$1"
    if [ ! -s "$f" ]; then return 1; fi
    local sz; sz=$(wc -c < "$f" | tr -d ' ')
    [ "$sz" -gt 1024 ]
}

error_exit() {
    log "❌ 错误: $1"
    exit 1
}

# ==================== 每天增量备份 (Git) ====================
daily_backup() {
    log "📅 开始每日增量备份 (Git)..."
    
    cd "${WORKSPACE_DIR}" || error_exit "无法进入工作目录"
    
    # 检查 git 仓库
    if [ ! -d ".git" ]; then
        log "初始化 Git 仓库..."
        git init
        git config user.email "backup@openclaw.local"
        git config user.name "Backup Bot"
    fi
    
    # 添加所有变更
    git add -A
    
    # 提交（如果有变更）
    if git diff --cached --quiet; then
        log "✅ 没有变更需要提交"
    else
        git commit -m "Daily backup: ${DATETIME}" || true
        log "✅ 每日增量备份完成"
    fi
    
    # 推送到远程（如果配置了）
    if git remote get-url origin &>/dev/null; then
        git push origin main 2>&1 | tee -a "${LOG_DIR}/backup-${DATE}.log" || log "⚠️ 推送失败，仅本地备份"
    fi
}

# ==================== 每两天检查点备份 ====================
checkpoint_backup() {
    log "🔍 开始检查点备份..."
    
    local checkpoint_file="${BACKUP_ROOT}/checkpoints/checkpoint-${DATE}.tar.gz"
    
    # 创建检查点（包含完整状态）
    _SRC=()
    while IFS= read -r _s; do [ -n "$_s" ] && _SRC+=("$_s"); done < <(existing_sources ".coremem/memory" ".coremem/data" ".coremem/MEMORY.md" ".coremem/scripts" ".coremem/config" ".openclaw/workspace" ".openclaw/scripts" ".openclaw/config" "projects/my-wiki" "knowledge-base")
    tar -czf "${checkpoint_file}" \
        --exclude='*.log' \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*venv*' \
        --exclude='.git' \
        --exclude='dist' \
        --exclude='.vitepress/cache' \
        -C "${HOME}" "${_SRC[@]}" 2>/dev/null
    assert_archive "${checkpoint_file}" || error_exit "创建检查点失败"
    
    log "✅ 检查点备份完成: ${checkpoint_file}"
    
    # 保留最近 3 个检查点
    ls -t "${BACKUP_ROOT}/checkpoints/"/*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
    log "🧹 已清理旧检查点，保留最近 3 个"
}

# ==================== 每周全量备份 ====================
weekly_backup() {
    log "📦 开始每周全量备份..."
    
    local week_num=$(date +%Y-W%U)
    local weekly_file="${BACKUP_ROOT}/weekly/weekly-${week_num}.tar.gz"
    
    # 创建全量备份
    _SRC=()
    while IFS= read -r _s; do [ -n "$_s" ] && _SRC+=("$_s"); done < <(existing_sources ".coremem" ".openclaw" "knowledge-base")
    tar -czf "${weekly_file}" \
        --exclude='*.log' \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*venv*' \
        --exclude='.git' \
        --exclude='.openclaw/backups' \
        --exclude='.openclaw/browser' \
        --exclude='.openclaw/npm' \
        --exclude='.openclaw/artifacts' \
        --exclude='.openclaw/tmp' \
        --exclude='.coremem/backups' \
        -C "${HOME}" "${_SRC[@]}" 2>/dev/null
    assert_archive "${weekly_file}" || error_exit "创建每周备份失败"
    
    local size=$(du -h "${weekly_file}" | cut -f1)
    log "✅ 每周全量备份完成: ${weekly_file} (${size})"
    
    # 保留最近 4 周
    ls -t "${BACKUP_ROOT}/weekly/"/*.tar.gz 2>/dev/null | tail -n +5 | xargs -r rm -f
    log "🧹 已清理旧周备份，保留最近 4 周"
}

# ==================== 每月归档备份 ====================
monthly_backup() {
    log "📚 开始每月归档备份..."
    
    local month=$(date +%Y-%m)
    local monthly_file="${BACKUP_ROOT}/monthly/monthly-${month}.tar.gz"
    
    # 创建月度归档（包含 Git 历史）
    _SRC=()
    while IFS= read -r _s; do [ -n "$_s" ] && _SRC+=("$_s"); done < <(existing_sources ".coremem" ".openclaw" "knowledge-base")
    tar -czf "${monthly_file}" \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*venv*' \
        --exclude='.git' \
        --exclude='.openclaw/backups' \
        --exclude='.openclaw/browser' \
        --exclude='.openclaw/npm' \
        --exclude='.openclaw/artifacts' \
        --exclude='.openclaw/tmp' \
        -C "${HOME}" "${_SRC[@]}" 2>/dev/null
    assert_archive "${monthly_file}" || error_exit "创建月度备份失败"
    
    local size=$(du -h "${monthly_file}" | cut -f1)
    log "✅ 每月归档备份完成: ${monthly_file} (${size})"
    
    # 保留最近 3 个月
    ls -t "${BACKUP_ROOT}/monthly/"/*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
    log "🧹 已清理旧月备份，保留最近 3 个月"
    
    # 生成备份报告
    generate_report
}

# ==================== 生成备份报告 ====================
generate_report() {
    local report_file="${LOG_DIR}/backup-report-${month}.md"
    
    cat > "${report_file}" << EOF
# OpenClaw 备份报告 - $(date +%Y年%m月)

生成时间: $(date '+%Y-%m-%d %H:%M:%S')

## 备份统计

### 存储使用情况
\`\`\`
$(du -sh ${BACKUP_ROOT}/* 2>/dev/null)
\`\`\`

### 备份文件列表

#### 检查点备份 (最近15个)
$(ls -lh ${BACKUP_ROOT}/checkpoints/*.tar.gz 2>/dev/null | tail -5)

#### 周备份 (最近8周)
$(ls -lh ${BACKUP_ROOT}/weekly/*.tar.gz 2>/dev/null | tail -3)

#### 月备份 (最近12个月)
$(ls -lh ${BACKUP_ROOT}/monthly/*.tar.gz 2>/dev/null | tail -3)

## 备份内容

- ✅ OpenClaw Workspace
- ✅ Skills 和 Scripts
- ✅ Memory 文件
- ✅ 知识库 (knowledge-base)
- ✅ 配置文件

## 恢复指南

### 从检查点恢复
\`\`\`bash
cd ~
tar -xzf ${BACKUP_ROOT}/checkpoints/checkpoint-YYYYMMDD.tar.gz
\`\`\`

### 从周备份恢复
\`\`\`bash
cd ~
tar -xzf ${BACKUP_ROOT}/weekly/weekly-YYYY-WNN.tar.gz
\`\`\`

### 从 Git 恢复 (增量)
\`\`\`bash
cd ~/.openclaw/workspace
git log --oneline  # 查看历史
git checkout <commit-hash>  # 恢复到指定版本
\`\`\`

---
*由 OpenClaw 自动备份系统生成*
EOF

    log "📊 备份报告已生成: ${report_file}"
}

# ==================== 主程序 ====================
# 运行模式（2026-10-03 修复，此前 main "$@" 忽略参数、无法跑单档）：
#   auto       每天增量 + 奇数日 checkpoint + 周日 weekly + 每月1日 monthly（原逻辑，缺省）
#   daily      仅增量（git commit）
#   checkpoint 仅检查点
#   weekly     仅周全量
#   monthly    仅月归档
main() {
    local mode="${1:-auto}"
    log "========================================"
    log "🚀 OpenClaw 分级备份开始（模式: ${mode}）"
    log "========================================"

    case "${mode}" in
        daily)      daily_backup ;;
        checkpoint) checkpoint_backup ;;
        weekly)     weekly_backup ;;
        monthly)    monthly_backup ;;
        auto|"")
            # 每天执行：增量备份
            daily_backup

            # 每两天执行：检查点备份（奇数天）
            if [ $((10#${DAY_OF_MONTH} % 2)) -eq 1 ]; then
                checkpoint_backup
            fi

            # 每周日执行：全量备份
            if [ "${DAY_OF_WEEK}" -eq 7 ]; then
                weekly_backup
            fi

            # 每月1日执行：归档备份
            if [ "${DAY_OF_MONTH}" -eq 01 ]; then
                monthly_backup
            fi
            ;;
        *)
            echo "用法: $0 [daily|checkpoint|weekly|monthly|auto]" >&2
            exit 2
            ;;
    esac

    log "========================================"
    log "✅ 分级备份完成"
    log "========================================"
}

# 运行主程序
main "$@"
