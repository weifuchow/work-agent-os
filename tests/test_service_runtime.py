from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import models.db  # noqa: F401
from models.db import Message, PipelineStatus


@pytest.mark.asyncio
async def test_monitor_job_tolerates_missing_stuck_key(monkeypatch):
    import apps.worker.scheduler as scheduler_mod
    import core.monitor as monitor_mod

    async def fake_check_running_tasks():
        return {"running": 0, "notified": 2, "inflight": []}

    monkeypatch.setattr(monitor_mod, "check_running_tasks", fake_check_running_tasks)

    await scheduler_mod.monitor_job()


@pytest.mark.asyncio
async def test_monitor_marks_stale_inflight_message_failed(monkeypatch, tmp_path):
    import core.monitor as monitor_mod

    db_file = tmp_path / "monitor.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    old_time = datetime.now() - timedelta(minutes=monitor_mod.STALE_INFLIGHT_FAIL_MINUTES + 1)
    async with factory() as db:
        msg = Message(
            platform="feishu",
            platform_message_id="om_stale_monitor_001",
            chat_id="oc_stale",
            sender_id="ou_stale",
            sender_name="tester",
            message_type="text",
            content="stale task",
            received_at=old_time,
            created_at=old_time,
            pipeline_status=PipelineStatus.classifying,
        )
        db.add(msg)
        await db.commit()
        message_id = msg.id

    monkeypatch.setattr(monitor_mod, "async_session_factory", factory)

    async def fail_if_notified(_msg):
        raise AssertionError("stale in-flight messages must not send thinking notifications")

    monkeypatch.setattr(monitor_mod, "_notify_thinking", fail_if_notified)

    counts = await monitor_mod.check_running_tasks()

    assert counts["stuck"] == 1
    assert counts["notified"] == 0

    async with factory() as db:
        refreshed = await db.get(Message, message_id)
        assert refreshed is not None
        assert refreshed.pipeline_status == PipelineStatus.failed
        assert "stale in-flight pipeline state" in refreshed.pipeline_error
        assert refreshed.processed_at is not None

    await engine.dispose()


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
