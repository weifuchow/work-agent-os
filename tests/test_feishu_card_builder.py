from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from core.app.contract import parse_skill_result


def _load_renderer():
    path = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "skills"
        / "feishu_card_builder"
        / "scripts"
        / "render_feishu_card.py"
    )
    spec = importlib.util.spec_from_file_location("feishu_card_builder_renderer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_render_feishu_card_outputs_reply_contract():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "Triage",
            "summary": "Need more evidence",
            "table": {
                "columns": [{"key": "fact", "label": "Fact"}],
                "rows": [{"fact": "missing logs"}],
            },
            "fallback_text": "Need more evidence",
        },
        reply_type="feishu_card",
    )

    assert contract["action"] == "reply"
    assert contract["reply"]["type"] == "feishu_card"
    assert contract["reply"]["content"] == "Need more evidence"
    assert contract["reply"]["payload"]["schema"] == "2.0"
    assert contract["reply"]["payload"]["body"]["elements"][-1]["tag"] == "table"
    assert contract["skill_trace"][0]["skill"] == "feishu_card_builder"


def test_render_markdown_outputs_structured_summary_payload():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "订单取消问题",
            "summary": "订单已取消但小车仍挂单。",
            "conclusion": "当前证据不足以下根因，需要补齐小车解绑链路日志。",
            "confidence": "low",
            "facts": ["订单状态显示取消成功"],
            "missing_items": ["取消回调日志", "小车任务解绑日志"],
            "next_steps": ["按订单号检索解绑事件"],
        },
        reply_type="markdown",
    )

    assert contract["action"] == "reply"
    assert contract["reply"]["type"] == "markdown"
    assert "## 已确认事实" in contract["reply"]["content"]
    assert "## 待补充" in contract["reply"]["content"]
    summary = contract["reply"]["payload"]["structured_summary"]
    assert summary["confidence"] == "low"
    assert summary["facts"] == ["订单状态显示取消成功"]
    assert summary["missing_items"] == ["取消回调日志", "小车任务解绑日志"]


def test_render_feishu_card_uses_code_panel_for_structured_code_blocks():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "前端 WebSocket 调整",
            "summary": "新增轻量车辆帧类型并切换订阅。",
            "code_blocks": [
                {
                    "title": "轻量车辆帧类型",
                    "path": "src/types/robot.ts",
                    "language": "ts",
                    "code": "export interface RobotLiteFrame {\n  deviceKey: string\n  x: number\n}",
                    "note": "只保留地图渲染必要字段。",
                }
            ],
        },
        reply_type="feishu_card",
    )

    elements = contract["reply"]["payload"]["body"]["elements"]
    code_title = next(item for item in elements if item.get("tag") == "markdown" and "代码片段：轻量车辆帧类型" in item.get("content", ""))
    code_body = elements[elements.index(code_title) + 1]
    assert "文件 `src/types/robot.ts`" in code_title["content"]
    assert "语言 `ts`" in code_title["content"]
    assert "只保留地图渲染必要字段" in code_title["content"]
    assert code_body["tag"] == "markdown"
    assert code_body["content"].startswith("```ts\n")
    assert "export interface RobotLiteFrame" in code_body["content"]


def test_render_feishu_card_splits_markdown_code_fences_from_section_content():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "后端 DTO 调整",
            "summary": "从 RobotDTO 拆出轻量帧。",
            "sections": [
                {
                    "title": "关键代码",
                    "content": "先新增轻量 DTO。\n\n```java path=packages/presentation/RobotLiteDTO.java\npublic record RobotLiteDTO(String deviceKey) {}\n```\n\n再切换推送类型。",
                }
            ],
        },
        reply_type="feishu_card",
    )

    elements = contract["reply"]["payload"]["body"]["elements"]
    contents = [item.get("content", "") for item in elements if item.get("tag") == "markdown"]
    assert any(content == "先新增轻量 DTO。" for content in contents)
    assert any("代码片段：packages/presentation/RobotLiteDTO.java" in content for content in contents)
    assert any(content.startswith("```java\npublic record RobotLiteDTO") for content in contents)
    assert any(content == "再切换推送类型。" for content in contents)


def test_render_feishu_card_uses_mermaid_flow_panel_for_flow_summary():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "format": "flow",
            "title": "WebSocket 配置链路",
            "summary": "前端订阅后由后端网关鉴权并推送轻量车辆帧。",
            "steps": [
                {"title": "前端建立连接", "detail": "携带项目和地图参数。"},
                {"title": "后端鉴权", "detail": "校验 token 和订阅范围。"},
            ],
            "mermaid": "flowchart TD\nA[\"前端建立连接\"]-->B[\"后端鉴权\"]\nB-->C[\"推送轻量车辆帧\"]",
        },
        reply_type="feishu_card",
    )

    elements = contract["reply"]["payload"]["body"]["elements"]
    contents = [item.get("content", "") for item in elements if item.get("tag") == "markdown"]
    assert any("流程图：关键流程图" in content for content in contents)
    assert any(content.startswith("```mermaid\nflowchart TD") for content in contents)
    assert any("流程步骤" in content for content in contents)
    assert any("1. 前端建立连接：携带项目和地图参数。" in content for content in contents)
    assert not any("代码片段：mermaid" in content for content in contents)


def test_render_feishu_card_keeps_section_mermaid_as_flow_panel():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "取消链路",
            "summary": "订单取消后需要等待调度内存态收口。",
            "sections": [
                {
                    "title": "关键流程图",
                    "content": "```mermaid\nflowchart TD\nA[\"订单取消\"]-->B[\"解绑小车\"]\n```",
                }
            ],
        },
        reply_type="feishu_card",
    )

    elements = contract["reply"]["payload"]["body"]["elements"]
    contents = [item.get("content", "") for item in elements if item.get("tag") == "markdown"]
    assert any("流程图：关键流程图" in content for content in contents)
    assert any(content.startswith("```mermaid\nflowchart TD") for content in contents)
    assert not any("代码片段" in content for content in contents)


def test_render_markdown_outputs_flowcharts_and_steps():
    renderer = _load_renderer()

    contract = renderer.render_contract(
        {
            "title": "跨项目处理流程",
            "summary": "先加载调度，再加载前端。",
            "flowcharts": [
                {
                    "title": "多项目 Worktree",
                    "description": "按需加载相关项目。",
                    "source": "flowchart TD\nA[\"allspark\"]-->B[\"riot-frontend-v3\"]",
                }
            ],
            "steps": ["加载 allspark", "加载 riot-frontend-v3"],
        },
        reply_type="markdown",
    )

    content = contract["reply"]["content"]
    assert "## 流程图" in content
    assert "### 多项目 Worktree" in content
    assert "```mermaid\nflowchart TD" in content
    assert "## 流程步骤" in content
    assert "1. 加载 allspark" in content


def test_parse_skill_result_recovers_truncated_card_contract_as_markdown():
    broken = (
        '{"action":"reply","reply":{"channel":"feishu","type":"feishu_card",'
        '"content":"#150552 结论：这是完整兜底摘要。","payload":{"schema":"2.0",'
        '"body":{"elements":[{"tag":"markdown","content":"未闭合'
    )

    result = parse_skill_result(broken)

    assert result.action == "reply"
    assert result.reply is not None
    assert result.reply.type == "markdown"
    assert result.reply.content == "#150552 结论：这是完整兜底摘要。"
    assert result.skill_trace[0]["skill"] == "contract_repair"
