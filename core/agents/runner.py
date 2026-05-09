"""Agent runtime invocation and generic result parsing."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from core.app.context import MessageContext, PreparedWorkspace
from core.app.contract import SkillResult, parse_skill_result
from core.orchestrator.agent_runtime import DEFAULT_AGENT_RUNTIME, normalize_agent_runtime
from core.orchestrator.routing_policy import build_policy_prompt_summary
from core.ports import AgentPort, AgentRequest, AgentResponse, ReplyPayload

PROJECT_POLICY_PROMPT_SUMMARY = build_policy_prompt_summary()

GENERIC_SYSTEM_PROMPT = """你是工作消息处理 agent。

Core 已经把平台事实写入 workspace/input。你必须读取这些事实，并根据
skill_registry.json 以及各 SKILL.md 的 Trigger 自行决定是否调用 skill。
平台副作用由 Core 统一处理：不要尝试发送飞书消息、保存 bot reply、直接写 DB
会话状态或审计日志。你只能通过最终 JSON contract 表达 reply、session_patch、
workspace_patch 和 audit，由 MessageProcessor/ResultHandler 决定如何落库和投递。

内置处理流程：
1. Intake：先判断消息类型、用户意图、是否需要回复、优先级、是否是噪音或闲聊。
2. Context：先读取 workspace/input/artifact_roots.json、project_context.json 和 project_workspace.json，
   必要时读取 session_dir/session_workspace.json 了解 session 级 workspace 地图；再读取 message.json、
   session.json、history.json、media_manifest.json、skill_registry.json，只取回答当前问题所需的最小上下文。
   project_workspace.projects 是唯一的项目工作区注册表；source_path/project_path 只表示源仓库登记位置，
   不作为分析或修改目录。所有项目分析必须优先使用 worktree_path/execution_path 指向的 session worktree。
   主编排负责项目工程目录初始化：如果问题涉及尚未加载的相关项目，调用 prepare_project_worktree
   按需加载到当前 session-xx/worktrees 并写入 project_workspace；项目 Agent 不负责创建或切换项目 worktree。
   ONES/现场问题的回复必须显式包含本次实际使用的项目、版本、checkout_ref/target_branch、
   current_branch、worktree_path/execution_path、worktree 检出版本和检出 commit 信息。
   workspace/input/session.json 里的 agent_session_id 只属于当前主编排 Agent；
   派发项目 Agent 时不要把它当作项目级 resume id 传入 dispatch_to_project。
   project_workspace.active_project 只是历史提示，不是路由结论；每轮都必须按当前问题和历史重新判断项目相关度。
   项目 Agent 是一次性执行单元，历史项目 session id 只作为审计记录，不作为自动 resume 来源。
   如果当前 media_manifest 为空，但 history.json 里最近有文件、图片或附件占位消息，
   必须到 artifact_roots.uploads_dir 查找同 session 的最近上传文件；不要把前序附件当成缺失。
3. Analysis：基于事实和上下文做结构化判断；证据不足时明确写缺口，不要编造根因。
4. Skill selection：只有遇到明确业务场景时才调用 workflow skill，例如 ONES、日志排障、
   GitLab review、飞书卡片构建等。intake/context/analysis 是内置流程，不是 skill 名称。
   {PROJECT_POLICY_PROMPT_SUMMARY}
   项目任务先加载 main-orchestrator-project-coordination skill 作为项目协同 playbook；
   调用 dispatch_to_project 时必须显式传入 project_name、task、context；涉及 workflow 时必须显式传入 skill。
   项目 Agent 返回后，主编排必须检查结果是否充分，再决定最终回复、纠正同项目、补派其他项目或追问用户。
5. Presentation：飞书工作消息里的结构化摘要、项目分析、ONES 分析、日志排障结论默认使用
   feishu_card_builder 输出 reply.type=feishu_card；只有闲聊、极短确认或用户明确要求纯文本时，
   才直接输出 markdown 或 text。
   涉及流程、链路、调用顺序、状态流转、排障步骤或跨项目协作步骤时，优先在结构化摘要里提供
   Mermaid 流程图（mermaid 或 flowcharts 字段），并保留给飞书渲染为图片；不要把流程图当代码片段展示。
6. Artifact location：所有本轮产物都必须写在 artifact_roots.json 指定的 session 目录下。
   ONES 产物写 ones_dir，日志排障状态和搜索结果写 triage_dir，review 产物写 review_dir，
   用户上传的原始文件和图片读取 uploads_dir，解压后的材料或人工整理附件写 attachments_dir；
   不要在项目根目录创建新的 .ones、.triage、.review 或 .session 目录。
   日志排障 topic 目录必须保留 skill 工作流结构：01-intake、02-process、search-runs；
   根目录保留 00-state.json、keyword_package.roundN.json、query.roundN.dsl.txt。

不要要求 core 提供业务路由。最终只输出一个 JSON 对象，schema:
{
  "action": "reply | no_reply | failed",
  "reply": {
    "channel": "feishu",
    "type": "text | markdown | feishu_card | file",
    "content": "纯文本 fallback 或正文",
    "payload": null,
    "intent": null
  },
  "session_patch": {},
  "workspace_patch": {},
  "skill_trace": [],
  "audit": []
}
""".replace("{PROJECT_POLICY_PROMPT_SUMMARY}", PROJECT_POLICY_PROMPT_SUMMARY)


CONTRACT_REPAIR_SYSTEM_PROMPT = """你是回复 contract 修复 agent。

你的任务不是重新分析业务，而是修复上一轮 agent 输出的格式问题。你必须只输出一个合法 JSON 对象，
不要输出 Markdown 代码块、解释、前后缀或自然语言。

修复规则：
1. 保留原结论、证据、风险和下一步，不要编造新的业务事实。
2. 默认输出 action=reply，并尽量保留 reply.type=feishu_card。
3. 如果修复飞书卡片，reply.payload 必须是合法飞书卡片根对象，只包含 schema/config/header/body 等飞书字段。
4. Mermaid 必须可被 mermaid-cli 解析。含中文、空格、箭头、括号、斜杠、冒号或等号的节点标签必须加双引号，
   例如 A["actuator存在 -> nextStage(Cancel)"]。
5. 如果无法保证卡片合法，降级为 reply.type=markdown，并把完整可读摘要放入 reply.content。

最终 schema:
{
  "action": "reply",
  "reply": {
    "channel": "feishu",
    "type": "text | markdown | feishu_card | file",
    "content": "纯文本 fallback 或正文",
    "payload": null
  },
  "session_patch": {},
  "workspace_patch": {},
  "skill_trace": [{"skill": "contract_repair_ai"}],
  "audit": []
}
"""


class AgentRunner:
    def __init__(self, agent_port: AgentPort) -> None:
        self.agent_port = agent_port

    async def run_with_skills(
        self,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
    ) -> tuple[SkillResult, AgentResponse]:
        response = await self.agent_port.run(
            AgentRequest(
                workspace_path=workspace.path,
                message=ctx.message,
                session=ctx.session,
                history=ctx.history,
                skill_registry=workspace.skill_registry,
            )
        )
        result = parse_skill_result(response)
        if _needs_contract_repair(result):
            repaired_response = await self.agent_port.run(
                AgentRequest(
                    workspace_path=workspace.path,
                    message=ctx.message,
                    session=ctx.session,
                    history=ctx.history,
                    skill_registry=workspace.skill_registry,
                    mode="repair_contract",
                    repair_context={
                        "error_message": result.error_message,
                        "invalid_output": response.text,
                    },
                )
            )
            repaired_result = parse_skill_result(repaired_response)
            if repaired_result.action == "reply" and repaired_result.reply is not None:
                return (
                    _with_repair_metadata(
                        repaired_result,
                        skill="contract_repair_ai",
                        reason="model repaired malformed agent contract",
                        event_type="agent_contract_repair_model_completed",
                        detail={"original_error": result.error_message},
                    ),
                    repaired_response,
                )
            result = _with_repair_metadata(
                result,
                skill="contract_repair_ai",
                reason="model failed to repair malformed agent contract",
                event_type="agent_contract_repair_model_failed",
                detail={
                    "original_error": result.error_message,
                    "repair_error": repaired_result.error_message,
                },
            )
        return result, response

    async def repair_reply(
        self,
        *,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        reply: ReplyPayload,
        validation_errors: list[dict[str, Any]],
    ) -> ReplyPayload | None:
        response = await self.agent_port.run(
            AgentRequest(
                workspace_path=workspace.path,
                message=ctx.message,
                session=ctx.session,
                history=ctx.history,
                skill_registry=workspace.skill_registry,
                mode="repair_reply",
                repair_context={
                    "validation_errors": validation_errors,
                    "reply": reply.to_dict(),
                },
            )
        )
        result = parse_skill_result(response)
        if result.action != "reply" or result.reply is None:
            return None
        return result.reply


class DefaultAgentPort:
    async def run(self, request: AgentRequest) -> AgentResponse:
        from core.orchestrator.agent_client import agent_client

        session = request.session or {}
        runtime = normalize_agent_runtime(
            str(session.get("agent_runtime") or DEFAULT_AGENT_RUNTIME)
        )
        prompt = _build_generic_prompt(request)
        repair_mode = request.mode.startswith("repair_")
        result = await agent_client.run(
            prompt=prompt,
            system_prompt=CONTRACT_REPAIR_SYSTEM_PROMPT if repair_mode else GENERIC_SYSTEM_PROMPT,
            max_turns=8 if repair_mode else 30,
            session_id=None if repair_mode else str(session.get("agent_session_id") or "").strip() or None,
            runtime=runtime,
            cwd=str(request.workspace_path),
            image_paths=[] if repair_mode else _image_paths_from_registry(request),
        )
        return AgentResponse(
            text=str(result.get("text") or ""),
            session_id=str(result.get("session_id") or "").strip() or None,
            runtime=runtime,
            model=result.get("model"),
            usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
            raw=result,
        )


def _build_generic_prompt(request: AgentRequest) -> str:
    if request.mode == "repair_contract":
        return _build_contract_repair_prompt(request)
    if request.mode == "repair_reply":
        return _build_reply_repair_prompt(request)

    message_preview = {
        "id": request.message.get("id"),
        "message_type": request.message.get("message_type"),
        "content": request.message.get("content"),
    }
    skill_names = [
        item.get("name")
        for item in request.skill_registry.get("skills", [])
        if isinstance(item, dict) and item.get("name")
    ]
    return (
        "请处理这条工作消息。\n\n"
        f"workspace: {request.workspace_path}\n"
        "请先读取 workspace/input/artifact_roots.json、project_context.json 和 project_workspace.json；"
        "如需目录导航，再读取 project_context.session_workspace_path 指向的 session 级 "
        "session_workspace.json。然后读取 message.json、session.json、history.json、"
        "media_manifest.json 和 skill_registry.json。\n"
        "project_workspace.projects 是唯一项目工作区注册表；source_path/project_path 只表示源仓库登记位置，"
        "不要作为分析或修改目录。所有项目分析都使用 worktree_path/execution_path 指向的 session worktree。"
        "主编排负责项目工程目录初始化：如果问题涉及尚未加载的相关项目，调用 prepare_project_worktree，"
        "并传入 session_workspace_path，把项目加载到当前 session-xx/worktrees；项目 Agent 不负责创建或切换项目 worktree。\n"
        "workspace/input/session.json 里的 agent_session_id 只属于当前主编排 Agent；"
        "派发项目 Agent 时不要把它传给 dispatch_to_project。project_workspace.active_project 只是历史提示，"
        "不是本轮路由结论；每轮都要按当前问题和历史重新判断项目相关度。\n"
        f"{PROJECT_POLICY_PROMPT_SUMMARY}"
        "先使用 main-orchestrator-project-coordination skill 作为项目协同 playbook；"
        "调用 dispatch_to_project 时必须显式传入 project_name、task、context 和 db_session_id。\n"
        "项目 Agent 返回后，必须检查结果是否充分，再决定最终回复、纠正同项目、补派其他项目或追问用户。\n"
        "ONES/现场问题的最终卡片必须带上本次实际使用的项目、版本、分支/Tag、current_branch、"
        "worktree_path/execution_path、worktree 检出版本和检出 commit。\n"
        "如果当前 media_manifest.json 为空，但 history.json 显示同 session 前面有附件、图片或日志文件，"
        "请到 artifact_roots.uploads_dir 读取最近上传的文件清单并把它们作为本轮上下文。\n"
        "按 system prompt 的 Intake -> Context -> Analysis -> Skill selection -> Presentation "
        "内置流程处理；根据 skill trigger 自行决定是否调用业务 skill。"
        "如果回答包含流程、链路、调用顺序、状态流转、排障步骤或跨项目协作步骤，"
        "优先使用 feishu_card_builder 的 mermaid/flowcharts/steps 字段生成 Mermaid 流程图。"
        "所有产物必须写入 artifact_roots.json 指定的 session-scoped 目录，"
        "不要写到项目根目录的 .ones/.triage/.review/.session。日志排障 topic 目录必须保留 "
        "01-intake、02-process、search-runs，DSL 写 query.roundN.dsl.txt。\n"
        "如果需要回复，请把最终展示内容放入 reply。\n\n"
        f"message_preview: {json.dumps(message_preview, ensure_ascii=False)}\n"
        f"available_skills: {', '.join(skill_names)}"
    )


def _build_contract_repair_prompt(request: AgentRequest) -> str:
    invalid_output = str(request.repair_context.get("invalid_output") or "")
    error_message = str(request.repair_context.get("error_message") or "")
    return (
        "上一轮 agent 输出无法被 core 解析为合法 JSON contract，请修复。\n\n"
        f"workspace: {request.workspace_path}\n"
        f"message_id: {request.message.get('id')}\n"
        f"parse_error: {error_message}\n\n"
        "invalid_output:\n"
        f"{_truncate(invalid_output, 12000)}\n\n"
        "只输出修复后的完整 JSON contract。"
    )


def _build_reply_repair_prompt(request: AgentRequest) -> str:
    return (
        "投递前校验发现 reply/card 存在格式或 Mermaid 解析问题，请修复。\n\n"
        f"workspace: {request.workspace_path}\n"
        f"message_id: {request.message.get('id')}\n"
        "validation_errors:\n"
        f"{json.dumps(request.repair_context.get('validation_errors') or [], ensure_ascii=False, indent=2)}\n\n"
        "current_reply:\n"
        f"{json.dumps(request.repair_context.get('reply') or {}, ensure_ascii=False, indent=2)}\n\n"
        "只输出修复后的完整 JSON contract。"
    )


def _image_paths_from_registry(request: AgentRequest) -> list[str]:
    manifest_path = request.workspace_path / "input" / "media_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    paths: list[str] = []
    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") != "image":
            continue
        local_path = str(item.get("local_path") or "").strip()
        if local_path:
            paths.append(local_path)
    return paths


def _needs_contract_repair(result: SkillResult) -> bool:
    if result.action == "failed" and "malformed JSON contract" in result.error_message:
        return True
    return any(str(item.get("skill") or "") == "contract_repair" for item in result.skill_trace)


def _with_repair_metadata(
    result: SkillResult,
    *,
    skill: str,
    reason: str,
    event_type: str,
    detail: dict[str, Any],
) -> SkillResult:
    return replace(
        result,
        skill_trace=[{"skill": skill, "reason": reason}, *result.skill_trace],
        audit=[
            {
                "event_type": event_type,
                "detail": detail,
            },
            *result.audit,
        ],
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"
