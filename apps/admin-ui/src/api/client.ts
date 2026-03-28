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
  created_at: string | null
  updated_at: string | null
  messages?: (MessageItem & { role: string; sequence_no: number })[]
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

export default api
