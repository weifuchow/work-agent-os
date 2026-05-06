"""Unified agent runtime client with Claude and Codex support."""

import asyncio
from contextvars import ContextVar
import json
import os
import shutil
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AgentDefinition,
    HookMatcher,
    query,
    list_sessions as sdk_list_sessions,
    get_session_messages as sdk_get_session_messages,
    delete_session as sdk_delete_session,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from core.config import (
    settings,
    get_agent_runtime_override,
    get_model_override,
    load_models_config,
)
from core.orchestrator.agent_runtime import (
    DEFAULT_AGENT_RUNTIME,
    normalize_agent_runtime,
)
from core.orchestrator.codex_runtime import CodexRuntimeMixin
from core.orchestrator.hooks import _on_subagent_stop
from core.orchestrator.tools import (
    CUSTOM_MCP_SERVER,
    CUSTOM_TOOL_NAMES,
    CUSTOM_TOOLS,
    ORCHESTRATOR_MCP_SERVER,
    ORCHESTRATOR_TOOL_NAMES,
    ORCHESTRATOR_TOOLS,
    PROJECT_TOOL_NAMES,
    PROJECT_TOOLS,
    dispatch_to_project,
    link_task_context,
    list_projects_tool,
    query_db,
    read_memory,
    reply_to_message,
    save_bot_reply,
    search_memory_entries,
    send_feishu_message,
    update_session,
    upsert_memory_entry,
    write_audit_log,
    write_memory,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
_ACTIVE_AGENT_RUNTIME: ContextVar[str] = ContextVar(
    "active_agent_runtime",
    default=DEFAULT_AGENT_RUNTIME,
)
# ==================== Agent Client ====================

class AgentClient(CodexRuntimeMixin):
    """Unified agent runtime client with skills, session management, and custom tools."""

    def get_active_runtime(self) -> str:
        return _ACTIVE_AGENT_RUNTIME.get()

    def _resolve_runtime(self, runtime: str | None = None) -> str:
        return normalize_agent_runtime(
            runtime or get_agent_runtime_override() or settings.default_agent_runtime
        )

    def _select_model(self, model: str | None = None) -> str | None:
        runtime = self.get_active_runtime()
        config = load_models_config()
        from core.config import get_default_model_for_runtime

        runtime_default = get_default_model_for_runtime(config, runtime)
        return (
            model
            or get_model_override(runtime)
            or runtime_default
            or config.get("default")
        )

    def _get_env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
                or os.environ.get("ANTHROPIC_API_KEY", "")
                or settings.anthropic_auth_token
                or settings.anthropic_api_key,
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "")
                or settings.anthropic_base_url,
        }

    def _get_codex_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        openai_key = os.environ.get("OPENAI_API_KEY", "") or settings.openai_api_key
        openai_base_url = os.environ.get("OPENAI_BASE_URL", "") or settings.openai_base_url
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        if openai_base_url:
            env["OPENAI_BASE_URL"] = openai_base_url
        return env

    async def _run_cli_resume(
        self,
        prompt: str,
        session_id: str,
        cwd: str | None = None,
        model: str | None = None,
        max_turns: int = 20,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Resume session via CLI subprocess directly.

        Bypasses SDK's --input-format stream-json which has a bug
        causing exit code 1 when combined with --resume.
        """
        cli_path = shutil.which("claude")
        if not cli_path:
            raise RuntimeError("claude CLI not found in PATH")

        cmd = [
            cli_path,
            "--resume", session_id,
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--permission-mode", "bypassPermissions",
        ]
        if model:
            cmd.extend(["--model", model])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        env = {**os.environ, **self._get_env()}
        env.pop("CLAUDECODE", None)

        logger.info("CLI resume: session={}, cwd={}, model={}, prompt={}",
                     session_id, cwd or "(default)", model, prompt[:100])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=300,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"CLI resume timed out (session={session_id})")

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(
                f"CLI resume exit code {proc.returncode} (session={session_id})\n"
                f"Stderr: {stderr_text}"
            )

        output = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not output:
            return {"text": "", "session_id": session_id, "is_error": True}

        # Parse last JSON object containing "result" key
        data = None
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict) and "result" in candidate:
                    data = candidate
                    break
            except json.JSONDecodeError:
                continue

        if not data:
            return {"text": output, "session_id": session_id}

        return {
            "text": data.get("result", ""),
            "session_id": data.get("session_id", session_id),
            "duration_ms": data.get("duration_ms"),
            "num_turns": data.get("num_turns"),
            "cost_usd": data.get("total_cost_usd", 0),
            "is_error": data.get("is_error", False),
            "usage": data.get("usage", {}),
            "model": model,
        }

    async def _run_sdk_query(
        self,
        *,
        prompt: str | AsyncIterable[dict[str, Any]],
        options: ClaudeAgentOptions,
        log_scope: str,
    ) -> dict[str, Any]:
        stderr_lines: list[str] = []
        options.stderr = lambda line: stderr_lines.append(line)

        result_text = ""
        result_session_id = options.resume
        result_meta: dict[str, Any] = {}

        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage) and msg.content:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_text = block.text
                elif isinstance(msg, ResultMessage):
                    result_session_id = msg.session_id
                    result_meta = {
                        "duration_ms": msg.duration_ms,
                        "num_turns": msg.num_turns,
                        "cost_usd": msg.total_cost_usd,
                        "is_error": msg.is_error,
                        "usage": msg.usage or {},
                    }
        except Exception as e:
            stderr_text = "\n".join(stderr_lines[-20:])
            if result_meta.get("is_error") and result_text:
                logger.warning(
                    "Agent CLI error ({}): {} (returning result: {})\nStderr:\n{}",
                    log_scope,
                    e,
                    result_text[:100],
                    stderr_text,
                )
            else:
                logger.error("Agent CLI failed ({}): {}\nStderr:\n{}", log_scope, e, stderr_text)
                detail = f"{e}\nStderr: {stderr_text}" if stderr_text else str(e)
                raise RuntimeError(detail) from e

        return {
            "text": result_text,
            "session_id": result_session_id,
            "model": options.model,
            **result_meta,
        }

    def _render_skill_block(
        self,
        skill: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
    ) -> str:
        from core.skill_registry import SKILL_REGISTRY

        blocks: list[str] = []

        if skill:
            definition = (project_agents or {}).get(skill) or SKILL_REGISTRY.get(skill)
            if definition:
                blocks.append(
                    "\n".join(
                        part
                        for part in [
                            f"### 指定 Skill: {skill}",
                            definition.description,
                            definition.prompt,
                        ]
                        if part
                    )
                )
            else:
                blocks.append(f"### 指定 Skill: {skill}\n优先按 {skill} 的职责完成任务。")

        if project_agents:
            for name, definition in project_agents.items():
                if skill and name == skill:
                    continue
                blocks.append(
                    "\n".join(
                        part
                        for part in [
                            f"### 可用 Skill: {name}",
                            definition.description,
                            definition.prompt,
                        ]
                        if part
                    )
                )

        text = "\n\n".join(blocks).strip()
        if len(text) > 6000:
            return text[:6000] + "\n\n[技能说明已截断]"
        return text

    def _build_options(
        self,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        project_cwd: Optional[str] = None,
        project_agents: Optional[dict] = None,
        exclude_tools: Optional[list[str]] = None,
        model: Optional[str] = None,
        orchestrator_mode: bool = False,
    ) -> ClaudeAgentOptions:
        from core.skill_registry import SKILL_REGISTRY

        builtin_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        # Agent-visible MCP tools stay side-effect-light. Platform delivery,
        # session writes, bot replies, and audit persistence are handled by
        # MessageProcessor/ResultHandler through core ports.
        custom_tools = ORCHESTRATOR_TOOL_NAMES if orchestrator_mode else CUSTOM_TOOL_NAMES
        allowed = builtin_tools + custom_tools
        if exclude_tools:
            allowed = [t for t in allowed if t not in exclude_tools]

        selected_model = self._select_model(model)

        mcp = ORCHESTRATOR_MCP_SERVER if orchestrator_mode else CUSTOM_MCP_SERVER
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            mcp_servers={"work-agent-tools": mcp},
            allowed_tools=allowed,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            cwd=project_cwd or str(PROJECT_ROOT),
            env=self._get_env(),
            model=selected_model,
            # Register skills as sub-agents. For project runs, an empty dict means
            # "no project-local skills", and must not fall back to unrelated globals.
            agents=project_agents if project_agents is not None else SKILL_REGISTRY,
            # Capture sub-agent transcripts
            hooks={
                "SubagentStop": [HookMatcher(hooks=[_on_subagent_stop])],
            },
        )

        # Resume existing session
        if session_id:
            opts.resume = session_id

        return opts

    async def run(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
        image_paths: list[str] | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Run agent to completion. Returns {"text": ..., "session_id": ...}."""
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        if resolved_runtime == "codex":
            try:
                return await self._run_codex(
                    prompt=prompt if isinstance(prompt, str) else "[multimodal prompt omitted for codex runtime]",
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    skill=skill,
                    model=model,
                    cwd=cwd,
                    scope="orchestrator",
                    orchestrator_mode=True,
                    image_paths=image_paths,
                )
            finally:
                _ACTIVE_AGENT_RUNTIME.reset(token)

        # If a specific skill is requested, prepend instruction to use it
        try:
            if skill and isinstance(prompt, str):
                prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

            if session_id and isinstance(prompt, str):
                selected_model = self._select_model(model)
                return await self._run_cli_resume(
                    prompt=prompt,
                    session_id=session_id,
                    cwd=cwd,
                    model=selected_model,
                    max_turns=max_turns,
                    system_prompt=system_prompt or None,
                )

            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                skill=skill,
                model=model,
                project_cwd=cwd,
                orchestrator_mode=True,
            )
            return await self._run_sdk_query(
                prompt=prompt,
                options=options,
                log_scope="orchestrator",
            )
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    async def run_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
        image_paths: list[str] | None = None,
    ) -> AsyncIterator[dict]:
        """Run agent with SSE streaming. Yields event dicts."""
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        try:
            if resolved_runtime == "codex":
                async for event in self._run_codex_stream(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    skill=skill,
                    model=model,
                    scope="orchestrator",
                    orchestrator_mode=True,
                    image_paths=image_paths,
                ):
                    yield event
                return

            if skill:
                prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                skill=skill,
                model=model,
            )

            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, SystemMessage):
                    yield {"type": "system", "subtype": msg.subtype}

                elif isinstance(msg, AssistantMessage) and msg.content:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {"type": "tool_use", "tool": block.name, "input": json.dumps(block.input, ensure_ascii=False)[:500]}

                elif isinstance(msg, ResultMessage):
                    yield {
                        "type": "result",
                        "session_id": msg.session_id,
                        "is_error": msg.is_error,
                        "duration_ms": msg.duration_ms,
                        "num_turns": msg.num_turns,
                        "cost_usd": msg.total_cost_usd,
                    }
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    # ---- Project Dispatch ----

    async def run_for_project(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        system_prompt: str,
        project_cwd: str,
        project_agents: dict,
        max_turns: int = 20,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run an agent in a specific project directory with project-specific skills.

        Args:
            session_id: Pass a previous SDK session_id to resume conversation context.

        Used by dispatch_to_project tool. Excludes recursive dispatch tools.
        """
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        try:
            if resolved_runtime == "codex":
                return await self._run_codex(
                    prompt=prompt if isinstance(prompt, str) else "[multimodal prompt omitted for codex runtime]",
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    skill=skill,
                    model=model,
                    cwd=project_cwd,
                    project_agents=project_agents,
                    scope="project",
                    image_paths=image_paths,
                )

            if skill and isinstance(prompt, str):
                prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

            if session_id and isinstance(prompt, str):
                selected_model = self._select_model(model)
                return await self._run_cli_resume(
                    prompt=prompt,
                    session_id=session_id,
                    cwd=project_cwd,
                    model=selected_model,
                    max_turns=max_turns,
                    system_prompt=system_prompt,
                )

            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                skill=skill,
                project_cwd=project_cwd,
                project_agents=project_agents,
                exclude_tools=[name for name in CUSTOM_TOOL_NAMES if name not in PROJECT_TOOL_NAMES],
                model=model,
            )
            return await self._run_sdk_query(
                prompt=prompt,
                options=options,
                log_scope=f"project={project_cwd}",
            )
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    # ---- Session Management ----

    def _find_codex_session_files(self, session_id: str) -> list[Path]:
        sessions_root = CODEX_HOME / "sessions"
        if not sessions_root.exists():
            return []
        return sorted(
            sessions_root.rglob(f"*{session_id}.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _read_codex_rollout(self, session_id: str) -> tuple[Path | None, list[dict[str, Any]]]:
        files = self._find_codex_session_files(session_id)
        if not files:
            return None, []
        events: list[dict[str, Any]] = []
        for raw_line in files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return files[0], events

    async def _list_claude_sessions(self) -> list[dict]:
        sessions = await sdk_list_sessions()
        return [
            {
                "id": s.id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "tags": s.tags,
                "runtime": "claude",
            }
            for s in sessions
        ]

    def _list_codex_sessions(self) -> list[dict]:
        from datetime import datetime

        index_path = CODEX_HOME / "session_index.jsonl"
        if not index_path.exists():
            return []

        sessions: list[dict] = []
        for raw_line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = entry.get("id")
            if not session_id:
                continue

            session_file, rollout = self._read_codex_rollout(session_id)
            created_at = None
            for event in rollout:
                if event.get("type") == "session_meta":
                    payload = event.get("payload") or {}
                    created_at = payload.get("timestamp") or payload.get("created_at")
                    break

            updated_at = entry.get("updated_at")
            if not updated_at and session_file:
                updated_at = datetime.fromtimestamp(session_file.stat().st_mtime).isoformat()

            sessions.append(
                {
                    "id": session_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "tags": [entry.get("thread_name", "")] if entry.get("thread_name") else [],
                    "runtime": "codex",
                }
            )

        return sessions

    def _extract_message_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content") or []
        texts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("input_text", "output_text"):
                text = item.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()

    async def list_sessions(self, runtime: Optional[str] = None) -> list[dict]:
        if runtime:
            resolved_runtime = self._resolve_runtime(runtime)
            if resolved_runtime == "claude":
                return await self._list_claude_sessions()
            return self._list_codex_sessions()

        sessions = await self._list_claude_sessions()
        sessions.extend(self._list_codex_sessions())
        sessions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return sessions

    async def get_session_messages(
        self,
        session_id: str,
        runtime: Optional[str] = None,
    ) -> list[dict]:
        resolved_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
        if resolved_runtime == "claude":
            messages = await sdk_get_session_messages(session_id)
            result = []
            for m in messages:
                result.append({"role": m.role, "content": str(m.content)[:1000]})
            return result

        _, rollout = self._read_codex_rollout(session_id)
        messages: list[dict] = []
        for event in rollout:
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._extract_message_text(payload)
            if not text or text.startswith("<environment_context>"):
                continue
            messages.append({"role": role, "content": text[:1000]})
        return messages

    async def delete_session(
        self,
        session_id: str,
        runtime: Optional[str] = None,
    ) -> bool:
        resolved_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
        if resolved_runtime == "claude":
            await sdk_delete_session(session_id)
            return True

        deleted = False
        for path in self._find_codex_session_files(session_id):
            try:
                path.unlink()
                deleted = True
            except OSError:
                continue

        for filename in ("session_index.jsonl", "history.jsonl"):
            target = CODEX_HOME / filename
            if not target.exists():
                continue
            kept: list[str] = []
            changed = False
            for raw_line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(raw_line)
                    continue
                if item.get("id") == session_id or item.get("session_id") == session_id:
                    changed = True
                    continue
                kept.append(raw_line)
            if changed:
                target.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                deleted = True

        return deleted


# Singleton
agent_client = AgentClient()
