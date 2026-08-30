# sixmem

> **六层记忆架构的参考实现骨架**——设计哲学 + 工具脚本，用于 AI Agent 的长期记忆管理。
> 名字暂定 `sixmem`，可改。

## 一句话定位

一套**单向数据流、单点写入、扇出由代码完成**的六层记忆架构，让 AI Agent 拥有可追溯、可检索、可演化的长期记忆。

## 六层架构

```
 ┌──────────────────────────────────────────────────────┐
 │  L0  会话日志 (.jsonl)         原始对话记录，不可变     │
 │  L1  每日笔记 (daily notes)    Source of Truth (SoT)  │
 │  L2  语义索引 (PlugMem-compat)  向量检索 + 语义关系     │
 │  L3  编译缓存 (MEMORY.md)      Compiled Truth 快照     │
 │  L4  (保留层)                                           │
 │  L5  知识投影 (Obsidian)       跨项目永久记忆中枢       │
 └──────────────────────────────────────────────────────┘
```

**数据流向**：L0 → L1(SoT, 人工+自动) → L2(语义索引) → L3(编译缓存) → L5(知识投影)

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 写入记忆（统一入口）
python3 src/remember.py "今天完成了项目迁移" --tags 决策,工程

# 检索记忆（全层搜索）
python3 src/memory_recall.py all "项目迁移"

# 检查系统状态
python3 src/remember.py --status
```

## 设计原则

| 原则 | 说明 |
|------|------|
| **单向数据流** | L0→L1→L2→L3→L5，不可逆 |
| **单点写入** | `remember.py` 是唯一写入入口，调用方只需调一次 |
| **扇出由代码完成** | 一次写入自动分发到三层（daily log + 语义索引 + 事实池） |
| **Compiled Truth + Timeline** | MEMORY.md 分为 AUTO（自动编译快照）和 Timeline（事件日志）两个区块 |
| **写入降级** | 语义索引写入失败 → 自动降级到 buffer，cron 30min 自动重试 |
| **时间戳裁决** | 同主题事实取最新时间戳，避免信息冲突 |
| **失败不阻断** | 任一层写入失败不影响主流程（除 daily log，那是 SoT） |

## 性能数据

| 指标 | 优化前 | 优化后 | 说明 |
|------|--------|--------|------|
| 检索热路径 p50 | 7.5ms | 0.7ms | 关键词预筛 + 多 token OR |
| top-5 一致率 | — | 100% | 黄金回归集 15 条全通过 |
| 写入降级丢失 | — | 0 | buffer 机制 + 30min cron 重试 |

## 目录结构

```
sixmem/
├── README.md
├── LICENSE
├── CHANGELOG.md
├── .gitignore
├── .pre-commit-config.yaml
├── requirements.txt
├── docs/
│   └── architecture.md
├── src/
│   ├── remember.py              # 统一写入入口
│   ├── memory_write_buffer.py   # 写入缓冲区管理器
│   ├── memory_insight_scan.py   # 语义索引 → 编译缓存同步器
│   ├── morning_briefing.py      # 晨间记忆简报（纯确定性）
│   └── memory_recall.py         # 全层检索工具
└── scripts/
    └── secret_scan.py           # 隐私泄露扫描
```

## 依赖

- Python 3.10+
- 语义索引后端（兼容 PlugMem API 格式）
- Obsidian（可选，用于 L5 知识投影）

## 许可

MIT License — 详见 LICENSE 文件。