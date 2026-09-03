# coremem 部署指南（跨机器同步用）

## 模型
- **仓库（GitHub）= 唯一正统源**：平台无关，无隐私信息，可在任何电脑 `git pull`。
- **生产部署 = 打补丁的副本**：`deploy/deploy.sh` 把 `src/*.py` 拷到 `~/.openclaw/workspace/scripts/`，并把「默认值」就地改为该机器的现网路径（env 通道保留、env 优先）。
- 机器专属值放 `coremem.env`（已 gitignore，每台机器自持一份）。

## 新机器三步
```bash
git clone https://github.com/albaozhen-eng/coremem && cd coremem
bash deploy/deploy.sh                      # 自动备份旧脚本→部署→装 pre-commit 隐私钩子→语法自检
python3 ~/.openclaw/workspace/scripts/remember.py --status
```
默认补丁假设 OpenClaw 目录结构（`~/.openclaw/workspace-main/memory`、`agents/main/sessions`、graph `brian-main`）。
不同 graph/工作区名称时：改 `deploy/patch_deploy.py` 顶部补丁值，或用 cron `--command-env` 注入 `MEM_*`（见 `coremem.env` 模板）。

## 注意
- 生产脚本被 cron 直接引用路径（`~/.openclaw/workspace/scripts/*.py`），deploy 即热更新，无需重启网关。
- Windows 上 OpenClaw 工作区路径为 `%USERPROFILE%\.openclaw\...`，`Path.home()` 自适应。
- 发布纪律：任何个人路径/域名/手机号不得进入 src、docs、README——pre-commit 的 secret_scan 会拦。
