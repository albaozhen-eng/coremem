# coremem

**可以 git diff 的 Agent 记忆。** 六层记忆架构参考实现：纯文本文件就是 Source of Truth——你的 AI 记得什么，能读、能改、能版本化、能审计，还能**给人类看**。

## 为什么需要它

每一个认真做 AI Agent 的团队，最终都会遇到同一头怪兽：**失忆的 Agent**。上下文随会话蒸发；向量库变成没人看得懂的黑洞；SaaS 记忆层把数据锁进你 grep 不了的 API。coremem 下了一个相反的赌注：

- **文件即真相**——日记与事实是 Markdown/JSONL。`git diff` 你的 Agent 大脑，用 PR review 它，用你本来就有的备份恢复它。
- **人类在环的图谱**——一条命令把记忆图谱投影成 [Obsidian](https://obsidian.md) vault：frontmatter 供 Dataview 查询，`[[wikilinks]]` 就是边。Agent 的记忆第一次变成人类看得懂、点得进、打得了分的星云。
- **本地优先，不锁定**——语义层兼容自托管的 [PlugMem](https://github.com/TIMAN-group/PlugMem)（约 800 行服务代码），也可以不要它——L1 文件本身就是真相。
- **被打断也不会丢字**——所有失败写入落盘进 buffer、自动重放。生产环境经历断网、限流风暴、进程被杀之后：**零写入丢失**。

## 30 秒上手

```bash
pip install -r requirements.txt

python3 src/remember.py "结算系统迁到 v2，旧 API 保留到十月" --tags 决策,工程
python3 src/memory_recall.py all "结算 v2"
python3 src/remember.py --status

# 保留节目：把记忆图谱投影进 Obsidian
python3 src/vault_export.py --limit 400 --vault ~/memory-vault/Memory
# → Obsidian 打开 ~/memory-vault → 关系图谱 → 400 节点 + 370 个标签页
```

## 检索性能（真实图谱 4.9 万节点）

hybrid（余弦 × BM25）候选打分 numpy 向量化后：

| | p50 | p95 | top5 一致性 |
|---|---|---|---|
| 旧循环 | 18.4 ms | 19.4 ms | — |
| 向量化 | **1.4 ms** | **1.7 ms** | **100%** |

复现：`python3 scripts/bench_p0_2b.py`（只读、可离线跑）。

## 和 Mem0 / Zep / Letta 的差别

| | Mem0 / Zep / Letta | **coremem** |
|---|---|---|
| Source of Truth | 数据库 | **纯文本文件** |
| 人类审计 | 控制台/API | `git diff`、Obsidian 图谱、Dataview |
| 部署 | SaaS 或 服务+DB | 文件先行，语义引擎可选 |
| 写入失败 | 静默丢失 | **落盘 buffer + 自动重放** |
| 定位 | 记忆**服务** | 你拥有的记忆**架构** |

一句话：我们是「**文件即记忆**」流派的参考实现，需要向量时插一个自托管 PlugMem。

## 六层架构

| 层 | 介质 | 可变性 | 谁写入 |
|---|---|---|---|
| L0 会话日志 | `.jsonl` | 不可变 | 运行框架 |
| L1 每日笔记 | `.md` | 追加/合并 | `remember.py`（**唯一**写入口）|
| L2 语义索引 | 向量 | 由 L1 重建 | buffer 重放 / 实时写入 |
| L3 编译缓存 | `MEMORY.md` | 再生成 | 固化任务 |
| L4 （预留） | — | — | — |
| L5 知识投影 | Obsidian vault | 再生成 | `vault_export.py` |

跨机器部署是一等公民：`bash deploy/deploy.sh <仓库> <目标> <profile>` 会把默认值补丁成该机现网路径并跑冒烟自检；pre-commit 隐私闸门 `secret_scan` 保证任何个人数据进不了公开仓库——这套纪律先从我们自己做起。

## 项目状态

早期但诚实：架构、写入链路、降级 buffer、召回、固化、Obsidian 投影，全部在一个真实的小公司生产环境里每天运行（招聘、客服等 Agent 工作负载）。当前重点是文档、打包与 MCP server——见 [good-first-issues](https://github.com/albaozhen-eng/coremem/issues)。

## License

MIT

## 免责声明

coremem 存的是**记忆**。请把 L1/L5 当作和你的数据库一样敏感：Obsidian 投影仅供本机人审，**切勿**把真实 vault 同步到任何公开渠道。
