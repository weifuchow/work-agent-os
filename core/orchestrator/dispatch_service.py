"""Project dispatch service for orchestrator MCP tools."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from core.orchestrator.agent_runtime import get_agent_run_runtime_type, normalize_agent_runtime
from core.orchestrator.dispatch_artifacts import (
    _app_db_path,
    _dispatch_id,
    _dispatch_triage_dir,
    _ensure_triage_preflight,
    _extract_message_id,
    _message_artifact_segment,
    _persist_project_run_record,
    _project_entry_from_workspace,
    _project_entry_is_session_worktree_ready,
    _read_json_artifact,
    _safe_artifact_segment,
    _session_upload_image_paths,
    _session_workspace_path,
    _sessions_root,
    _write_json_artifact,
    _write_project_dispatch_failure,
)
from core.orchestrator.routing_policy import RIOT_LOG_TRIAGE_SKILL, select_required_project_skill

_DEFAULT_PROJECT_DISPATCH_TIMEOUT_SECONDS = 1800

PROJECT_AGENT_RESPONSE_RULES = """
处理原则：
1. 默认先尽可能搜索代码、配置、脚本、注释和日志线索，再给用户结论。
2. 只有在你已经搜索过仍然缺少“会直接影响结论”的关键上下文时，才允许向用户追问。
3. 在没有至少完成一次仓库内检索（Read/Grep/Glob/Bash）之前，不允许直接向用户追问。
4. 如果必须追问，先简短说明你已经检查过什么，再只问最少必要的问题，通常 1 个，而且必须先给出已确认的部分结论。
5. 如果问题存在多层实现或多个可能口径，优先先给出你已经能确认的部分结论，再说明还缺哪一个关键信息。
6. 对 ONES / 现场问题，先做证据完整性检查。若缺少问题时间、相关日志/异常堆栈、业务 ID、配置截图/配置片段等关键证据，先要求最小补料，不要直接分析根因。
7. 对 ONES / 现场问题，现场证据只能来自当前工单、当前会话补料和仓库内能确认的代码/配置事实。不要把本机或仓库里其他无关历史日志当作现场证据；若引用，只能标注为实现参考。
8. 默认按纯静态分析处理：只读代码、配置、日志、历史记录和状态文件。除非用户明确要求“运行/构建/编译/测试/复现/启动服务”，禁止执行任何会启动项目、解析依赖或产生构建副作用的命令，例如 gradle/gradlew/mvn/npm/yarn/pnpm、docker compose、java -jar、pytest 等。需要验证时优先用已有日志和代码推理。
9. Bash 仅用于只读检索与轻量文件盘点，例如 rg、Get-Content、Get-ChildItem、git status/show/describe、压缩包目录查看；不要为了“顺手验证”运行项目构建、依赖解析、测试或服务启动。
10. 对工作类问题，优先输出一个“纯 JSON 对象”作为结构化回复，且不要包在代码块里。普通说明/分析优先用 `format=rich`；流程、步骤、链路、时序、状态流转、跨项目协作优先用 `format=flow`。
11. `format=rich` 示例：
   {"format":"rich","title":"标题","summary":"一句话总结","sections":[{"title":"结论","content":"核心结论"},{"title":"说明","content":"补充说明"}],"table":{"columns":[{"key":"item","label":"检查项","type":"text"}],"rows":[{"item":"配置"}]},"fallback_text":"纯文本兜底"}
12. `format=flow` 示例：
   {"format":"flow","title":"标题","summary":"一句话说明","steps":[{"title":"步骤1","detail":"说明"}],"table":{"columns":[{"key":"step","label":"步骤","type":"text"}],"rows":[{"step":"准备"}]},"mermaid":"flowchart TD\\nA[\\\"开始\\\"]-->B[\\\"结束\\\"]","fallback_text":"纯文本兜底"}
   流程图必须优先给 `mermaid` 或 `flowcharts[].source`，节点标签包含中文、空格、箭头、括号、斜杠、冒号或等号时必须加双引号，且不要把 Mermaid 放进 `code_blocks`。
13. 不要因为分析内容较长就额外生成内容相同的附件；如果卡片/结构化摘要已经能表达清楚，就只返回结构化摘要。
14. 只有主编排任务里明确要求你生成目标文件，或用户明确要求 Word/PDF/Markdown/PPT/完整技术方案/方案设计等独立产物时，才写文件。
15. 目标文件由主编排预先定义固定文件名和输出路径，并通过 `target_artifacts` 下发。生成文件时只能
   创建、覆盖或完善这些预定义 path，不能自行临时起名，不能把未列出的历史文件放入结果。
16. 最终 JSON 中返回 `artifacts` 时只能引用主编排预定义 path：
   {"artifacts":[{"path":"绝对或相对路径","title":"文件标题","description":"文件用途"}]}
   如果主编排下发多个 target_artifacts，例如方案 A、方案 B，必须分别生成每个文件，并在 artifacts
   中分别引用对应 path；不能合并成一个未声明文件。
17. 未被明确要求的历史文件、旧报告、旧 PPT 不要重新列入 `artifacts`。
18. 闲聊、问候、极短确认可以继续输出自然语言正文。
19. 不要输出 action/classified_type/topic/project_name/reply_content 等外层元字段。
""".strip()


def _project_dispatch_timeout_seconds() -> int:
    raw = str(os.environ.get("PROJECT_DISPATCH_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured
    return _DEFAULT_PROJECT_DISPATCH_TIMEOUT_SECONDS


def _skill_registered(skill_name: str) -> bool:
    try:
        from core.skill_registry import SKILL_REGISTRY as registry

        if skill_name in registry:
            return True
    except Exception:
        pass

    from core.orchestrator.dispatch_artifacts import _project_root

    return (_project_root() / ".claude" / "skills" / skill_name / "SKILL.md").is_file()


def _normalize_target_artifacts(value: Any, *, base_dir: Path | None = None) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            path = item.strip()
            title = Path(path).name
            description = ""
        elif isinstance(item, dict):
            path = str(item.get("path") or item.get("file_path") or "").strip()
            title = str(item.get("title") or Path(path).name).strip()
            description = str(item.get("description") or "").strip()
        else:
            continue
        if not path:
            continue
        resolved_path = _target_artifact_path(path, base_dir=base_dir)
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        result.append({"path": resolved_path, "title": title or Path(path).name, "description": description})
    return result


def _target_artifact_path(path: str, *, base_dir: Path | None = None) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute() or base_dir is None:
        return raw
    return str((base_dir / candidate).resolve())


def _extract_project_result_artifacts(text: str, target_artifacts: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    parsed = _parse_project_result_json(text)
    artifacts = parsed.get("artifacts") if isinstance(parsed, dict) else None
    if not isinstance(artifacts, list):
        return []
    target_by_path = {str(item.get("path") or "").strip(): item for item in (target_artifacts or [])}
    result: list[dict[str, str]] = []
    for item in artifacts:
        if isinstance(item, str):
            path = item.strip()
            if path:
                if target_by_path and path not in target_by_path:
                    continue
                target = target_by_path.get(path, {})
                result.append(
                    {
                        "path": path,
                        "title": str(target.get("title") or Path(path).name),
                        "description": str(target.get("description") or ""),
                    }
                )
            continue
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("file_path") or "").strip()
        if not path:
            continue
        if target_by_path and path not in target_by_path:
            continue
        target = target_by_path.get(path, {})
        result.append(
            {
                "path": path,
                "title": str(target.get("title") or item.get("title") or Path(path).name).strip(),
                "description": str(target.get("description") or item.get("description") or "").strip(),
            }
        )
    return result


def _parse_project_result_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    candidates = [raw]
    if "```" in raw:
        for block in raw.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block:
                candidates.append(block)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


async def dispatch_to_project(input: dict) -> dict[str, Any]:
    from core.orchestrator.agent_client import agent_client
    from core.app import project_workspace as project_workspace_mod
    from core.app.project_workspace import build_project_workspace_prompt_block
    from core.projects import get_project, merge_skills
    from core.skill_registry import SKILL_REGISTRY

    project_name = str(input["project_name"]).strip()
    task = str(input["task"])
    context = str(input.get("context") or "")
    raw_target_artifacts = input.get("target_artifacts")
    artifact_instruction = str(input.get("artifact_instruction") or "").strip()
    requested_skill = str(input.get("skill") or "").strip()
    input_session_id_record = str(input.get("session_id") or "").strip()
    db_session_id = input.get("db_session_id")
    message_id = _extract_message_id(input)
    orchestration_turn_id = str(input.get("orchestration_turn_id") or "").strip()
    runtime = agent_client.get_active_runtime()

    project = get_project(project_name)
    if not project:
        return {"error": f"Project '{project_name}' not found. Call list_projects to see available projects."}

    import aiosqlite
    from datetime import datetime

    db_path = _app_db_path()
    analysis_workspace = ""
    session_agent_runtime = ""
    dispatch_run_id = None
    if db_session_id:
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                cursor = await db.execute(
                    "SELECT analysis_mode, analysis_workspace, agent_runtime FROM sessions WHERE id = ?",
                    (db_session_id,),
                )
                row = await cursor.fetchone()
                if row:
                    analysis_workspace = str(row[1] or "")
                    session_agent_runtime = str(row[2] or "").strip()
        except Exception as e:
            logger.warning("Failed to read analysis session context: {}", e)
    if session_agent_runtime:
        runtime = normalize_agent_runtime(session_agent_runtime)

    async def _update_dispatch_run(
        *,
        status: str,
        input_path: str = "",
        output_path: str = "",
        error_message: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        if not dispatch_run_id:
            return
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                now = datetime.now().isoformat()
                await db.execute(
                    "UPDATE agent_runs SET status=?, ended_at=?, input_path=?, output_path=?, cost_usd=?, error_message=? WHERE id=?",
                    (status, now, input_path, output_path, cost_usd, error_message[:500], dispatch_run_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to update dispatch AgentRun: {}", exc)

    async def _record_dispatch_paths(*, input_path: str = "", output_path: str = "") -> None:
        if not dispatch_run_id:
            return
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    "UPDATE agent_runs SET input_path=?, output_path=? WHERE id=?",
                    (input_path, output_path, dispatch_run_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record dispatch artifact paths: {}", exc)

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now().isoformat()
            cursor = await db.execute(
                "INSERT INTO agent_runs (agent_name, runtime_type, session_id, status, "
                "message_id, input_path, output_path, input_tokens, output_tokens, cost_usd, error_message, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"dispatch:{project_name}", get_agent_run_runtime_type(runtime), db_session_id, "running",
                 message_id, "", "", 0, 0, 0.0, "", now),
            )
            dispatch_run_id = cursor.lastrowid
            await db.commit()
    except Exception as e:
        logger.warning("Failed to create dispatch AgentRun: {}", e)

    dispatch_id = _dispatch_id(dispatch_run_id)
    runtime_context = None
    project_workspace: dict[str, Any] | None = None
    project_cwd = project.path
    session_workspace_path = None
    session_workspace: dict[str, Any] = {}
    artifact_roots: dict[str, str] = {}
    if db_session_id:
        session_workspace_path = _session_workspace_path(int(db_session_id))
        if session_workspace_path.exists():
            session_workspace = _read_json_artifact(session_workspace_path)
            roots = session_workspace.get("session_artifact_roots")
            if isinstance(roots, dict):
                artifact_roots = {str(key): str(value) for key, value in roots.items()}
            project_workspace_path = session_workspace_path.parent / "project_workspace.json"
            project_workspace = _read_json_artifact(project_workspace_path)
            project_entry = _project_entry_from_workspace(project_workspace, project_name)
            if _project_entry_is_session_worktree_ready(project_entry, project_workspace, artifact_roots):
                project_cwd = Path(str(project_entry.get("worktree_path") or project_entry.get("execution_path")))
                if str(project_workspace.get("active_project") or "").strip() != project_name:
                    project_workspace["active_project"] = project_name
                    _write_json_artifact(project_workspace_path, project_workspace)

    selected_skill = ""
    auto_selected_skill = ""
    message_segment = _message_artifact_segment(message_id)
    project_segment = _safe_artifact_segment(project_name, fallback="project")
    if artifact_roots.get("session_dir"):
        session_dir = Path(artifact_roots["session_dir"])
    elif session_workspace_path:
        session_dir = session_workspace_path.parent
    else:
        session_dir = _sessions_root() / "no-session"
    analysis_root = Path(artifact_roots.get("analysis_dir") or (session_dir / ".analysis"))
    orchestration_root = Path(artifact_roots.get("orchestration_dir") or (session_dir / ".orchestration"))
    session_image_paths = _session_upload_image_paths(artifact_roots)
    analysis_dir = (
        analysis_root / message_segment / project_segment / dispatch_id
        if analysis_root
        else None
    )
    orchestration_dir = orchestration_root / message_segment if orchestration_root else None
    if analysis_dir:
        (analysis_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    if orchestration_dir:
        orchestration_dir.mkdir(parents=True, exist_ok=True)
    target_artifacts = _normalize_target_artifacts(
        raw_target_artifacts,
        base_dir=(analysis_dir / "artifacts") if analysis_dir else None,
    )

    project_entry = _project_entry_from_workspace(project_workspace, project_name)
    if not requested_skill:
        required_skill = select_required_project_skill(
            project_name=project_name,
            task=task,
            context=context,
            artifact_roots=artifact_roots,
        )
        if required_skill and _skill_registered(required_skill):
            selected_skill = required_skill
            auto_selected_skill = required_skill

    triage_dir = _dispatch_triage_dir(
        analysis_workspace=analysis_workspace,
        artifact_roots=artifact_roots,
        requested_skill=requested_skill,
        selected_skill=selected_skill,
        message_segment=message_segment,
        project_segment=project_segment,
        dispatch_id=dispatch_id,
    )

    input_artifact = analysis_dir / "input.json" if analysis_dir else None
    prompt_artifact = analysis_dir / "prompt.md" if analysis_dir else None
    result_artifact = analysis_dir / "result.json" if analysis_dir else None
    trace_artifact = analysis_dir / "analysis_trace.md" if analysis_dir else None
    dispatch_artifact = orchestration_dir / f"{dispatch_id}.json" if orchestration_dir else None

    def _analysis_input_payload() -> dict[str, Any]:
        return {
            "message_id": message_id,
            "db_session_id": db_session_id,
            "orchestration_turn_id": orchestration_turn_id,
            "dispatch_id": dispatch_id,
            "project_name": project_name,
            "skill": selected_skill or requested_skill,
            "task": task,
            "context": context,
            "target_artifacts": target_artifacts,
            "artifact_instruction": artifact_instruction,
            "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
            "checkout_ref": str(project_entry.get("checkout_ref") or ""),
            "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
            "execution_version": str(project_entry.get("execution_version") or ""),
            "project_path": str(project_entry.get("project_path") or project_entry.get("source_path") or project.path),
            "input_session_id_record_only": input_session_id_record,
            "triage_dir": triage_dir,
            "image_paths": session_image_paths,
        }

    async def _fail_preflight(error_text: str, *, result_status: str) -> dict[str, Any]:
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status=result_status,
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill,
            analysis_dir=analysis_dir,
            agent_session_id="",
            status=result_status,
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "triage_dir": triage_dir,
            "project_agent_run": project_agent_run,
        }

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        _write_json_artifact(
            dispatch_artifact,
            {
                "dispatch_id": dispatch_id,
                "message_id": message_id,
                "db_session_id": db_session_id,
                "orchestration_turn_id": orchestration_turn_id,
                "project": project_name,
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "task_summary": task[:500],
                "context_summary": context[:1000],
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "input_path": str(input_artifact or ""),
                "result_path": str(result_artifact or ""),
                "image_paths": session_image_paths,
                "status": "preflight",
            },
        )
    if trace_artifact:
        trace_artifact.write_text(
            "\n".join(
                [
                    "# Project Analysis Trace",
                    "",
                    "- status: preflight",
                    f"- project: {project_name}",
                    f"- dispatch_id: {dispatch_id}",
                    f"- skill: {selected_skill}",
                    f"- requested_skill: {requested_skill}",
                    f"- auto_selected_skill: {auto_selected_skill}",
                    f"- analysis_dir: {analysis_dir or ''}",
                    f"- triage_dir: {triage_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    _ensure_triage_preflight(
        triage_dir=triage_dir,
        project_name=project_name,
        message_id=message_id,
        dispatch_id=dispatch_id,
        task=task,
        context=context,
        analysis_dir=analysis_dir,
        artifact_roots=artifact_roots,
    )
    await _record_dispatch_paths(input_path=str(input_artifact or ""), output_path=str(result_artifact or ""))

    if (
        not _project_entry_is_session_worktree_ready(project_entry, project_workspace, artifact_roots)
        and session_workspace_path
        and session_workspace_path.exists()
    ):
        try:
            entry = await asyncio.wait_for(
                asyncio.to_thread(
                    project_workspace_mod.prepare_project_from_session_workspace_path,
                    project_name,
                    session_workspace_path=session_workspace_path,
                    reason="dispatch_to_project",
                    active=True,
                ),
                timeout=30,
            )
            if entry:
                project_cwd = Path(str(entry.get("worktree_path") or entry.get("execution_path") or project.path))
                project_workspace = _read_json_artifact(session_workspace_path.parent / "project_workspace.json")
                project_entry = _project_entry_from_workspace(project_workspace, project_name)
                if not _project_entry_is_session_worktree_ready(project_entry, project_workspace, artifact_roots):
                    return await _fail_preflight(
                        "dispatch_to_project preflight failed: project workspace policy requires a session worktree, "
                        f"but prepared path is {project_entry.get('worktree_path') or project_entry.get('execution_path') or ''}",
                        result_status="preflight_failed",
                    )
        except asyncio.TimeoutError:
            return await _fail_preflight(
                "dispatch_to_project preflight timed out while preparing project worktree after 30s",
                result_status="preflight_timeout",
            )
        except Exception as exc:
            return await _fail_preflight(
                f"dispatch_to_project preflight failed while preparing project worktree: {exc}",
                result_status="preflight_failed",
            )

    if not project_workspace:
        try:
            runtime_context = await asyncio.wait_for(
                asyncio.to_thread(project_workspace_mod.prepare_project_runtime_context, project_name),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return await _fail_preflight(
                "dispatch_to_project preflight timed out while preparing fallback project runtime after 30s",
                result_status="preflight_timeout",
            )
        except Exception as exc:
            return await _fail_preflight(
                f"dispatch_to_project preflight failed while preparing fallback project runtime: {exc}",
                result_status="preflight_failed",
            )
        if runtime_context:
            project_cwd = Path(runtime_context.execution_path)
            project_workspace = {
                "workspace_scope": "fallback",
                "active_project": project_name,
                "projects": {
                    project_name: runtime_context.to_payload(),
                },
            }
            project_entry = _project_entry_from_workspace(project_workspace, project_name)

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "preparing_prompt",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)

    merged_agents = merge_skills(SKILL_REGISTRY, project_cwd, include_global=True)
    runtime_block = build_project_workspace_prompt_block(project_workspace)

    if requested_skill:
        if requested_skill in merged_agents:
            selected_skill = requested_skill
        else:
            error_text = f"Skill '{requested_skill}' not available for project '{project_name}'."
            if input_artifact:
                _write_json_artifact(input_artifact, _analysis_input_payload())
            if result_artifact:
                _write_json_artifact(
                    result_artifact,
                    {
                        "project": project_name,
                        "dispatch_id": dispatch_id,
                        "failed": True,
                        "error": error_text,
                        "project_agent_session_id": "",
                        "project_agent_session_record_only": True,
                        "triage_dir": triage_dir,
                        "structured_evidence_summary": [],
                    },
                )
            if trace_artifact:
                trace_artifact.write_text(
                    f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
                    encoding="utf-8",
                )
            if dispatch_artifact:
                _write_json_artifact(
                    dispatch_artifact,
                    {
                        "dispatch_id": dispatch_id,
                        "message_id": message_id,
                        "project": project_name,
                        "skill": requested_skill,
                        "task_summary": task[:500],
                        "context_summary": context[:1000],
                        "analysis_dir": str(analysis_dir) if analysis_dir else "",
                        "status": "failed",
                        "error": error_text,
                    },
                )
            await _update_dispatch_run(
                status="failed",
                input_path=str(input_artifact or ""),
                output_path=str(result_artifact or ""),
                error_message=error_text,
            )
            _persist_project_run_record(
                session_workspace_path=session_workspace_path,
                project_name=project_name,
                message_id=message_id,
                dispatch_id=dispatch_id,
                skill=requested_skill,
                analysis_dir=analysis_dir,
                agent_session_id="",
                status="failed",
            )
            return {
                "error": error_text,
                "project": project_name,
                "dispatch_id": dispatch_id,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
            }
    elif selected_skill and selected_skill not in merged_agents:
        logger.warning(
            "Auto-selected {} for dispatch {} project {}, but the skill is unavailable after merge; continuing without it",
            selected_skill,
            dispatch_id,
            project_name,
        )
        selected_skill = ""
        auto_selected_skill = ""
        triage_dir = ""

    if selected_skill:
        if auto_selected_skill:
            logger.info(
                "Auto-selected {} for dispatch {} project {}",
                RIOT_LOG_TRIAGE_SKILL,
                dispatch_id,
                project_name,
            )
        merged_agents = {
            name: definition
            for name, definition in merged_agents.items()
            if name == selected_skill or name != "ones"
        }

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "prompt_ready",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "triage_dir": triage_dir,
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)
    if trace_artifact:
        trace_artifact.write_text(
            "\n".join(
                [
                    "# Project Analysis Trace",
                    "",
                    "- status: prompt_ready",
                    f"- project: {project_name}",
                    f"- dispatch_id: {dispatch_id}",
                    f"- skill: {selected_skill}",
                    f"- requested_skill: {requested_skill}",
                    f"- auto_selected_skill: {auto_selected_skill}",
                    f"- analysis_dir: {analysis_dir or ''}",
                    f"- triage_dir: {triage_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    project_only = list(merged_agents)
    skills_line = ""
    if project_only:
        skills_line = f"\n\n可用的项目 Skills: {', '.join(project_only)}"

    project_prompt = f"""你是项目 "{project.name}" 的工作 Agent。

源仓库目录: {project.path}
执行/分析目录: {project_cwd}
项目分析落盘目录: {analysis_dir or ""}
项目说明: {project.description}{skills_line}{runtime_block}

## 回复规则
{PROJECT_AGENT_RESPONSE_RULES}

## 指定 Skill
{f"必须使用 {selected_skill} workflow skill 完成本次项目任务。" if selected_skill else "未指定，按项目任务自行选择合适的项目 skill。"}

## 边界
你只负责当前项目 {project.name} 的一次性分析。不要判断或调度其他项目，不要调用 dispatch_to_project，不要生成最终飞书卡片，不要依赖历史项目 Agent session。
请把可追溯的分析摘要写入项目分析落盘目录；最终输出给主编排的是结构化结论和证据摘要。

## 任务
{task}"""

    if context:
        project_prompt += f"\n\n## 消息背景\n{context}"
    if artifact_instruction or target_artifacts:
        project_prompt += "\n\n## 目标产物\n"
        if artifact_instruction:
            project_prompt += f"{artifact_instruction}\n"
        if target_artifacts:
            project_prompt += "需要产出的文件：\n" + "\n".join(
                f"- path: {item['path']} | title: {item['title']} | description: {item['description']}"
                for item in target_artifacts
            )
            project_prompt += "\n"
        project_prompt += (
            "这些 path 是主编排预先定义的固定文件名和输出位置。只能创建、覆盖或完善这些文件；"
            "最终 JSON 的 artifacts 也只能引用这些 path。不要自行临时起名，不要把未列出的旧文件或额外文件放入 artifacts。\n"
        )
    if triage_dir:
        project_prompt += f"\n\n## 关联 Triage 目录\n{triage_dir}"
    if session_image_paths:
        project_prompt += (
            "\n\n## 已作为视觉输入传入的图片\n"
            + "\n".join(f"- {path}" for path in session_image_paths)
        )

    project_prompt += (
        "\n\n请在执行/分析目录中处理以上任务，可以使用 Read、Bash、Glob、Grep 等工具访问项目文件。"
        "不要把源仓库目录作为工作目录；不要跨项目调度，缺少其他项目信息时返回给主编排说明需要补派。"
    )

    project_system = (
        f"你运行在项目 {project.name} 的 session worktree 中（{project_cwd}）。"
        f"源仓库目录仅供识别：{project.path}。使用项目级 skills 完成任务。"
        f"{f'本次必须使用 {selected_skill} workflow skill。' if selected_skill else ''}"
        f"项目分析落盘目录：{analysis_dir or ''}。"
        "你是一次性项目执行单元，不要 resume 历史项目 Agent session，不要调用 dispatch_to_project，不要生成最终飞书卡片。"
        f"{runtime_block}"
        f"\n\n{PROJECT_AGENT_RESPONSE_RULES}"
    )

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if prompt_artifact:
        prompt_artifact.write_text(project_prompt, encoding="utf-8")
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "running",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "triage_dir": triage_dir,
                "prompt_path": str(prompt_artifact or ""),
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)

    project_dispatch_timeout = _project_dispatch_timeout_seconds()
    try:
        result = await asyncio.wait_for(
            agent_client.run_for_project(
                prompt=project_prompt,
                system_prompt=project_system,
                project_cwd=str(project_cwd),
                project_agents=merged_agents,
                max_turns=20,
                session_id=None,
                skill=selected_skill or None,
                runtime=runtime,
                image_paths=session_image_paths,
            ),
            timeout=project_dispatch_timeout,
        )
        new_session_id = str(result.get("session_id") or "").strip()
        if result.get("is_error"):
            error_text = str(result.get("text") or "project agent returned an error")
            if result_artifact:
                _write_json_artifact(
                    result_artifact,
                    {
                        "project": project_name,
                        "dispatch_id": dispatch_id,
                        "output_text": str(result.get("text") or ""),
                        "failed": True,
                        "error": error_text,
                        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                        "cost_usd": result.get("cost_usd", 0),
                        "project_agent_session_id": new_session_id,
                        "project_agent_session_record_only": True,
                        "input_session_id_record_only": input_session_id_record,
                        "triage_dir": triage_dir,
                        "structured_evidence_summary": [],
                        "raw": result,
                    },
                )
            if trace_artifact:
                trace_artifact.write_text(
                    f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- skill: {selected_skill or ''}\n- error: {error_text}\n",
                    encoding="utf-8",
                )
            if dispatch_artifact:
                payload = _read_json_artifact(dispatch_artifact)
                payload.update({"status": "failed", "error": error_text, "result_path": str(result_artifact or "")})
                _write_json_artifact(dispatch_artifact, payload)
            await _update_dispatch_run(
                status="failed",
                input_path=str(input_artifact or ""),
                output_path=str(result_artifact or ""),
                error_message=error_text,
                cost_usd=float(result.get("cost_usd") or 0),
            )
            project_agent_run = _persist_project_run_record(
                session_workspace_path=session_workspace_path,
                project_name=project_name,
                message_id=message_id,
                dispatch_id=dispatch_id,
                skill=selected_skill or "",
                analysis_dir=analysis_dir,
                agent_session_id=new_session_id,
                status="failed",
            )
            return {
                "error": error_text,
                "project": project_name,
                "session_id": new_session_id,
                "dispatch_id": dispatch_id,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "result_path": str(result_artifact) if result_artifact else "",
                "agent_session_scope": "record_only",
                "project_agent_run": project_agent_run,
            }

        if result_artifact:
            parsed_result_artifacts = _extract_project_result_artifacts(str(result.get("text") or ""), target_artifacts)
            _write_json_artifact(
                result_artifact,
                {
                    "project": project_name,
                    "dispatch_id": dispatch_id,
                    "output_text": str(result.get("text") or ""),
                    "failed": False,
                    "error": "",
                    "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                    "cost_usd": result.get("cost_usd", 0),
                    "project_agent_session_id": new_session_id,
                    "project_agent_session_record_only": True,
                    "input_session_id_record_only": input_session_id_record,
                    "triage_dir": triage_dir,
                    "structured_evidence_summary": [],
                    "artifacts": parsed_result_artifacts,
                    "target_artifacts": target_artifacts,
                    "artifact_instruction": artifact_instruction,
                    "raw": result,
                },
            )
        if trace_artifact:
            trace_artifact.write_text(
                "\n".join(
                    [
                        "# Project Analysis Trace",
                        "",
                        "- status: success",
                        f"- project: {project_name}",
                        f"- dispatch_id: {dispatch_id}",
                        f"- skill: {selected_skill or ''}",
                        f"- analysis_dir: {analysis_dir or ''}",
                        f"- triage_dir: {triage_dir}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        if dispatch_artifact:
            payload = _read_json_artifact(dispatch_artifact)
            payload.update(
                {
                    "status": "success",
                    "result_path": str(result_artifact or ""),
                    "project_agent_session_id": new_session_id,
                    "project_agent_session_record_only": True,
                }
            )
            _write_json_artifact(dispatch_artifact, payload)

        await _update_dispatch_run(
            status="success",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            cost_usd=float(result.get("cost_usd") or 0),
        )

        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id=new_session_id,
            status="success",
        )

        if db_session_id:
            try:
                async with aiosqlite.connect(str(db_path)) as db:
                    updates = ["project = ?", "agent_runtime = ?", "updated_at = ?"]
                    params = [project_name, runtime, datetime.now().isoformat()]
                    params.append(db_session_id)
                    await db.execute(
                        f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                        params,
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("Failed to update session after dispatch: {}", e)

        return {
            "project": project_name,
            "result": result.get("text", ""),
            "session_id": new_session_id,
            "resume_session_id": None,
            "resume_source": "",
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "triage_dir": triage_dir,
            "agent_session_scope": "record_only",
            "project_agent_run": project_agent_run,
        }
    except asyncio.TimeoutError:
        error_text = f"dispatch_to_project timed out after {project_dispatch_timeout}s"
        logger.warning("dispatch_to_project timed out for {} after {}s", project_name, project_dispatch_timeout)
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status="timeout",
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="timeout",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "project_agent_run": project_agent_run,
        }
    except asyncio.CancelledError:
        error_text = "dispatch_to_project cancelled before project agent completed"
        logger.warning("dispatch_to_project cancelled for {}", project_name)
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status="cancelled",
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="failed",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
        }
    except Exception as e:
        logger.exception("dispatch_to_project failed for {}: {}", project_name, e)
        error_text = str(e)
        if result_artifact:
            _write_json_artifact(
                result_artifact,
                {
                    "project": project_name,
                    "dispatch_id": dispatch_id,
                    "failed": True,
                    "error": error_text,
                    "project_agent_session_id": "",
                    "project_agent_session_record_only": True,
                    "triage_dir": triage_dir,
                    "structured_evidence_summary": [],
                },
            )
        if trace_artifact:
            trace_artifact.write_text(
                f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
                encoding="utf-8",
            )
        if dispatch_artifact:
            payload = _read_json_artifact(dispatch_artifact)
            payload.update({"status": "failed", "error": error_text, "result_path": str(result_artifact or "")})
            _write_json_artifact(dispatch_artifact, payload)
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="failed",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
        }


