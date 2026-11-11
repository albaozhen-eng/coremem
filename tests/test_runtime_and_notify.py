#!/usr/bin/env python3
"""Isolated tests for local wake, alert delivery and alert retry semantics."""
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import memory_heartbeat_sentinel as heartbeat
import notify
import omlx_runtime

FAILS = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


# Healthy local service must not invoke launchctl.
with patch.object(omlx_runtime, "_health", return_value={"status": "healthy", "engine_pool": {"loaded_count": 1}}), \
     patch.object(omlx_runtime, "_start_service", side_effect=AssertionError("must not restart a healthy service")):
    check("omlx 健康时零重启", omlx_runtime.ensure_omlx(timeout=1))

# Wake path is exercised entirely with mocks.
with patch.object(omlx_runtime, "_health", side_effect=[None, {"status": "healthy", "default_model": "test-model"}]), \
     patch.object(omlx_runtime, "_port_open", side_effect=[False, True]), \
     patch.object(omlx_runtime, "_start_service") as start, \
     patch.object(omlx_runtime, "_warmup", return_value=True):
    check("omlx 休眠时可经本地 launchd 适配器唤醒", omlx_runtime.ensure_omlx(timeout=2))
    check("唤醒只触发一次", start.call_count == 1)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.payload).encode()


class FakeOpener:
    def __init__(self):
        self.requests = []
    def open(self, request, timeout=30):
        self.requests.append(request)
        if "tenant_access_token" in request.full_url:
            return FakeResponse({"code": 0, "tenant_access_token": "test-token"})
        return FakeResponse({"code": 0})


opener = FakeOpener()
cfg = {"appId": "id", "appSecret": "secret", "allowUsers": ["recipient"]}
check("记忆断供标题不会被通知层吞掉", notify.send("outage", title="🔴 记忆链断供告警", config=cfg, opener=opener))
check("通知恰好走 token + message 两个请求", len(opener.requests) == 2)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    heartbeat.MEM_WS = root
    heartbeat.STATE_F = root / "state.json"
    heartbeat.CAPTURE_LOG = root / "capture.log"
    heartbeat.FACTS = root / "facts.jsonl"
    # check() 现在返回 4 元组（第四项 idle_note：「无输入」不是故障的说明），
    # 桩必须同步，否则 main() 解包直接 ValueError。
    heartbeat.check = lambda: ([("test_down", "test outage")], None, None, None)
    with patch.object(sys, "argv", ["memory_heartbeat_sentinel.py"]):
        with patch.object(heartbeat.notify, "send", return_value=False):
            heartbeat.main()
        state = heartbeat.load_state()
        check("发送失败不记去重时间", not state.get("alerts"))
        check("发送失败下轮立即可重试", heartbeat.should_alert(state, "test_down"))
        with patch.object(heartbeat.notify, "send", return_value=True):
            heartbeat.main()
        state = heartbeat.load_state()
        check("发送成功才记去重时间", "test_down" in state.get("alerts", {}))
        check("成功后进入去重窗口", not heartbeat.should_alert(state, "test_down"))

print("\n全部通过" if not FAILS else f"\n失败: {FAILS}")
sys.exit(1 if FAILS else 0)
