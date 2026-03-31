import enum
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    """Local time for all timestamp fields."""
    return datetime.now()


# ---------- Enums ----------

class MessageClassifiedType(str, enum.Enum):
    noise = "noise"
    chat = "chat"
    work_question = "work_question"
    urgent_issue = "urgent_issue"
    task_request = "task_request"


class PipelineStatus(str, enum.Enum):
    pending = "pending"
    classifying = "classifying"
    routing = "routing"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"  # for noise/chat that don't need routing


class SessionStatus(str, enum.Enum):
    open = "open"
    waiting = "waiting"
    paused = "paused"
    closed = "closed"
    archived = "archived"


class TaskContextStatus(str, enum.Enum):
    active = "active"
    closed = "closed"


class TaskStatus(str, enum.Enum):
    open = "open"
    doing = "doing"
    done = "done"
    cancelled = "cancelled"


class ReportStatus(str, enum.Enum):
    generated = "generated"
    sent = "sent"
    failed = "failed"


class AgentRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


# ---------- Models ----------

class TaskContext(SQLModel, table=True):
    __tablename__ = "task_contexts"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(default="", max_length=256)
    description: str = Field(default="")
    status: str = Field(default=TaskContextStatus.active, max_length=32)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="feishu", max_length=32)
    platform_message_id: str = Field(max_length=128, index=True)
    chat_id: str = Field(max_length=128, index=True)
    sender_id: str = Field(max_length=128, index=True)
    sender_name: str = Field(default="", max_length=128)
    message_type: str = Field(default="text", max_length=32)
    content: str = Field(default="")
    sent_at: Optional[datetime] = None
    received_at: datetime = Field(default_factory=_now)
    raw_payload: str = Field(default="")
    classified_type: Optional[str] = Field(default=None, max_length=32)
    session_id: Optional[int] = Field(default=None, foreign_key="sessions.id", index=True)
    thread_id: str = Field(default="", max_length=128, index=True)
    root_id: str = Field(default="", max_length=128)
    parent_id: str = Field(default="", max_length=128)
    pipeline_status: str = Field(default=PipelineStatus.pending, max_length=32)
    pipeline_error: str = Field(default="")
    processed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)


class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_key: str = Field(max_length=128, unique=True, index=True)
    source_platform: str = Field(default="feishu", max_length=32)
    source_chat_id: str = Field(default="", max_length=128)
    owner_user_id: str = Field(default="", max_length=128)
    title: str = Field(default="")
    topic: str = Field(default="")
    project: str = Field(default="")
    priority: str = Field(default="normal", max_length=32)
    status: str = Field(default=SessionStatus.open, max_length=32)
    parent_session_id: Optional[int] = Field(default=None, foreign_key="sessions.id")
    task_context_id: Optional[int] = Field(default=None, foreign_key="task_contexts.id", index=True)
    # Feishu thread_id — used to bind session to a thread/topic
    thread_id: str = Field(default="", max_length=128, index=True)
    # Agent SDK session ID — used to resume multi-turn conversations
    agent_session_id: Optional[str] = Field(default=None, max_length=256)
    summary_path: str = Field(default="")
    last_active_at: datetime = Field(default_factory=_now)
    message_count: int = Field(default=0)
    risk_level: str = Field(default="low", max_length=32)
    needs_manual_review: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SessionMessage(SQLModel, table=True):
    __tablename__ = "session_messages"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id", index=True)
    message_id: int = Field(foreign_key="messages.id", index=True)
    role: str = Field(default="user", max_length=32)
    sequence_no: int = Field(default=0)
    created_at: datetime = Field(default_factory=_now)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: Optional[int] = Field(default=None, foreign_key="sessions.id", index=True)
    title: str = Field(default="")
    description: str = Field(default="")
    status: str = Field(default=TaskStatus.open, max_length=32)
    priority: str = Field(default="normal", max_length=32)
    assignee: str = Field(default="", max_length=128)
    source: str = Field(default="", max_length=128)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Report(SQLModel, table=True):
    __tablename__ = "reports"

    id: Optional[int] = Field(default=None, primary_key=True)
    report_date: str = Field(max_length=10, index=True)  # YYYY-MM-DD
    report_type: str = Field(default="daily", max_length=32)
    content_path: str = Field(default="")
    status: str = Field(default=ReportStatus.generated, max_length=32)
    generated_at: datetime = Field(default_factory=_now)
    sent_at: Optional[datetime] = None


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: Optional[int] = Field(default=None, foreign_key="sessions.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="messages.id", index=True)
    agent_name: str = Field(default="", max_length=64)
    runtime_type: str = Field(default="claude_api", max_length=32)
    input_path: str = Field(default="")
    output_path: str = Field(default="")
    status: str = Field(default=AgentRunStatus.pending, max_length=32)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    error_message: str = Field(default="")


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    event_type: str = Field(max_length=64, index=True)
    target_type: str = Field(default="", max_length=64)
    target_id: str = Field(default="", max_length=128)
    detail: str = Field(default="")
    operator: str = Field(default="system", max_length=64)
    created_at: datetime = Field(default_factory=_now)
