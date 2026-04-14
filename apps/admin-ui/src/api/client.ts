import axios from "axios"

const api = axios.create({
  baseURL: "/api",
})

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface MessageItem {
  id: number
  platform: string
  platform_message_id: string
  chat_id: string
  sender_id: string
  sender_name: string
  message_type: string
  content: string
  raw_payload?: string | null
  classified_type: string | null
  session_id: number | null
  pipeline_status: string
  pipeline_error: string
  processed_at: string | null
  sent_at: string | null
  received_at: string | null
  created_at: string | null
}

export interface SessionItem {
  id: number
  session_key: string
  source_platform: string
  source_chat_id: string
  owner_user_id: string
  title: string
  topic: string
  project: string
  agent_session_id?: string | null
  agent_runtime?: string
  priority: string
  status: string
  summary_path: string
  last_active_at: string | null
  message_count: number
  risk_level: string
  needs_manual_review: boolean
  task_context_id: number | null
  created_at: string | null
  updated_at: string | null
  summary_content?: string | null
  messages?: (MessageItem & { role: string; sequence_no: number })[]
}

export interface TaskContextItem {
  id: number
  title: string
  description: string
  status: string
  created_at: string | null
  updated_at: string | null
  sessions?: SessionItem[]
  session_count?: number
}

export interface TaskContextListResponse {
  items: TaskContextItem[]
  unlinked_sessions: SessionItem[]
  total: number
  page: number
  page_size: number
}

export interface MemoryFile {
  path: string
  category: string
  name: string
  size: number
  modified_at: string
}

export interface MemoryEntryItem {
  id: number
  scope: string
  project_name: string
  project_version: string
  project_branch: string
  project_commit_sha: string
  project_commit_time: string | null
  category: string
  title: string
  content: string
  tags: string[]
  source_type: string
  source_session_id: number | null
  source_message_id: number | null
  importance: number
  happened_at: string | null
  valid_until: string | null
  first_seen_at: string | null
  last_seen_at: string | null
  occurrence_count: number
  created_at: string | null
  updated_at: string | null
}

export interface MemoryOverviewData {
  total: number
  by_scope: Record<string, number>
  by_category: Record<string, number>
  by_project: { project_name: string; count: number }[]
}

export interface AuditLogItem {
  id: number
  event_type: string
  target_type: string
  target_id: string
  detail: string
  operator: string
  created_at: string | null
}

export interface StatsData {
  messages: number
  sessions: number
  tasks: number
  audit_logs: number
  classification: Record<string, number>
}

export interface TopicCount {
  topic: string
  count: number
}

export interface SessionBrief {
  id: number
  title: string
  topic: string
  status: string
  message_count: number
  risk_level?: string
  last_active_at: string | null
}

export interface ProjectInsightItem {
  name: string
  description: string
  path_exists: boolean
  git_version: string
  git_branch: string
  git_commit_sha: string
  git_commit_time: string | null
  session_count: number
  message_count: number
  open_sessions: number
  active_recent_sessions: number
  memory_count: number
  classification: Record<string, number>
  memory_by_category: Record<string, number>
  memory_highlights: { id: number; title: string; category: string; updated_at: string | null }[]
  recent_sessions: SessionBrief[]
  top_topics: TopicCount[]
  last_activity_at: string | null
}

export interface PersonalInsightItem {
  name: string
  label: string
  session_count: number
  message_count: number
  open_sessions: number
  active_recent_sessions: number
  memory_count: number
  classification: Record<string, number>
  preferences: { id: number; title: string; category: string; content: string; updated_at: string | null }[]
  recent_sessions: SessionBrief[]
  top_topics: TopicCount[]
  last_activity_at: string | null
}

export interface ProjectInsightsData {
  period_days: number
  generated_at: string
  overview: {
    registered_projects: number
    tracked_projects: number
    project_sessions: number
    active_project_sessions: number
    personal_sessions: number
    structured_memories: number
    project_memories: number
    personal_memories: number
  }
  projects: ProjectInsightItem[]
  personal: PersonalInsightItem
  hot_topics: { project_name: string; topic: string; count: number }[]
}

export interface ProjectSummaryData {
  project_name: string
  period_days: number
  summary: string
  generated_at: string
  fallback?: boolean
}

export const fetchMessages = (page = 1, pageSize = 20) =>
  api.get<PaginatedResponse<MessageItem>>("/messages", { params: { page, page_size: pageSize } })

export const fetchMessage = (id: number) =>
  api.get<MessageItem>(`/messages/${id}`)

export const fetchSessions = (page = 1, pageSize = 20) =>
  api.get<PaginatedResponse<SessionItem>>("/sessions", { params: { page, page_size: pageSize } })

export const fetchSession = (id: number) =>
  api.get<SessionItem>(`/sessions/${id}`)

export const fetchAuditLogs = (page = 1, pageSize = 50) =>
  api.get<PaginatedResponse<AuditLogItem>>("/audit-logs", { params: { page, page_size: pageSize } })

export const fetchStats = () =>
  api.get<StatsData>("/stats")

export interface PlaygroundMessage {
  role: "user" | "assistant"
  content: string
}

export interface ModelOption {
  id: string
  provider: string
  label: string
  enabled: boolean
  supports_chat: boolean
  supports_agent: boolean
  is_default: boolean
  is_fallback: boolean
}

export interface ModelsResponse {
  default: string | null
  fallback: string | null
  current: string | null
  override: string | null
  runtime?: string
  providers?: Record<string, { label?: string; enabled?: boolean }>
  models: ModelOption[]
}

export interface AgentRuntimeResponse {
  supported: string[]
  current: string
  override: string | null
}

export const fetchModels = (runtime?: string) =>
  api.get<ModelsResponse>("/models", { params: runtime ? { runtime } : undefined })

export const switchModel = (model: string, runtime?: string) =>
  api.post<{ old_model: string; new_model: string; current: string; runtime: string }>("/model/switch", { model, runtime })

export const fetchAgentRuntime = () =>
  api.get<AgentRuntimeResponse>("/agent/runtime")

export const switchAgentRuntime = (runtime: string) =>
  api.post<{ old_runtime: string; new_runtime: string; current: string }>("/agent/runtime", { runtime })

export const playgroundChat = (
  messages: PlaygroundMessage[],
  system = "",
  model?: string,
) =>
  api.post<{ text: string; run_id: number }>("/playground/chat", {
    messages,
    system,
    model,
    stream: false,
  })

// Pipeline APIs
export const processMessage = (id: number) =>
  api.post(`/messages/${id}/process`)

export const reprocessMessage = (id: number) =>
  api.post(`/messages/${id}/reprocess`)

export const processPendingMessages = (limit = 10) =>
  api.post("/pipeline/process-pending", null, { params: { limit } })

export const fetchPipelineStats = () =>
  api.get<{ pipeline_status: Record<string, number> }>("/pipeline/stats")

// Conversation APIs
export const fetchConversations = (page = 1, pageSize = 20) =>
  api.get("/conversations", { params: { page, page_size: pageSize } })

export const fetchChatHistory = (chatId: string, limit = 50) =>
  api.get(`/conversations/${chatId}/history`, { params: { limit } })

// Token usage
export const fetchTokenUsage = (days = 7) =>
  api.get("/token-usage", { params: { days } })

// Task Contexts
export const fetchTaskContexts = (page = 1, pageSize = 20, status?: string) =>
  api.get<TaskContextListResponse>("/task-contexts", { params: { page, page_size: pageSize, status } })

export const fetchTaskContext = (id: number) =>
  api.get<TaskContextItem>(`/task-contexts/${id}`)

export const updateTaskContext = (id: number, data: { title?: string; description?: string; status?: string }) =>
  api.put(`/task-contexts/${id}`, data)

// Memory Files
export const fetchMemoryFiles = () =>
  api.get<{ files: MemoryFile[] }>("/memory/files")

export const fetchMemoryFile = (path: string) =>
  api.get<{ path: string; content: string }>(`/memory/files/${path}`)

export const updateMemoryFile = (path: string, content: string) =>
  api.put(`/memory/files/${path}`, { content })

export const deleteMemoryFile = (path: string) =>
  api.delete(`/memory/files/${path}`)

// Structured Memory
export const fetchMemoryEntries = (
  page = 1,
  pageSize = 20,
  params?: {
    project_name?: string
    scope?: string
    category?: string
    q?: string
  },
) =>
  api.get<PaginatedResponse<MemoryEntryItem>>("/memory/entries", {
    params: { page, page_size: pageSize, ...params },
  })

export const fetchMemoryEntry = (id: number) =>
  api.get<MemoryEntryItem>(`/memory/entries/${id}`)

export const createMemoryEntry = (data: Partial<MemoryEntryItem> & { title: string; content: string }) =>
  api.post<MemoryEntryItem>("/memory/entries", data)

export const updateMemoryEntry = (id: number, data: Partial<MemoryEntryItem>) =>
  api.put<MemoryEntryItem>(`/memory/entries/${id}`, data)

export const deleteMemoryEntry = (id: number) =>
  api.delete(`/memory/entries/${id}`)

export const fetchMemoryOverview = () =>
  api.get<MemoryOverviewData>("/memory/overview")

export const consolidateMemory = () =>
  api.post<{ consolidated: number; sessions?: number }>("/memory/consolidate")

// Project Insights
export const fetchProjectInsights = (days = 30) =>
  api.get<ProjectInsightsData>("/projects/insights", { params: { days } })

export const generateProjectSummary = (projectName: string, days = 30) =>
  api.post<ProjectSummaryData>("/projects/insights/summary", {
    project_name: projectName,
    days,
  })

export default api
