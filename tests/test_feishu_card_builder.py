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
