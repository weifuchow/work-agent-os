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

export const playgroundChat = (
  messages: PlaygroundMessage[],
  system = "",
  model = "claude-sonnet-4-6",
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

export default api
