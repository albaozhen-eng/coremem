#!/bin/bash
# PlugMem suite 启动器：密钥放在 ~/.coremem/plugmem/env（0600），不写进 plist。
set -euo pipefail
set -a
. "$HOME/.coremem/plugmem/env"
set +a
export PYTHONPATH="$HOME/.coremem/plugmem/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$HOME/.plugmem/service/.venv/bin/python" -m uvicorn \
  plugmem.api.app:app --host 127.0.0.1 --port 8089
