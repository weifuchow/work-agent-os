import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { fetchMessages, reprocessMessage, processPendingMessages } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronLeft, ChevronRight, RefreshCw, Play } from "lucide-react"

export default function Messages() {
  const [page, setPage] = useState(1)
  const pageSize = 20
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ["messages", page],
    queryFn: () => fetchMessages(page, pageSize).then((r) => r.data),
  })

  const reprocessMut = useMutation({
    mutationFn: (id: number) => reprocessMessage(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["messages"] }),
  })

  const processPendingMut = useMutation({
    mutationFn: () => processPendingMessages(20),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["messages"] }),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const { items, total } = data!
  const totalPages = Math.ceil(total / pageSize)

  const typeColors: Record<string, string> = {
    work_question: "bg-blue-100 text-blue-700",
    urgent_issue: "bg-red-100 text-red-700",
    task_request: "bg-orange-100 text-orange-700",
    chat: "bg-gray-100 text-gray-600",
    noise: "bg-gray-50 text-gray-400",
  }

  const pipelineColors: Record<string, string> = {
    pending: "bg-yellow-50 text-yellow-600",
    classifying: "bg-blue-50 text-blue-600",
    routing: "bg-purple-50 text-purple-600",
    completed: "bg-green-50 text-green-600",
    skipped: "bg-gray-50 text-gray-400",
    failed: "bg-red-50 text-red-600",
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-gray-900">消息记录</h2>
        <div className="flex items-center gap-3">
          <button
            onClick={() => processPendingMut.mutate()}
            disabled={processPendingMut.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            <Play size={14} />
            处理待分类
          </button>
          <span className="text-sm text-gray-500">共 {total} 条</span>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">ID</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">发送者</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">内容</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">分类</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">管线</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">会话</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">时间</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((msg) => (
              <tr key={msg.id} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-gray-400">#{msg.id}</td>
                <td className="px-4 py-3">
                  <div className="text-gray-900">{msg.sender_name || msg.sender_id}</div>
                  <div className="text-xs text-gray-400">{msg.chat_id.slice(0, 12)}...</div>
                </td>
                <td className="px-4 py-3 text-gray-700 max-w-md truncate">
                  {msg.content || <span className="text-gray-300">[{msg.message_type}]</span>}
                </td>
                <td className="px-4 py-3">
                  {msg.classified_type ? (
                    <span className={`px-2 py-0.5 rounded-full text-xs ${typeColors[msg.classified_type] || "bg-gray-100 text-gray-600"}`}>
                      {msg.classified_type}
                    </span>
                  ) : (
                    <span className="text-gray-300 text-xs">未分类</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 rounded-full text-xs ${pipelineColors[msg.pipeline_status] || "bg-gray-100 text-gray-600"}`}>
                    {msg.pipeline_status}
                  </span>
                  {msg.pipeline_error && (
                    <div className="text-xs text-red-400 mt-0.5 truncate max-w-[120px]" title={msg.pipeline_error}>
                      {msg.pipeline_error}
                    </div>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-400">
                  {msg.session_id ? `#${msg.session_id}` : "-"}
                </td>
                <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">
                  {formatDate(msg.created_at)}
                </td>
                <td className="px-4 py-3">
                  <button
                    onClick={() => reprocessMut.mutate(msg.id)}
                    disabled={reprocessMut.isPending}
                    className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600"
                    title="重新处理"
                  >
                    <RefreshCw size={14} />
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-gray-400">
                  暂无消息记录
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 mt-4">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="p-2 rounded-lg hover:bg-gray-100 disabled:opacity-30"
          >
            <ChevronLeft size={16} />
          </button>
          <span className="text-sm text-gray-600">
            {page} / {totalPages}
          </span>
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
