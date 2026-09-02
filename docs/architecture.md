# 六层记忆架构 (coremem)

## 概述

coremem 是一套 AI Agent 长期记忆的六层架构参考实现。数据从原始会话日志逐层向上编译，最终形成可检索、可演化的知识图谱。

## 六层数据流

```
┌──────────────────────────────────────────────────────────────────┐
│ L0: 会话日志                                                     │
│   - 格式: JSONL (每行一条消息)                                    │
│   - 内容: 原始对话记录 (role + content + timestamp)               │
│   - 特性: 不可变，仅追加                                          │
│   - 端口: 由 Agent 框架自动写入                                   │
│                                                                  │
│   ▼ 单向流                                                       │
│                                                                  │
│ L1: 每日笔记 (Daily Notes)                                       │
│   - 格式: Markdown (YYYY-MM-DD.md)                               │
│   - 内容: 人工整理 + 自动提取的关键事件和决策                      │
│   - 角色: Source of Truth (SoT) — 必须写入成功                    │
│   - 写入: remember.py 自动追加                                   │
│                                                                  │
│   ▼ 单向流                                                       │
│                                                                  │
│ L2: 语义索引 (Semantic Index)                                     │
│   - 后端: 兼容 PlugMem API 格式的向量数据库                        │
│   - 接口: POST /api/v1/graphs/{id}/memories                     │
│   - 查询: GET /api/v1/graphs/{id}/search?q=...&limit=N           │
│   - 特性: 语义搜索 + 标签分类 + 置信度评分                         │
│   - 降级: 写入失败 → buffer 落盘 → cron 30min 自动重试            │
│                                                                  │
│   ▼ 单向流                                                       │
│                                                                  │
│ L3: 编译缓存 (Compiled Truth)                                    │
│   - 格式: Markdown (MEMORY.md)                                   │
│   - 内容: AUTO 区块（自动编译快照）+ Timeline 区块（事件日志）      │
│   - 同步: memory_insight_scan.py 定期从 L2 拉取 → 去重 → 写入     │
│   - 特性: 时间戳裁决（同主题取最新）、类别分类（决策/配置/其他）    │
│                                                                  │
│   ▼ 单向流                                                       │
│                                                                  │
│ L4: (保留层，未来扩展)                                            │
│                                                                  │
│   ▼ 单向流                                                       │
│                                                                  │
│ L5: 知识投影 (Knowledge Projection)                               │
│   - 格式: Markdown (Obsidian vault)                              │
│   - 内容: 长期规划 / 个人偏好 / 工作流决策 / 项目快照              │
│   - 同步: 夜间 cron 自动复盘写入                                  │
│   - 特性: 跨项目永久记忆中枢，支持双向链接和标签系统               │
└──────────────────────────────────────────────────────────────────┘
```

## 核心设计原则

### 1. 单向数据流

数据只能从低层流向高层，不可逆。L0 是原始记录，L1 是 SoT，L2 是索引，L3 是缓存，L5 是投影。每一层都是上一层的"编译产物"。

### 2. 单点写入

所有记忆写入经过 `remember.py` 统一入口。调用方只需调用一次，脚本自动扇出到三层：
- ① daily log（必成，SoT）
- ② 语义索引（尽力，失败 → buffer）
- ③ 统一事实池（夜间提取到 L3 AUTO 区）

### 3. 扇出由代码完成

三层写入由 `remember.py` 在内部完成，调用方无需关心内部细节。任一层失败不阻断主流程（除 daily log）。

### 4. Compiled Truth + Timeline 格式

MEMORY.md 分为两个独立区块：
- **AUTO 区块**：由 `memory_insight_scan.py` 从 L2 自动编译，按决策/配置/其他分类
- **Timeline 区块**：人工维护的事件日志，按时间线记录

### 5. 写入降级与重试

语义索引写入失败时，自动降级到本地 buffer 文件（JSONL 格式）。cron 每 30 分钟自动重试，成功后清空 buffer。保证零丢失。

### 6. 时间戳裁决

当同主题存在多条事实时，以最新时间戳（`event_time` 优先，否则 `created_at`）为准。避免信息冲突。

## 检索管道

```
用户查询 → 关键词拆分 → 多 token OR 预筛
  ├── L0 会话日志 (grep 关键词 → 命中数排序)
  ├── L1 每日笔记 (grep 关键词 → 命中数排序)
  ├── L2 语义索引 (GET /search → 向量检索)
  ├── L3 编译缓存 (grep 关键词 → 命中数排序)
  └── L5 知识投影 (grep 关键词 → 命中数排序)
  → RRF 融合 → 分数重排 → 时效衰减 → Top-N 结果
```

## 检索性能

| 指标 | 数值 |
|------|------|
| 语义检索热路径 p50 | 0.7ms |
| 关键词预筛 p50 | 7.5ms |
| top-5 一致率 | 100% (黄金回归集 15 条) |
| 写入降级丢失 | 0 (buffer 兜底) |

## 依赖

- Python 3.10+
- 语义索引后端（兼容 PlugMem API 的 `/api/v1/graphs/{id}/memories` 和 `/search` 端点）
- Obsidian（可选，L5 知识投影）

## 配置

所有路径通过环境变量配置，默认值见各脚本顶部 `CONFIG` 区块：

| 环境变量 | 用途 | 默认值 |
|----------|------|--------|
| `MEMORY_HOME` | 记忆系统根目录 | `~/.coremem` |
| `MEM_SEMANTIC_BASE_URL` | 语义索引后端地址 | `http://127.0.0.1:<PORT>` |
| `MEM_SEMANTIC_API_KEY` | 语义索引 API Key | 空 |
| `MEM_SEMANTIC_GRAPH_ID` | 语义索引图 ID | `default` |
| `MEM_OBSIDIAN_VAULT` | Obsidian vault 路径 | `~/knowledge-base` |
| `MEM_DATA_DIR` | 数据目录（buffer、状态文件） | `$MEMORY_HOME/data` |
| `MEM_SESSIONS_DIR` | 会话日志目录 | `$MEMORY_HOME/sessions` |
| `MEM_DAILY_NOTES_DIR` | 每日笔记目录 | `$MEMORY_HOME/daily` |
| `MEM_BRIEFINGS_DIR` | 简报输出目录 | `$MEMORY_HOME/briefings` |