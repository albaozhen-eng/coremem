# 定时任务包装脚本

这些是 launchd 直接执行的入口。安装时复制到 `~/.coremem/scripts/`，
plist 的 `ProgramArguments` 指向那里。

| 脚本 | 频率 | 作用 |
|---|---|---|
| `l2-kis-run.sh` | 每月 1 日 07:40 | L2 已知项评测（探针 >8 天无报告判黄，必须有生产者）|
| `l3-schema-induct.sh` | 每周日 07:30 | L3 归纳，**只产草稿**，人工确认后才进 MEMORY.md |
| `kb-consistency-gate.sh` | 每日 06:40 | 知识库事实一致性；有冲突返回 1（**不是故障**）|
| `tiered-backup.sh` | 每日 02:30 | 分级备份：daily / checkpoint / weekly / monthly |

## 三个踩过的坑（改这些脚本前必读）

1. **必须加载 `coremem.env`**。脚本路径靠 `MEM_*` 环境变量决定；不加载就用默认值。
   迁移时曾因此出现「取近 7 天：0 条事实」却 `rc=0` 的假成功。

2. **`/bin/bash` 在 macOS 是 3.2**，没有 `mapfile`。用
   `while IFS= read -r ... done < <(...)` 代替。

3. **被进程替换捕获的函数，日志必须走 stderr**。否则日志行会变成数组元素
   （在 `existing_sources` 上实测过，会把日志当成备份源传给 tar）。

## 退出码语义

`kb-consistency-gate.sh` 的 `rc=1` 表示**发现了冲突**，是正常结果，不是脚本故障。
判断健康要看心跳与日志内容，不能只看退出码。
