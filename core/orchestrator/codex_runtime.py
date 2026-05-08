"""Codex CLI runtime support for AgentClient."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from claude_agent_sdk import AgentDefinition
from loguru import logger

from core.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_NPM_CODEX_EXE = (
    Path(os.environ.get("APPDATA", ""))
    / "npm"
    / "node_modules"
    / "@openai"
    / "codex"
    / "node_modules"
    / "@openai"
    / "codex-win32-x64"
    / "vendor"
    / "x86_64-pc-windows-msvc"
    / "codex"
    / "codex.exe"
)
_DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS = 1200
_WINDOWS_CODEX_START_RETRY_WINERRORS = {5, 193}


def _codex_exec_timeout_seconds(max_turns: int) -> int:
    raw = str(
        os.environ.get("CODEX_EXEC_TIMEOUT_SECONDS")
        or settings.codex_exec_timeout_seconds
        or ""
    ).strip()
    configured = 0
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
    floor = configured if configured > 0 else _DEFAULT_CODEX_EXEC_TIMEOUT_SECONDS
    return max(floor, max_turns * 30)


def _codex_mcp_tool_timeout_seconds() -> int:
    raw = str(os.environ.get("CODEX_MCP_TOOL_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured

    exec_timeout = _codex_exec_timeout_seconds(30)
    return max(120, exec_timeout - 120)


def _same_command_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _resolve_codex_cli_path(*, prefer_wrapper: bool = False) -> str:
    candidates: list[str | None] = []
    if os.name == "nt":
        candidates.extend([
            shutil.which("codex.cmd"),
            shutil.which("codex.exe"),
        ])
    if not prefer_wrapper and _NPM_CODEX_EXE.exists():
        candidates.append(str(_NPM_CODEX_EXE))
    candidates.append(shutil.which("codex"))
    if os.name == "nt" and prefer_wrapper:
        candidates.extend([
            shutil.which("codex.cmd"),
            shutil.which("codex.exe"),
        ])
    if _NPM_CODEX_EXE.exists():
        candidates.append(str(_NPM_CODEX_EXE))

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if os.name == "nt" and Path(candidate).suffix.lower() == ".ps1":
            continue
        return candidate
    return ""


def _should_retry_codex_start(exc: OSError, cmd: list[str], fallback_cmd: list[str] | None) -> bool:
    if os.name != "nt" or not fallback_cmd:
        return False
    if not cmd or not fallback_cmd or _same_command_path(cmd[0], fallback_cmd[0]):
        return False
    return getattr(exc, "winerror", None) in _WINDOWS_CODEX_START_RETRY_WINERRORS


def _should_retry_codex_start_via_shell(exc: OSError, cmd: list[str]) -> bool:
    if os.name != "nt" or not cmd:
        return False
    return getattr(exc, "winerror", None) in _WINDOWS_CODEX_START_RETRY_WINERRORS


def _windows_shell_command(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


async def _kill_windows_process_tree(pid: int, *, reason: str) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(killer.communicate(), timeout=10)
        return killer.returncode == 0
    except Exception as exc:
        logger.warning("Failed to kill Codex process tree after {}: {}", reason, exc)
        return False


async def _terminate_process(proc: asyncio.subprocess.Process, *, reason: str) -> None:
    if proc.returncode is not None:
        return
    pid = int(getattr(proc, "pid", 0) or 0)
    killed_tree = await _kill_windows_process_tree(pid, reason=reason)
    if killed_tree:
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass
        return

    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception as exc:
        logger.warning("Failed to kill Codex process after {}: {}", reason, exc)
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("Codex process did not exit within 10s after {}", reason)
    except Exception as exc:
        logger.warning("Failed while waiting for Codex process after {}: {}", reason, exc)


class CodexRuntimeMixin:
    def _build_codex_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        skill: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        orchestrator_mode: bool = False,
    ) -> str:
        sections: list[str] = []
        if system_prompt:
            sections.append(f"## Operating Context\n{system_prompt}")

        skill_block = self._render_skill_block(skill=skill, project_agents=project_agents)
        if skill_block:
            sections.append(f"## Skill Context\n{skill_block}")

        if orchestrator_mode:
            sections.append(
                "## Runtime Notes\n"
                "你运行在 Codex CLI 中，仓库内置本地 MCP 工具。"
                "主编排只做消息理解、项目路由、session-xx 项目工程目录初始化和结果汇总。"
                "涉及已注册项目、ONES、日志、项目代码或版本 worktree 时，必须调用 dispatch_to_project 进入项目子编排；"
                "项目未加载时，由主编排调用 prepare_project_worktree 准备 session worktree，项目 Agent 不负责创建或切换项目 worktree；"
                "active_project 只是历史提示，每轮必须重新判断项目相关度；dispatch_to_project 必须显式给出 project_name、task、context 和需要的 skill；"
                "ONES 工单、现场日志、订单/车辆执行链路、截图或附件排障必须给 dispatch_to_project 传 skill=\"riot-log-triage\"；"
                "项目 Agent 返回后必须检查结果，再决定最终回复、纠正、补派或追问；不要在主编排里直接用 shell/文件读取完成项目分析。"
            )

        sections.append(f"## User Task\n{prompt}")
        return "\n\n".join(sections)

    def _toml_string(self, value: str) -> str:
        return json.dumps(value)

    def _toml_string_array(self, values: list[str]) -> str:
        return "[" + ", ".join(self._toml_string(v) for v in values) + "]"

    def _build_codex_mcp_overrides(self, scope: str) -> list[str]:
        server_path = PROJECT_ROOT / "core" / "orchestrator" / "codex_mcp_server.py"
        return [
            "-c", f"mcp_servers.work_agent.command={self._toml_string(sys.executable)}",
            "-c", (
                "mcp_servers.work_agent.args="
                f"{self._toml_string_array([str(server_path), '--scope', scope])}"
            ),
            "-c", "mcp_servers.work_agent.startup_timeout_sec=30",
            "-c", f"mcp_servers.work_agent.tool_timeout_sec={_codex_mcp_tool_timeout_seconds()}",
        ]

    def _build_codex_command(
        self,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        scope: str = "orchestrator",
        image_paths: list[str] | None = None,
        prefer_wrapper: bool = False,
    ) -> list[str]:
        cli_path = _resolve_codex_cli_path(prefer_wrapper=prefer_wrapper)
        if not cli_path:
            raise RuntimeError("codex CLI not found in PATH")

        run_cwd = cwd or str(PROJECT_ROOT)
        cmd = [
            cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            run_cwd,
        ]
        if Path(run_cwd).resolve() != PROJECT_ROOT.resolve():
            cmd.extend(["--add-dir", str(PROJECT_ROOT)])
        if model:
            cmd.extend(["-m", model])
        for image_path in image_paths or []:
            cmd.extend(["--image", str(image_path)])
        cmd.extend(self._build_codex_mcp_overrides(scope))
        if session_id:
            cmd.extend(["resume", session_id, "-"])
        else:
            cmd.append("-")
        return cmd

    async def _start_codex_process(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        fallback_cmd: list[str] | None = None,
    ) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except OSError as exc:
            if _should_retry_codex_start(exc, cmd, fallback_cmd):
                logger.warning(
                    "Codex executable start failed with winerror {}; retrying with {}",
                    getattr(exc, "winerror", None),
                    fallback_cmd[0],
                )
                return await asyncio.create_subprocess_exec(
                    *fallback_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            if not _should_retry_codex_start_via_shell(exc, cmd):
                raise
            logger.warning(
                "Codex executable start failed with winerror {}; retrying via cmd.exe shell",
                getattr(exc, "winerror", None),
            )
            return await asyncio.create_subprocess_shell(
                _windows_shell_command(cmd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

    def _extract_codex_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("info") or {}
        total = info.get("total_token_usage") or info.get("last_token_usage") or {}
        return {
            "input_tokens": total.get("input_tokens", 0),
            "cache_read_input_tokens": total.get("cached_input_tokens", 0),
            "output_tokens": total.get("output_tokens", 0),
            "reasoning_output_tokens": total.get("reasoning_output_tokens", 0),
        }

    def _parse_codex_output(
        self,
        output: str,
        *,
        session_id: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "text": "",
            "session_id": session_id,
            "duration_ms": None,
            "num_turns": None,
            "cost_usd": 0,
            "is_error": False,
            "usage": {},
            "model": model,
        }

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            outer_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            event_type = payload.get("type") if outer_type in {"event_msg", "response_item"} else outer_type

            if event_type == "thread.started":
                state["session_id"] = event.get("thread_id") or state["session_id"]
            elif event_type == "agent_message":
                message = payload.get("message") or event.get("message") or ""
                if message:
                    state["text"] = message
            elif event_type == "item.completed":
                item = event.get("item") if isinstance(event.get("item"), dict) else payload.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    state["text"] = item["text"]
            elif event_type == "agent_message_delta":
                delta = payload.get("delta") or event.get("delta") or ""
                if delta:
                    state["text"] += delta
            elif event_type == "task_complete" and not state["text"]:
                state["text"] = payload.get("last_agent_message", "")
            elif event_type == "message":
                if payload.get("role") == "assistant":
                    message_text = self._extract_message_text(payload)
                    if message_text:
                        state["text"] = message_text
            elif event_type == "token_count":
                state["usage"] = self._extract_codex_usage(payload)
            elif event_type == "turn.completed":
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else payload.get("usage", {})
                if usage:
                    state["usage"] = {
                        "input_tokens": usage.get("input_tokens", 0),
                        "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
                    }
            elif event_type == "error":
                state["last_error"] = payload.get("message") or event.get("message") or ""
            elif event_type == "turn.failed":
                state["is_error"] = True
                error = payload.get("error") or {}
                state["text"] = state["text"] or error.get("message", "")

        if state.get("last_error") and not state["text"]:
            state["text"] = state["last_error"]
            state["is_error"] = True

        return state

    async def _run_codex(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: str | None = None,
        skill: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        scope: str = "orchestrator",
        orchestrator_mode: bool = False,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        selected_model = self._select_model(model)
        codex_prompt = self._build_codex_prompt(
            prompt,
            system_prompt=system_prompt,
            skill=skill,
            project_agents=project_agents,
            orchestrator_mode=orchestrator_mode,
        )
        cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
            image_paths=image_paths,
        )
        fallback_cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
            image_paths=image_paths,
            prefer_wrapper=True,
        ) if os.name == "nt" else None
        env = {**os.environ, **self._get_codex_env()}

        logger.info(
            "Codex run: scope={}, cwd={}, model={}, resume={}, timeout_s={}",
            scope,
            cwd or str(PROJECT_ROOT),
            selected_model,
            bool(session_id),
            _codex_exec_timeout_seconds(max_turns),
        )

        proc = await self._start_codex_process(
            cmd,
            cwd=cwd or str(PROJECT_ROOT),
            env=env,
            fallback_cmd=fallback_cmd,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(codex_prompt.encode("utf-8")),
                timeout=_codex_exec_timeout_seconds(max_turns),
            )
        except asyncio.TimeoutError:
            await _terminate_process(proc, reason="timeout")
            raise RuntimeError(f"codex exec timed out (session={session_id or 'new'})")
        except asyncio.CancelledError:
            await _terminate_process(proc, reason="cancellation")
            raise

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        result = self._parse_codex_output(
            stdout_text,
            session_id=session_id,
            model=selected_model,
        )

        if not result.get("text") and result.get("session_id"):
            _, rollout = self._read_codex_rollout(result["session_id"])
            if rollout:
                rollout_text = "\n".join(json.dumps(item, ensure_ascii=False) for item in rollout)
                fallback = self._parse_codex_output(
                    rollout_text,
                    session_id=result["session_id"],
                    model=selected_model,
                )
                if fallback.get("text"):
                    logger.info("Codex stdout empty, recovered final text from rollout {}", result["session_id"])
                    merged_usage = result.get("usage") or fallback.get("usage") or {}
                    result = {
                        **result,
                        **fallback,
                        "usage": merged_usage,
                    }

        if proc.returncode != 0 and not (result.get("is_error") and result.get("text")):
            detail = stderr_text or stdout_text or f"codex exec exit code {proc.returncode}"
            raise RuntimeError(detail)

        return result

    async def _run_codex_stream(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: str | None = None,
        skill: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        scope: str = "orchestrator",
        orchestrator_mode: bool = False,
        image_paths: list[str] | None = None,
    ) -> AsyncIterator[dict]:
        selected_model = self._select_model(model)
        codex_prompt = self._build_codex_prompt(
            prompt,
            system_prompt=system_prompt,
            skill=skill,
            project_agents=project_agents,
            orchestrator_mode=orchestrator_mode,
        )
        cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
            image_paths=image_paths,
        )
        fallback_cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
            image_paths=image_paths,
            prefer_wrapper=True,
        ) if os.name == "nt" else None
        env = {**os.environ, **self._get_codex_env()}

        proc = await self._start_codex_process(
            cmd,
            cwd=cwd or str(PROJECT_ROOT),
            env=env,
            fallback_cmd=fallback_cmd,
        )

        last_text = ""
        current_session_id = session_id
        usage: dict[str, Any] = {}

        async def _read_stderr() -> str:
            if not proc.stderr:
                return ""
            data = await proc.stderr.read()
            return data.decode("utf-8", errors="replace").strip()

        stderr_task = asyncio.create_task(_read_stderr())

        if not proc.stdout:
            raise RuntimeError("codex exec stdout stream unavailable")

        async def _write_stdin() -> None:
            if not proc.stdin:
                return
            proc.stdin.write(codex_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        stdin_task = asyncio.create_task(_write_stdin())

        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                outer_type = event.get("type")
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                event_type = payload.get("type") if outer_type in {"event_msg", "response_item"} else outer_type

                if event_type == "thread.started":
                    current_session_id = event.get("thread_id") or current_session_id
                elif event_type == "agent_message":
                    text = payload.get("message") or event.get("message") or ""
                    if text:
                        if text.startswith(last_text):
                            delta = text[len(last_text):]
                            if delta:
                                yield {"type": "text", "content": delta}
                        else:
                            yield {"type": "text", "content": text}
                        last_text = text
                elif event_type == "item.completed":
                    item = event.get("item") if isinstance(event.get("item"), dict) else payload.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text:
                            if text.startswith(last_text):
                                delta = text[len(last_text):]
                                if delta:
                                    yield {"type": "text", "content": delta}
                            else:
                                yield {"type": "text", "content": text}
                            last_text = text
                elif event_type == "agent_message_delta":
                    delta = payload.get("delta") or event.get("delta") or ""
                    if delta:
                        last_text += delta
                        yield {"type": "text", "content": delta}
                elif event_type == "exec.command_begin":
                    yield {
                        "type": "tool_use",
                        "tool": payload.get("command", "shell"),
                        "input": json.dumps(payload, ensure_ascii=False)[:500],
                    }
                elif event_type == "token_count":
                    usage = self._extract_codex_usage(payload)
                elif event_type == "turn.completed":
                    raw_usage = event.get("usage") if isinstance(event.get("usage"), dict) else payload.get("usage", {})
                    if raw_usage:
                        usage = {
                            "input_tokens": raw_usage.get("input_tokens", 0),
                            "cache_read_input_tokens": raw_usage.get("cached_input_tokens", 0),
                            "output_tokens": raw_usage.get("output_tokens", 0),
                            "reasoning_output_tokens": raw_usage.get("reasoning_output_tokens", 0),
                        }
                elif event_type == "error":
                    message = payload.get("message") or event.get("message") or ""
                    if message:
                        yield {"type": "error", "message": message}
                elif event_type == "turn.failed":
                    error = payload.get("error") or {}
                    message = error.get("message", "codex exec failed")
                    yield {"type": "error", "message": message}

            await stdin_task
            returncode = await proc.wait()
            stderr_text = await stderr_task
            if returncode != 0:
                raise RuntimeError(stderr_text or "codex exec failed")

            yield {
                "type": "result",
                "session_id": current_session_id,
                "is_error": False,
                "duration_ms": None,
                "num_turns": None,
                "cost_usd": 0,
                "usage": usage,
            }
        finally:
            if not stdin_task.done():
                stdin_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()
            await _terminate_process(proc, reason="stream close")
