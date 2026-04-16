import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { fetchTaskContexts, type SessionItem } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronDown, ChevronRight, ChevronLeft, Layers } from "lucide-react"

function SessionCard({ sess }: { sess: SessionItem }) {
  const statusColors: Record<string, string> = {
    open: "bg-green-100 text-green-700",
    waiting: "bg-yellow-100 text-yellow-700",
    paused: "bg-gray-100 text-gray-600",
    closed: "bg-blue-100 text-blue-700",
    archived: "bg-gray-50 text-gray-400",
  }

  return (
    <Link
      to={`/sessions/${sess.id}`}
      className="block bg-gray-50 rounded-lg border border-gray-100 p-3 hover:border-blue-300 transition-colors"
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-800">
              #{sess.id} {sess.title || sess.session_key}
            </span>
            <span className={`px-1.5 py-0.5 rounded-full text-xs ${statusColors[sess.status] || "bg-gray-100"}`}>
              {sess.status}
            </span>
            {sess.analysis_mode && (
              <span className="px-1.5 py-0.5 rounded-full text-xs bg-violet-100 text-violet-700">
                分析会话
              </span>
            )}
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {sess.topic && <span>主题: {sess.topic} · </span>}
            {sess.project && <span>项目: {sess.project} · </span>}
            消息数: {sess.message_count}
          </div>
          {sess.analysis_mode && sess.analysis_workspace && (
            <div className="text-xs text-violet-600 mt-1 break-all">
              工作目录: {sess.analysis_workspace}
            </div>
          )}
        </div>
        <div className="text-xs text-gray-400 whitespace-nowrap">
          {formatDate(sess.last_active_at)}
        </div>
      </div>
    </Link>
  )
}

function TaskContextGroup({
  title,
  description,
  status,
  sessions,
  defaultOpen = false,
}: {
  title: string
  description?: string
  status?: string
  sessions: SessionItem[]
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  const statusColors: Record<string, string> = {
    active: "bg-green-100 text-green-700",
    closed: "bg-gray-100 text-gray-500",
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition-colors text-left"
      >
        <div className="flex items-center gap-3">
          {open ? <ChevronDown size={16} className="text-gray-400" /> : <ChevronRight size={16} className="text-gray-400" />}
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium text-gray-900">{title}</span>
              {status && (
                <span className={`px-1.5 py-0.5 rounded-full text-xs ${statusColors[status] || "bg-gray-100"}`}>
                  {status}
                </span>
              )}
              <span className="text-xs text-gray-400">{sessions.length} 个会话</span>
            </div>
            {description && (
              <div className="text-xs text-gray-500 mt-0.5">{description}</div>
            )}
          </div>
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-2 border-t border-gray-100 pt-3">
          {sessions.map((sess) => (
            <SessionCard key={sess.id} sess={sess} />
          ))}
          {sessions.length === 0 && (
            <div className="text-center text-gray-400 py-2 text-sm">暂无会话</div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Sessions() {
  const [page, setPage] = useState(1)
  const pageSize = 20

  const { data, isLoading } = useQuery({
    queryKey: ["task-contexts", page],
    queryFn: () => fetchTaskContexts(page, pageSize).then((r) => r.data),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const { items, unlinked_sessions, total } = data!
  const totalPages = Math.ceil(total / pageSize) || 1

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <Layers size={20} className="text-gray-600" />
          <h2 className="text-xl font-bold text-gray-900">任务与会话</h2>
        </div>
        <span className="text-sm text-gray-500">
          {items.length} 个任务 · {unlinked_sessions.length} 个未分类会话
        </span>
      </div>

      <div className="space-y-3">
        {items.map((tc) => (
          <TaskContextGroup
            key={tc.id}
            title={tc.title || `任务 #${tc.id}`}
            description={tc.description}
            status={tc.status}
            sessions={tc.sessions || []}
            defaultOpen={tc.status === "active"}
          />
        ))}

        {unlinked_sessions.length > 0 && (
          <TaskContextGroup
            title="未分类会话"
            sessions={unlinked_sessions}
            defaultOpen={items.length === 0}
          />
        )}

        {items.length === 0 && unlinked_sessions.length === 0 && (
          <div className="text-center text-gray-400 py-8">暂无任务和会话</div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 mt-4">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="p-2 rounded-lg hover:bg-gray-100 disabled:opacity-30"
          >
            <ChevronLeft size={16} />
          </button>
          <span className="text-sm text-gray-600">{page} / {totalPages}</span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="p-2 rounded-lg hover:bg-gray-100 disabled:opacity-30"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      )}
    </div>
  )
}
