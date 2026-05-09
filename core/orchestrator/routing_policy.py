"""Hard routing policy shared by orchestrator prompts and dispatch tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.orchestrator.dispatch_artifacts import session_upload_file_paths

RIOT_LOG_TRIAGE_SKILL = "riot-log-triage"
RIOT_LOG_TRIAGE_PROJECTS = {"allspark", "riot-standalone", "fms-java"}

RIOT_DIRECT_TRIAGE_MARKERS = (
    "日志",
    "log",
    "ones",
    "工单",
    "现场",
    "附件",
    "上传",
    "图片",
    "截图",
    "报错",
    "异常",
    "堆栈",
    "trace",
    "error",
    "exception",
)
RIOT_EXECUTION_TRIAGE_MARKERS = (
    "订单",
    "任务",
    "子任务",
    "车辆",
    "agv",
    "seeragv",
    "seer",
    "自由导航",
    "导航",
    "路径",
    "点位",
    "站点",
    "工位",
    "库位",
    "下发",
    "经过",
    "未经过",
    "没经过",
    "跳过",
    "绕行",
    "死锁",
    "避让",
    "交通",
    "门禁",
    "电梯",
    "充电",
    "卡住",
    "超时",
    "挂起",
    "恢复",
    "状态流转",
    "执行链路",
)
INVESTIGATION_INTENT_MARKERS = ("检查", "排查", "分析", "定位", "为什么", "为何", "原因", "看一下", "看看", "查一下")
NEGATED_RIOT_TRIAGE_MARKERS = (
    "不涉及日志",
    "不涉及现场日志",
    "不用日志",
    "不看日志",
    "不要看日志",
    "不分析日志",
    "不是日志",
    "无日志",
    "没有日志",
    "不涉及订单",
    "不涉及车辆",
    "不需要排障",
)
LOG_LIKE_UPLOAD_SUFFIXES = {".log", ".gz", ".zip", ".tar", ".tgz", ".txt", ".json"}

PROJECT_DISPATCH_MARKERS = (
    "ones",
    "工单",
    "日志",
    "现场",
    "项目",
    "代码",
    "版本",
    "worktree",
    "配置",
    "bug",
    "review",
)


def select_required_project_skill(
    project_name: str,
    task: str,
    context: str = "",
    artifact_roots: dict[str, str] | None = None,
) -> str:
    """Return the required project workflow skill for hard-policy cases."""

    if looks_like_riot_log_triage_task(
        project_name=project_name,
        task=task,
        context=context,
        artifact_roots=artifact_roots or {},
    ):
        return RIOT_LOG_TRIAGE_SKILL
    return ""


def project_analysis_requires_dispatch(
    task: str,
    context: str = "",
    *,
    project_name: str = "",
    artifact_roots: dict[str, str] | None = None,
) -> bool:
    """Conservative prompt helper for whether main orchestration should dispatch."""

    text = f"{project_name}\n{task}\n{context}".strip()
    if not text:
        return False
    if _text_contains_any(text, PROJECT_DISPATCH_MARKERS):
        return True
    return _has_session_uploads(artifact_roots or {})


def build_policy_prompt_summary() -> str:
    """Short policy text injected into prompts instead of duplicating keyword tables."""

    return (
        "项目硬策略：涉及已注册项目、项目代码、ONES、现场日志、版本 worktree 或项目专属 skill 的任务，"
        "主编排必须调用 dispatch_to_project 派发项目 Agent，不在主编排里直接读项目代码或日志完成业务分析。"
        "RIOT/FMS/allspark 的 ONES、现场日志、订单/车辆执行链路、截图或附件排障必须传 "
        f"skill=\"{RIOT_LOG_TRIAGE_SKILL}\"。active_project 和历史 triage 状态只作上下文提示，"
        "不能替代本轮 project_name/skill 判断。"
    )


def looks_like_riot_log_triage_task(
    *,
    project_name: str,
    task: str,
    context: str,
    artifact_roots: dict[str, str],
) -> bool:
    """Detect RIOT troubleshooting turns where the log triage workflow is required.

    This intentionally uses only the current dispatch payload and session uploads.
    Existing .triage state is ignored so stale analysis workspaces cannot force a
    skill selection for unrelated project questions.
    """

    if project_name.strip().lower() not in RIOT_LOG_TRIAGE_PROJECTS:
        return False

    text = f"{task}\n{context}".strip()
    if not text:
        return False
    if _text_contains_any(text, NEGATED_RIOT_TRIAGE_MARKERS):
        return False

    has_uploads = _has_session_uploads(artifact_roots)
    has_log_uploads = _has_log_like_session_uploads(artifact_roots)
    has_investigation_intent = _text_contains_any(text, INVESTIGATION_INTENT_MARKERS)

    if _text_contains_any(text, RIOT_DIRECT_TRIAGE_MARKERS) and (
        has_investigation_intent or has_uploads or has_log_uploads
    ):
        return True

    execution_related = _text_contains_any(text, RIOT_EXECUTION_TRIAGE_MARKERS)
    if not execution_related:
        return False

    if has_investigation_intent:
        return True

    return has_uploads or has_log_uploads


def _has_session_uploads(artifact_roots: dict[str, str]) -> bool:
    return bool(session_upload_file_paths(artifact_roots, max_files=1))


def _has_log_like_session_uploads(artifact_roots: dict[str, str]) -> bool:
    for path in session_upload_file_paths(artifact_roots):
        name = Path(path).name.lower()
        if any(name.endswith(suffix) for suffix in LOG_LIKE_UPLOAD_SUFFIXES):
            return True
        if ".log." in name or name.endswith(".log"):
            return True
    return False


def _text_contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)
