#!/bin/bash
# 把仓库脚本同步到活体（默认 ~/.coremem/scripts）。
#
# 2026-11-02：改为完全委托 deploy/deploy.sh，不再自己维护一份文件清单。
# 此前这里只同步 remember.py + memory_write_buffer.py，而 deploy.sh 同步 14 个，
# 两份清单各写各的 —— 2026-10-30 那次"生产热修被静默回退"就是这个模式的产物。
# 现在只有一处清单（deploy/patch_deploy.py 的 FILES + SH_FILES），两条命令行为一致。
#
# 委托后额外获得：部署前时间戳备份、漂移检查（生产有仓库没有的改动默认拒部署）、
# 语法自检、检索别名表种子安装。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
exec bash "$REPO/deploy/deploy.sh" "$REPO" "$HOME/.coremem/scripts" coremem-local "$@"
