# coremem 部署指南（跨机器同步用）

## 一键安装（新机器，零改动）

```bash
git clone https://github.com/albaozhen-eng/coremem && cd coremem
bash deploy/install.sh --deepseek-key sk-xxx     # 云端通道；本机有 omlx 用 --local-model
python3 deploy/scheduler.py status               # 7 个定时任务应全部在列
```

它把下面"新机器三步"的内容全包了，并且**不需要任何路径替换**——默认布局就是
`~/.coremem`。只有历史宿主目录（如 `~/.openclaw`、`~/.plugmem/cli`）才需要 profile：

| 场景 | 命令 |
|---|---|
| 全新机器 | `bash deploy/install.sh` |
| 历史 OpenClaw 工作区 | `bash deploy/deploy.sh REPO DEST openclaw-scripts` |
| plugmem-suite 独立部署 | `bash deploy/deploy.sh REPO ~/.plugmem/cli plugmem-suite-cli` |

调度器也可以单独装卸：`python3 deploy/scheduler.py install|status|uninstall`
（macOS 走 launchd，Linux 走用户 crontab）。

### MCP server（可选，pi 等 MCP 客户端用）

```bash
bash deploy/install-mcp.sh            # 装到 ~/.plugmem/mcp，含依赖安装+接口回归
```

MCP 与 CLI 同仓库同源（`mcp/`）。**每次 deploy.sh 升级 CLI 后跑一遍回归**，
防接口漂移：`node ~/.plugmem/mcp/test-mcp-fix.mjs`。详见 `mcp/README.md`。

## 模型
- **仓库（GitHub）= 唯一正统源**：平台无关，无隐私信息，可在任何电脑 `git pull`。
- **生产部署 = 打补丁的副本**：`deploy/deploy.sh` 把 `src/*.py` 拷到 `~/.coremem/scripts/`（`--no-patch`，仓库默认值即现网值）；旧宿主 OpenClaw 的路径补丁仅在使用 `--profile openclaw-scripts` 时套用。
- 机器专属值放 `coremem.env`（已 gitignore，每台机器自持一份）。

## 新机器三步
```bash
git clone https://github.com/albaozhen-eng/coremem && cd coremem
bash deploy/deploy.sh                      # 自动备份旧脚本→部署→装 pre-commit 隐私钩子→语法自检
python3 ~/.coremem/scripts/remember.py --status
python3 tests/run_all.py                      # 统一运行脚本式回归测试
```
默认部署假设 coremem 自有目录结构（`~/.coremem/memory`、`~/.coremem/sessions`、graph `brian-main`）。
不同 graph/工作区名称时：改 `deploy/patch_deploy.py` 顶部补丁值，或用 cron `--command-env` 注入 `MEM_*`（见 `coremem.env` 模板）。

## 部署前的漂移护栏（2026-10-30 事故后新增）

```bash
bash deploy/deploy.sh                # 默认先比对，有漂移即中止（退出码 3）
bash deploy/deploy.sh --force        # 确需覆盖生产改动时才用
```

**事故记录 · 2026-10-30**：生产 `~/.openclaw/workspace/scripts/memory_recall.py`
已经过多轮热修（行级 IDF 检索、别名扩展、查询归一化、`MAX_TOKENS=12`、
「中文+英文」分词修复），而仓库 `src` 停更；一次常规 deploy 把它们全部回退，
`memory_health` 的检索自检当场变红（`A分词未展开`），`memory_insight_scan.py`
的 AUTO 区块退役守卫也会一并丢失。处置：把生产版本回灌仓库（提交 `92147c4`），
并在 `patch_deploy.py` 增加 `--check-drift`、`deploy.sh` 默认启用。

## 模型通道（加/换模型不用改代码）

采集与门控用的 LLM 通道完全由 `~/.coremem/config/providers.json`（或
`MEM_PROVIDERS_CONFIG` 指过去的那份，0600、不进 git）决定：

```json
{
  "capture_channels": ["omlx", "bailian:qwen3.8-flash", "bailian:qwen4-max-preview",
                       "scnet-tp", "deepseek"],
  "providers": {
    "omlx":     { "enabled": false },
    "bailian":  { "baseUrl": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  "apiKey": "sk-…", "models": ["qwen3.8-flash", "qwen4-max-preview"],
                  "extra": { "enable_thinking": false }, "timeout": 240 },
    "scnet-tp": { "apiKey": "sk-tp-…", "baseUrl": "https://api.scnet.cn/api/llm/v1",
                  "models": ["Qwen3.8-Max", "Kimi-K3"] },
    "deepseek": { "apiKey": "sk-…" }
  }
}
```

- `capture_channels` 决定顺序与降级路径；`provider` 或 `provider:model` 都可写，未列出的模型不挂。
- 没列 `capture_channels` 就用内置默认顺序（omlx → bailian → scnet-tp → deepseek），
  云端 provider 只在拿到 key 时才挂链（没 key 挂了也是每轮白撞一次 401）。
- 推理模型记得关思考链（百炼用 `extra.enable_thinking=false`；本地 omlx 代码里已自动带
  `chat_template_kwargs`）：否则 `max_tokens` 会被 reasoning 吃空 → empty content → 判失败降级。
- 账户级错误（401/402/403）不重试、直接跳下一通道；其余错误每通道一次重试。
- 断网演练：`MEMORY_CAPTURE_SKIP_CHANNELS="bailian:qwen3.8-flash,scnet-tp"` 可跳过指定通道。
- 实测参考（2026-11-03）：百炼 flash ≈1.2s；qwen4-max ≈50s 且会 429（宜作兜底不宜当主力）；
  DeepSeek 官网 ≈1s；scnet 订阅过期时三条 403 秒跳，总耗时仍 ≈1s。

## 注意
- 生产脚本被 launchd 直接引用路径（`~/.coremem/scripts/*.py`），deploy 即热更新，无需重启服务。
- Windows 上 OpenClaw 工作区路径为 `%USERPROFILE%\.openclaw\...`，`Path.home()` 自适应。
- 发布纪律：任何个人路径/域名/手机号不得进入 src、docs、README——pre-commit 的 secret_scan 会拦。

## OpenClaw 解耦现状（2026-11-03）

- **日常采集只读 pi**：`memory_capture.py` 默认 `--sources pi`，不再读取 OpenClaw 的会话库、模型配置或唤醒脚本。
- **历史归档显式可选**：`--sources openclaw|all` 才加载只读适配器 `openclaw_archive.py`；一次性回填使用 `migrate_openclaw_archive.py`，不会修改归档。
- **omlx 本地管理**：`omlx_runtime.py` 负责健康检查、launchd 唤醒和热加载；端点优先级为 `MEM_OMLX_BASE_URL` → `~/.boss-agent/llm-policy.json` → `MEM_PROVIDERS_CONFIG` → 本机默认值。
- **告警独立**：`notify.py` 直接使用本机飞书配置，不再复用招聘通知脚本。后者会按标题屏蔽“记忆链断供”，曾导致真实告警被当作成功吞掉。发送失败不写去重时间，下轮可立即重试。
- **部署清单一处维护**：新增/退役部署文件只改 `deploy/files.manifest`；Python 与 shell 文件均纳入漂移检查。

### 历史回填示例

```bash
# 先限定时间窗并 dry-run（不写记忆，也不更新 processed 状态）
python3 src/migrate_openclaw_archive.py --since 2026-08-12 --until '2026-08-12 23:59' --dry-run
# 明确需要重处理时才加 --force；正常回填依赖 processed 状态防重
python3 src/migrate_openclaw_archive.py --hours 24 --max-sessions 3
```

删除旧宿主目录不是部署步骤。若确需退役，先完成历史回填并保留备份，再单独评估其他工具的依赖。
