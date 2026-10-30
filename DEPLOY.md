# coremem 部署指南（跨机器同步用）

## 模型
- **仓库（GitHub）= 唯一正统源**：平台无关，无隐私信息，可在任何电脑 `git pull`。
- **生产部署 = 打补丁的副本**：`deploy/deploy.sh` 把 `src/*.py` 拷到 `~/.coremem/scripts/`（`--no-patch`，仓库默认值即现网值）；旧宿主 OpenClaw 的路径补丁仅在使用 `--profile openclaw-scripts` 时套用。
- 机器专属值放 `coremem.env`（已 gitignore，每台机器自持一份）。

## 新机器三步
```bash
git clone https://github.com/albaozhen-eng/coremem && cd coremem
bash deploy/deploy.sh                      # 自动备份旧脚本→部署→装 pre-commit 隐私钩子→语法自检
python3 ~/.openclaw/workspace/scripts/remember.py --status
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

## 注意
- 生产脚本被 launchd 直接引用路径（`~/.coremem/scripts/*.py`），deploy 即热更新，无需重启服务。
- Windows 上 OpenClaw 工作区路径为 `%USERPROFILE%\.openclaw\...`，`Path.home()` 自适应。
- 发布纪律：任何个人路径/域名/手机号不得进入 src、docs、README——pre-commit 的 secret_scan 会拦。
