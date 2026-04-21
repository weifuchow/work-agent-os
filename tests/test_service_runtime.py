from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_monitor_job_tolerates_missing_stuck_key(monkeypatch):
    import apps.worker.scheduler as scheduler_mod
    import core.monitor as monitor_mod

    async def fake_check_running_tasks():
        return {"running": 0, "notified": 2, "inflight": []}

    monkeypatch.setattr(monitor_mod, "check_running_tasks", fake_check_running_tasks)

    await scheduler_mod.monitor_job()


def test_feishu_start_ws_reconnects_when_connection_exits(monkeypatch):
    from core.connectors.feishu import FeishuClient

    class StopLoop(Exception):
        pass

    client = FeishuClient.__new__(FeishuClient)
    client._handle_message_event = lambda data: None

    starts: list[int] = []
    sleep_calls = {"count": 0}

    monkeypatch.setattr(client, "_build_ws_event_handler", lambda: object())

    def fake_build_ws_client(_event_handler):
        idx = len(starts) + 1

        class FakeWsClient:
            def start(self_inner):
                starts.append(idx)

        return FakeWsClient()

    def fake_sleep(_delay_seconds: int):
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            raise StopLoop()

    monkeypatch.setattr(client, "_build_ws_client", fake_build_ws_client)
    monkeypatch.setattr(client, "_sleep_for_ws_reconnect", fake_sleep)

    with pytest.raises(StopLoop):
        client.start_ws()

    assert starts == [1, 2]
