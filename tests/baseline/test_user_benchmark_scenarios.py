from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = ROOT / "tests"
for item in (ROOT, TESTS_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from builders.messages import create_schema, fetch_all, fetch_one, insert_message
from core.agents.runner import AgentRunner
from core.app.result_handler import ResultHandler
from core.artifacts.workspace import WorkspacePreparer
from core.connectors.feishu import FeishuChannelPort
from core.deps import CoreDependencies
from core.pipeline import configure_dependencies_for_tests, process_message
from core.ports import AgentResponse
import core.projects as projects_mod
from core.repositories import Repository
from core.sessions.service import SessionService
from fakes.ports import FakeAgentPort, FakeChannelPort, FakeFilePort, FixedClock, read_workspace_json

ONES_150552_DIRECT_MESSAGE = (
    "#150552 【3.52.0】【订单】：订单取消成功，但是订单未与小车解绑"
    "（现象，小车挂着订单，同时在执行其他订单）\n"
    "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"
)


@pytest.fixture(autouse=True)
def disable_real_ones_prefetch(monkeypatch):
    monkeypatch.setenv("WORK_AGENT_DISABLE_ONES_PREFETCH", "1")

    async def skip_real_ones_prefetch(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "core.app.message_processor.prepare_ones_intake",
        skip_real_ones_prefetch,
    )


@pytest.fixture
async def harness(tmp_path):
    db_file = tmp_path / "user_benchmarks.sqlite"
    await create_schema(db_file)

    repository = Repository(db_file)
    sessions = SessionService(repository)
    clock = FixedClock()
    channel = FakeChannelPort(thread_id="omt_benchmark")
    file_port = FakeFilePort()
    agent_port = FakeAgentPort()
    workspaces = WorkspacePreparer(file_port, workspace_root=tmp_path / "workspaces")
    agents = AgentRunner(agent_port)
    result_handler = ResultHandler(repository=repository, channel_port=channel, clock=clock)
    deps = CoreDependencies(
        repository=repository,
        sessions=sessions,
        workspaces=workspaces,
        agents=agents,
        result_handler=result_handler,
        clock=clock,
    )
    configure_dependencies_for_tests(deps)
    try:
        yield {
            "db_file": db_file,
            "agent_port": agent_port,
            "channel": channel,
            "workspace_root": tmp_path / "workspaces",
        }
    finally:
        configure_dependencies_for_tests(None)


@pytest.mark.asyncio
async def test_chat_001_multiturn_idle_chat_reuses_session_without_project(harness):
    """Benchmark 1: 闲聊必须复用飞书话题 session，且不能绑定业务项目。"""

    db_file = harness["db_file"]
    harness["agent_port"].results.extend(
        [
            _reply_result(
                "可以，今天先按一个轻量节奏推进。",
                agent_session_id="chat-agent-session",
                skill_trace=[{"skill": "general-chat", "reason": "idle chat"}],
            ),
            _reply_result(
                "延续刚才的话题，不需要进入项目分析。",
                agent_session_id="chat-agent-session",
                skill_trace=[{"skill": "general-chat", "reason": "same casual thread"}],
            ),
        ]
    )

    first_id = await insert_message(
        db_file,
        platform_message_id="om_chat_001_t1",
        content="早上好，今天辛苦了，推荐一个轻松点的工作节奏",
    )
    await process_message(first_id)
    first_msg, first_session = await _message_and_session(db_file, first_id)

    assert first_msg["pipeline_status"] == "completed"
    assert first_session["project"] == ""
    assert first_session["thread_id"] == "omt_benchmark"
    assert first_session["agent_session_id"] == "chat-agent-session"

    second_id = await insert_message(
        db_file,
        platform_message_id="om_chat_001_t2",
        content="继续刚才的话题，下午怎么安排",
        thread_id=first_session["thread_id"],
    )
    await process_message(second_id)
    second_msg, second_session = await _message_and_session(db_file, second_id)

    assert second_msg["pipeline_status"] == "completed"
    assert second_msg["session_id"] == first_msg["session_id"]
    assert second_session["project"] == ""
    assert second_session["agent_session_id"] == "chat-agent-session"
    assert len(harness["channel"].calls) == 2

    second_workspace = harness["agent_port"].calls[1].workspace_path
    history = read_workspace_json(second_workspace, "input/history.json")
    assert [item["role"] for item in history] == ["user", "assistant", "user"]
    assert "早上好" in history[0]["content"]
    assert "轻量节奏" in history[1]["content"]

    skill_names = await _completed_skill_names(db_file)
    assert skill_names == {"general-chat"}


@pytest.mark.parametrize(
    ("content", "expected_project"),
    [
        ("allspark 项目里的 SQL Server 数据源怎么配置", "allspark"),
        ("RIOT3.0 订单取消后小车还挂着订单，帮我看下", "allspark"),
        ("RIOT2.0 现场 DeadLockDetector 报警，需要排查日志", "riot-standalone"),
        ("RIOT1.0 / FMS 车队管理系统自动充电异常", "fms-java"),
    ],
)
@pytest.mark.asyncio
async def test_project_route_matrix_binds_expected_project(harness, content, expected_project):
    """Benchmark 2: 项目路由别名必须通过 session_patch 稳定落库。"""

    db_file = harness["db_file"]
    harness["agent_port"].results.append(
        _reply_result(
            f"已进入 {expected_project} 项目上下文。",
            project=expected_project,
            topic="项目路由确认",
            agent_session_id=f"{expected_project}-agent-session",
            skill_trace=[
                {"skill": "riot-log-triage", "reason": "RIOT/FMS project issue"},
            ],
        )
    )

    msg_id = await insert_message(
        db_file,
        platform_message_id=f"om_route_{expected_project.replace('-', '_')}",
        content=content,
    )
    await process_message(msg_id)
    msg, session = await _message_and_session(db_file, msg_id)

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == expected_project
    assert session["topic"] == "项目路由确认"
    assert session["agent_session_id"] == f"{expected_project}-agent-session"
    assert harness["channel"].calls[0]["reply"].content == f"已进入 {expected_project} 项目上下文。"

    skill_registry = harness["agent_port"].calls[0].skill_registry
    assert "riot-log-triage" in _registry_names(skill_registry)


@pytest.mark.asyncio
async def test_ones_150552_analysis_confirmation_requires_evidence_before_root_cause(harness):
    """Benchmark 3: ONES 分析必须识别工单，并在证据不足时输出低置信补料。"""

    db_file = harness["db_file"]
    reply_text = (
        "## 当前结论\n"
        "ONES #150552 / NbJXtiyGP7R4vYnF 已识别为 RIOT3 allspark 订单链路问题。"
        "现象是 3.52.0 订单取消成功，但订单未与小车解绑。\n\n"
        "## 置信度\n"
        "低置信：当前只能确认现象和版本线索，还不能定根因。\n\n"
        "## 缺口\n"
        "还缺订单ID、车辆解绑日志、取消回调日志和问题时间窗，补齐后再验证解绑门禁。"
    )
    harness["agent_port"].results.append(
        _reply_result(
            reply_text,
            reply_type="feishu_card",
            payload=_ones_150552_card_payload(),
            project="allspark",
            topic="ONES #150552 订单取消未解绑",
            intent="needs_input",
            agent_session_id="ones-150552-session",
            skill_trace=[
                {"skill": "ones", "reason": "message contains ONES task link"},
                {"skill": "riot-log-triage", "reason": "order execution issue"},
                {"skill": "feishu_card_builder", "reason": "rendered feishu_card summary"},
            ],
        )
    )

    msg_id = await insert_message(
        db_file,
        platform_message_id="om_ones_150552",
        content=ONES_150552_DIRECT_MESSAGE,
    )
    await process_message(msg_id)
    msg, session = await _message_and_session(db_file, msg_id)
    bot_reply = await _bot_reply(db_file, "om_ones_150552")
    raw_payload = json.loads(bot_reply["raw_payload"])
    workspace_message = read_workspace_json(harness["agent_port"].calls[0].workspace_path, "input/message.json")

    assert msg["pipeline_status"] == "completed"
    assert workspace_message["content"] == ONES_150552_DIRECT_MESSAGE
    assert session["project"] == "allspark"
    assert session["topic"] == "ONES #150552 订单取消未解绑"
    assert bot_reply["message_type"] == "feishu_card"
    assert raw_payload["reply"]["intent"] == "needs_input"
    assert raw_payload["reply"]["type"] == "feishu_card"
    assert raw_payload["reply"]["payload"]["schema"] == "2.0"
    assert _has_card_table(raw_payload["reply"]["payload"])

    content = bot_reply["content"]
    for expected in ("#150552", "NbJXtiyGP7R4vYnF", "3.52.0", "订单取消成功", "未与小车解绑"):
        assert expected in content
    assert "低置信" in content
    assert "缺口" in content
    assert "高置信" not in content

    card_text = _card_text(raw_payload["reply"]["payload"])
    for expected in ("#150552", "3.52.0", "低置信", "订单ID", "车辆解绑日志"):
        assert expected in card_text

    assert await _completed_skill_names(db_file) == {
        "ones",
        "riot-log-triage",
        "feishu_card_builder",
    }


@pytest.mark.asyncio
async def test_ones_150552_pipeline_sends_feishu_card_through_mocked_feishu_client(tmp_path):
    """Full-chain benchmark: mock only Feishu API and verify outgoing interactive card."""

    db_file = tmp_path / "mocked_feishu.sqlite"
    await create_schema(db_file)

    repository = Repository(db_file)
    sessions = SessionService(repository)
    clock = FixedClock()
    feishu_client = RecordingFeishuClient(thread_id="omt_mock_feishu_150552")
    channel = FeishuChannelPort(client=feishu_client)
    file_port = FakeFilePort()
    agent_port = FakeAgentPort(
        _reply_result(
            (
                "ONES #150552 / NbJXtiyGP7R4vYnF：3.52.0 订单取消成功但未与小车解绑。"
                "当前低置信，缺口是订单ID和车辆解绑日志。"
            ),
            reply_type="feishu_card",
            payload=_ones_150552_card_payload(),
            project="allspark",
            topic="ONES #150552 订单取消未解绑",
            intent="needs_input",
            agent_session_id="ones-150552-session",
            skill_trace=[
                {"skill": "ones", "reason": "message contains ONES task link"},
                {"skill": "riot-log-triage", "reason": "order execution issue"},
                {"skill": "feishu_card_builder", "reason": "rendered feishu_card summary"},
            ],
        )
    )
    deps = CoreDependencies(
        repository=repository,
        sessions=sessions,
        workspaces=WorkspacePreparer(file_port, workspace_root=tmp_path / "workspaces"),
        agents=AgentRunner(agent_port),
        result_handler=ResultHandler(repository=repository, channel_port=channel, clock=clock),
        clock=clock,
    )
    configure_dependencies_for_tests(deps)
    try:
        msg_id = await insert_message(
            db_file,
            platform_message_id="om_ones_150552_mock_feishu",
            content=ONES_150552_DIRECT_MESSAGE,
        )
        await process_message(msg_id)
    finally:
        configure_dependencies_for_tests(None)

    msg, session = await _message_and_session(db_file, msg_id)
    bot_reply = await _bot_reply(db_file, "om_ones_150552_mock_feishu")
    raw_payload = json.loads(bot_reply["raw_payload"])

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "allspark"
    assert session["thread_id"] == "omt_mock_feishu_150552"
    assert bot_reply["message_type"] == "feishu_card"
    assert raw_payload["delivery"]["message_id"] == "reply_om_ones_150552_mock_feishu"
    assert raw_payload["reply"]["type"] == "feishu_card"

    assert len(feishu_client.replies) == 1
    sent = feishu_client.replies[0]
    assert sent["message_id"] == "om_ones_150552_mock_feishu"
    assert sent["msg_type"] == "interactive"
    assert sent["reply_in_thread"] is True

    card_payload = json.loads(sent["content"])
    assert card_payload["schema"] == "2.0"
    assert card_payload["header"]["title"]["content"] == "ONES #150552 订单取消未解绑"
    assert _has_card_table(card_payload)
    assert "低置信" in _card_text(card_payload)
    assert "车辆解绑日志" in _card_text(card_payload)


@pytest.mark.asyncio
async def test_ones_150552_pipeline_materializes_dsl_trace_and_runtime_card_context(harness):
    """Benchmark 3c: ONES 链路必须产出 DSL、02-process trace，并在卡片展示 worktree 上下文。"""

    db_file = harness["db_file"]

    async def run_with_artifacts(request):
        harness["agent_port"].calls.append(request)
        roots = read_workspace_json(request.workspace_path, "input/artifact_roots.json")
        session_dir = Path(roots["session_dir"])
        runtime_context = {
            "running_project": "allspark",
            "project_path": r"D:\standard\riot\allspark",
            "execution_path": str(session_dir / "worktrees" / "allspark" / "ones-3.52.0"),
            "current_branch": "release/3.46.x",
            "current_commit_sha": "5ed0956073b6",
            "current_version": "3.46",
            "current_describe": "3.46.16-7-g5ed095607",
            "version_source_field": "summary_snapshot.version_normalized",
            "version_source_value": "3.52.0",
            "normalized_version": "3.52.0",
            "target_branch": "master",
            "target_branch_ref": "master",
            "target_tag": "3.52.0",
            "checkout_ref": "3.52.0",
            "recommended_worktree": str(session_dir / "worktrees" / "allspark" / "ones-3.52.0"),
            "execution_branch": "HEAD",
            "execution_commit_sha": "a7916525f8d1",
            "execution_describe": "3.52.0",
            "execution_version": "3.52.0",
            "status": "ready",
            "workspace_scope": "session",
        }
        Path(runtime_context["execution_path"]).mkdir(parents=True, exist_ok=True)
        (request.workspace_path / "input" / "project_runtime_context.json").write_text(
            json.dumps(runtime_context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (request.workspace_path / "output" / "project_runtime_context.json").write_text(
            json.dumps(runtime_context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        project_context_path = request.workspace_path / "input" / "project_context.json"
        project_context = json.loads(project_context_path.read_text(encoding="utf-8"))
        project_context["project_runtime"] = runtime_context
        project_context_path.write_text(
            json.dumps(project_context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        topic_dir = Path(roots["triage_dir"]) / "150552-order-cancel-binding"
        process_dir = topic_dir / "02-process"
        search_dir = topic_dir / "search-runs" / "round1"
        for path in (topic_dir / "01-intake", process_dir, search_dir):
            path.mkdir(parents=True, exist_ok=True)
        state = {
            "project": "allspark",
            "primary_question": "为什么订单 1777271159592 取消成功后仍绑定方格-10008？",
            "version_info": {"value": "3.52.0", "status": "known"},
            "evidence_anchor": {
                "issue_type": "order_execution",
                "order_id": "1777271159592",
                "vehicle_name": "方格-10008",
                "status": "strong",
            },
            "time_alignment": {
                "problem_timezone": "UTC+8",
                "normalized_window": {
                    "start": "2026-04-28 10:00:00",
                    "end": "2026-04-29 01:30:00",
                },
            },
        }
        package = {
            "round_no": 1,
            "focus_question": "确认订单取消成功后是否仍绑定车辆并筛选后端日志证据。",
            "anchor_terms": ["1777271159592", "方格-10008"],
            "gate_terms": ["CMD_ORDER_CANCEL", "绑定", "orderId", "vehicleProcState"],
            "target_files": ["bootstrap"],
            "preferred_files": ["bootstrap.log.2026-04-28"],
        }
        dsl = (
            "round: 1\n"
            "mode: verification/wide-recall\n"
            "question: 为什么订单 1777271159592 取消成功后方格-10008 仍显示绑定？\n"
            "anchors:\n"
            "  prefer: 1777271159592 OR 方格-10008\n"
            "gates:\n"
            "  cancel/canceled, 绑定, orderId, vehicleProcState\n"
            "files:\n"
            "  prefer bootstrap*\n"
        )
        search_results = {
            "search_root": str(session_dir / ".ones" / "150552_NbJXtiyGP7R4vYnF" / "attachment"),
            "dsl_query_file": str(topic_dir / "query.round1.dsl.txt"),
            "accepted_hits_total": 12,
            "keyword_package": package,
            "evidence_hits": [
                {
                    "matched_line": "CMD_ORDER_CANCEL orderId=1777271159592 vehicle=方格-10008 status=success",
                }
            ],
            "order_candidates": [
                {
                    "order_id": "1777271159592",
                    "samples": [{"line": "vehicle 方格-10008 currentOrder=1777271159592"}],
                }
            ],
        }
        final_decision = {
            "status": "partial",
            "confidence": "low",
            "conclusion": "已有截图和日志搜索锚点，但还缺后端解绑闭环证据。",
            "missing_items": ["车辆解绑日志", "订单状态 DB 记录"],
            "next_steps": ["继续围绕 traceId 和 orderId 搜索解绑更新"],
        }
        (topic_dir / "00-state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (topic_dir / "keyword_package.round1.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (topic_dir / "query.round1.dsl.txt").write_text(dsl, encoding="utf-8")
        (search_dir / "search_results.json").write_text(
            json.dumps(search_results, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (process_dir / "final_decision.json").write_text(
            json.dumps(final_decision, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = _reply_result(
            "ONES #150552 当前低置信，需继续补齐解绑日志。",
            reply_type="feishu_card",
            payload=_ones_150552_card_payload(),
            project="allspark",
            topic="ONES #150552 订单取消未解绑",
            intent="needs_input",
            agent_session_id="ones-150552-session",
            skill_trace=[
                {"skill": "ones", "reason": "summary_snapshot contains image_findings"},
                {"skill": "riot-log-triage", "reason": "dsl search executed"},
                {"skill": "feishu_card_builder", "reason": "structured card reply"},
            ],
        )
        return AgentResponse(
            text=json.dumps(result, ensure_ascii=False),
            session_id="ones-150552-session",
            runtime="fake",
            usage={"input_tokens": 11, "output_tokens": 7},
            raw={"cost_usd": 0.0},
        )

    harness["agent_port"].run = run_with_artifacts

    msg_id = await insert_message(
        db_file,
        platform_message_id="om_ones_150552_artifact_contract",
        content=ONES_150552_DIRECT_MESSAGE,
    )
    await process_message(msg_id)

    msg, session = await _message_and_session(db_file, msg_id)
    bot_reply = await _bot_reply(db_file, "om_ones_150552_artifact_contract")
    raw_payload = json.loads(bot_reply["raw_payload"])
    workspace = harness["agent_port"].calls[0].workspace_path
    roots = read_workspace_json(workspace, "input/artifact_roots.json")
    topic_dir = Path(roots["triage_dir"]) / "150552-order-cancel-binding"
    trace_json = topic_dir / "02-process" / "analysis_trace.json"
    trace_md = topic_dir / "02-process" / "analysis_trace.md"
    dsl_path = topic_dir / "query.round1.dsl.txt"
    search_results_path = topic_dir / "search-runs" / "round1" / "search_results.json"

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "allspark"
    assert dsl_path.exists()
    assert search_results_path.exists()
    assert trace_json.exists()
    assert trace_md.exists()

    dsl_text = dsl_path.read_text(encoding="utf-8")
    assert "round: 1" in dsl_text
    assert "1777271159592" in dsl_text
    assert "方格-10008" in dsl_text
    assert "????" not in dsl_text

    search_results = json.loads(search_results_path.read_text(encoding="utf-8"))
    assert Path(search_results["dsl_query_file"]) == dsl_path
    assert search_results["keyword_package"]["anchor_terms"] == ["1777271159592", "方格-10008"]
    assert search_results["accepted_hits_total"] == 12

    trace = json.loads(trace_json.read_text(encoding="utf-8"))
    trace_steps = [item["step"] for item in trace["steps"]]
    assert trace["trace_type"] == "observable_workflow_trace"
    assert trace_steps == [
        "state_initialized",
        "keyword_package_built",
        "log_search_executed",
        "final_decision_recorded",
    ]
    assert trace["steps"][1]["details"]["dsl"] == str(dsl_path)
    assert trace["steps"][2]["details"]["dsl_query_file"] == str(dsl_path)
    trace_text = trace_md.read_text(encoding="utf-8")
    assert "# Analysis Trace" in trace_text
    assert "not hidden model chain-of-thought" in trace_text
    assert str(dsl_path) in trace_text

    card_text = _card_text(raw_payload["reply"]["payload"])
    for expected in (
        "运行上下文",
        "allspark",
        "3.52.0",
        "目标分支：master",
        "主仓库分支：release/3.46.x",
        "Worktree",
        "Worktree 版本：3.52.0",
        "a7916525f8d1 / 3.52.0",
    ):
        assert expected in card_text

    first_element = raw_payload["reply"]["payload"]["body"]["elements"][0]
    assert first_element["tag"] == "markdown"
    assert "运行上下文" in first_element["content"]

    trace_audit = await fetch_one(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = ?",
        ("triage_analysis_trace_written",),
    )
    assert trace_audit is not None


def test_ones_150552_project_runtime_prepares_version_worktree(monkeypatch, tmp_path):
    """Benchmark 3b: ONES 版本上下文必须使用 worktree，不能污染主项目目录。"""

    repo = tmp_path / "allspark"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("allspark\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "3.52.0")
    main_branch = _git(repo, "branch", "--show-current").stdout.strip()

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=repo, description="RIOT3 allspark"),
    )
    monkeypatch.setattr(projects_mod, "settings", SimpleNamespace(project_root=tmp_path))
    projects_mod._git_ref_inventory.cache_clear()

    ctx = projects_mod.prepare_project_runtime_context(
        "allspark",
        ones_result={
            "task": {"number": 150552, "uuid": "NbJXtiyGP7R4vYnF"},
            "named_fields": {"FMS/RIoT版本": "3.52.0"},
            "project": {"display_name": "RIOT3 现场项目"},
        },
    )

    expected_worktree = tmp_path / ".worktrees" / "allspark" / "150552-NbJXtiyGP7R4vYnF-3.52.0"
    assert ctx is not None
    assert ctx.project_path == repo
    assert ctx.execution_path == expected_worktree
    assert ctx.recommended_worktree == expected_worktree
    assert ctx.execution_path.exists()
    assert ctx.execution_path != repo
    assert ctx.normalized_version == "3.52.0"
    assert ctx.target_tag == "3.52.0"
    assert ctx.checkout_ref == "3.52.0"
    assert _git(ctx.execution_path, "describe", "--tags", "--exact-match", "HEAD").stdout.strip() == "3.52.0"
    assert _git(repo, "branch", "--show-current").stdout.strip() == main_branch
    assert any("已创建 worktree" in note or "复用已有 worktree" in note for note in ctx.notes)


@pytest.mark.asyncio
async def test_project_multiturn_routes_same_session_and_second_turn_returns_feishu_card(harness):
    """Benchmark 4: 项目多轮追问必须复用 session，并用飞书卡片结构输出。"""

    db_file = harness["db_file"]
    harness["agent_port"].results.extend(
        [
            _reply_result(
                "已进入 allspark 项目上下文，先按订单执行链路排查。",
                project="allspark",
                topic="allspark 订单链路排查",
                agent_session_id="project-agent-session",
                skill_trace=[
                    {"skill": "riot-log-triage", "reason": "allspark order issue"},
                ],
            ),
            _reply_result(
                "allspark 订单链路排查摘要：当前结论为低置信，等待补充订单ID。",
                reply_type="feishu_card",
                payload=_feishu_card_payload(),
                agent_session_id="project-agent-session",
                skill_trace=[
                    {"skill": "riot-log-triage", "reason": "continued project analysis"},
                    {"skill": "feishu_card_builder", "reason": "structured card reply requested"},
                ],
            ),
        ]
    )

    first_id = await insert_message(
        db_file,
        platform_message_id="om_project_card_t1",
        content="allspark 订单取消后车还挂单，帮我建一个分析会话",
    )
    await process_message(first_id)
    first_msg, first_session = await _message_and_session(db_file, first_id)

    second_id = await insert_message(
        db_file,
        platform_message_id="om_project_card_t2",
        content="继续，按飞书卡片给我一个结构化摘要",
        thread_id=first_session["thread_id"],
    )
    await process_message(second_id)
    second_msg, second_session = await _message_and_session(db_file, second_id)
    second_bot_reply = await _bot_reply(db_file, "om_project_card_t2")
    raw_payload = json.loads(second_bot_reply["raw_payload"])

    assert first_msg["pipeline_status"] == "completed"
    assert second_msg["pipeline_status"] == "completed"
    assert second_msg["session_id"] == first_msg["session_id"]
    assert second_session["project"] == "allspark"
    assert second_session["agent_session_id"] == "project-agent-session"

    second_reply = harness["channel"].calls[1]["reply"]
    assert second_reply.type == "feishu_card"
    assert second_reply.payload["schema"] == "2.0"
    assert _has_card_table(second_reply.payload)

    assert second_bot_reply["message_type"] == "feishu_card"
    assert raw_payload["reply"]["type"] == "feishu_card"
    assert raw_payload["reply"]["payload"]["schema"] == "2.0"
    assert _has_card_table(raw_payload["reply"]["payload"])

    assert await _completed_skill_names(db_file) == {"riot-log-triage", "feishu_card_builder"}


def _reply_result(
    content: str,
    *,
    reply_type: str = "markdown",
    payload: Any = None,
    intent: str | None = None,
    project: str = "",
    topic: str = "",
    agent_session_id: str = "agent-session-1",
    skill_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    session_patch: dict[str, Any] = {}
    if project:
        session_patch["project"] = project
    if topic:
        session_patch["topic"] = topic
    return {
        "action": "reply",
        "agent_session_id": agent_session_id,
        "reply": {
            "channel": "feishu",
            "type": reply_type,
            "intent": intent,
            "content": content,
            "payload": payload,
        },
        "session_patch": session_patch,
        "workspace_patch": {},
        "skill_trace": skill_trace or [],
        "audit": [],
    }


def _feishu_card_payload() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "allspark 订单链路排查摘要",
            }
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**当前结论**：低置信，需要补充订单ID和解绑日志。",
                },
                {
                    "tag": "table",
                    "columns": [
                        {"data_type": "text", "name": "检查项", "display_name": "检查项"},
                        {"data_type": "text", "name": "结果", "display_name": "结果"},
                        {"data_type": "text", "name": "证据", "display_name": "证据"},
                    ],
                    "rows": [
                        {
                            "检查项": "项目路由",
                            "结果": "allspark",
                            "证据": "RIOT3 / allspark 关键词命中",
                        },
                        {
                            "检查项": "订单解绑",
                            "结果": "待验证",
                            "证据": "缺少订单ID与解绑日志",
                        },
                    ],
                },
            ]
        },
    }


def _ones_150552_card_payload() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "orange",
            "title": {
                "tag": "plain_text",
                "content": "ONES #150552 订单取消未解绑",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "**当前结论**：#150552 / 3.52.0 已识别为 allspark 订单链路问题，当前为低置信。",
                },
                {
                    "tag": "markdown",
                    "content": "**缺口**：还缺订单ID、车辆解绑日志、取消回调日志和问题时间窗。",
                },
                {
                    "tag": "table",
                    "columns": [
                        {"data_type": "text", "name": "检查项", "display_name": "检查项"},
                        {"data_type": "text", "name": "结果", "display_name": "结果"},
                        {"data_type": "text", "name": "证据", "display_name": "证据"},
                    ],
                    "rows": [
                        {
                            "检查项": "ONES 工单",
                            "结果": "#150552 / NbJXtiyGP7R4vYnF",
                            "证据": "标题和链接已解析",
                        },
                        {
                            "检查项": "版本/项目",
                            "结果": "3.52.0 -> allspark",
                            "证据": "RIOT3 版本线索",
                        },
                        {
                            "检查项": "置信度",
                            "结果": "低置信",
                            "证据": "缺少订单ID、车辆解绑日志",
                        },
                    ],
                },
            ]
        },
    }


class RecordingFeishuClient:
    def __init__(self, *, thread_id: str) -> None:
        self.thread_id = thread_id
        self.replies: list[dict[str, Any]] = []

    def reply_message(
        self,
        message_id: str,
        content: str,
        msg_type: str = "text",
        reply_in_thread: bool = False,
    ) -> dict[str, str]:
        self.replies.append(
            {
                "message_id": message_id,
                "content": content,
                "msg_type": msg_type,
                "reply_in_thread": reply_in_thread,
            }
        )
        return {
            "message_id": f"reply_{message_id}",
            "thread_id": self.thread_id,
            "root_id": "om_root_mock_feishu",
        }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


async def _message_and_session(db_file: Path, message_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (message_id,))
    assert msg is not None
    session = await fetch_one(db_file, "SELECT * FROM sessions WHERE id = ?", (msg["session_id"],))
    assert session is not None
    return msg, session


async def _bot_reply(db_file: Path, source_platform_message_id: str) -> dict[str, Any]:
    reply = await fetch_one(
        db_file,
        "SELECT * FROM messages WHERE platform_message_id = ?",
        (f"reply_{source_platform_message_id}",),
    )
    assert reply is not None
    return reply


async def _completed_skill_names(db_file: Path) -> set[str]:
    rows = await fetch_all(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = 'agent_run_completed' ORDER BY id",
    )
    skills: set[str] = set()
    for row in rows:
        detail = json.loads(row["detail"])
        for item in detail.get("skill_trace") or []:
            if isinstance(item, dict) and item.get("skill"):
                skills.add(str(item["skill"]))
    return skills


def _registry_names(skill_registry: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name"))
        for item in skill_registry.get("skills", [])
        if isinstance(item, dict) and item.get("name")
    }


def _has_card_table(payload: dict[str, Any]) -> bool:
    elements = payload.get("body", {}).get("elements", [])
    return any(isinstance(item, dict) and item.get("tag") == "table" for item in elements)


def _card_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    header_title = payload.get("header", {}).get("title", {})
    if isinstance(header_title, dict):
        parts.append(str(header_title.get("content") or ""))
    for element in payload.get("body", {}).get("elements", []):
        if not isinstance(element, dict):
            continue
        if element.get("tag") == "markdown":
            parts.append(str(element.get("content") or ""))
        if element.get("tag") == "table":
            for row in element.get("rows") or []:
                if isinstance(row, dict):
                    parts.extend(str(value) for value in row.values())
    return "\n".join(part for part in parts if part)
