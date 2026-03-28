import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { fetchSessions } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronLeft, ChevronRight } from "lucide-react"

export default function Sessions() {
  const [page, setPage] = useState(1)
  const pageSize = 20

  const { data, isLoading } = useQuery({
    queryKey: ["sessions", page],
    queryFn: () => fetchSessions(page, pageSize).then((r) => r.data),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const { items, total } = data!
  const totalPages = Math.ceil(total / pageSize)

  const statusColors: Record<string, string> = {
    open: "bg-green-100 text-green-700",
    waiting: "bg-yellow-100 text-yellow-700",
    paused: "bg-gray-100 text-gray-600",
    closed: "bg-blue-100 text-blue-700",
    archived: "bg-gray-50 text-gray-400",
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-gray-900">工作会话</h2>
        <span className="text-sm text-gray-500">共 {total} 个</span>
      </div>

      <div className="grid gap-3">
        {items.map((sess) => (
          <Link
            key={sess.id}
            to={`/sessions/${sess.id}`}
            className="block bg-white rounded-xl border border-gray-200 p-4 hover:border-blue-300 transition-colors"
          >
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900">
                    {sess.title || sess.session_key}
                  </span>
                  <span className={`px-2 py-0.5 rounded-full text-xs ${statusColors[sess.status] || "bg-gray-100"}`}>
                    {sess.status}
                  </span>
                  {sess.needs_manual_review && (
                    <span className="px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700">
                      需人工审核
                    </span>
                  )}
                </div>
                <div className="text-sm text-gray-500 mt-1">
                  {sess.topic && <span>主题: {sess.topic} · </span>}
                  {sess.project && <span>项目: {sess.project} · </span>}
                  消息数: {sess.message_count}
                </div>
              </div>
              <div className="text-xs text-gray-400 whitespace-nowrap">
                {formatDate(sess.last_active_at)}
              </div>
            </div>
          </Link>
        ))}
        {items.length === 0 && (
          <div className="text-center text-gray-400 py-8">暂无工作会话</div>
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
