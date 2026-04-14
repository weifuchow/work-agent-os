import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { fetchConversations } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { FeishuMessagePreview } from "../components/FeishuMessagePreview"

export default function Conversations() {
  const [page, setPage] = useState(1)
  const pageSize = 20

  const { data, isLoading } = useQuery({
    queryKey: ["conversations", page],
    queryFn: () => fetchConversations(page, pageSize).then((r) => r.data),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const { items, total } = data!
  const totalPages = Math.ceil(total / pageSize)

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-gray-900">对话记录</h2>
        <span className="text-sm text-gray-500">共 {total} 条对话</span>
      </div>

      <div className="space-y-4">
        {items.map((conv: any, i: number) => (
          <div key={i} className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            {/* User message */}
            <div className="p-4 border-b border-gray-100">
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">
                    用户
                  </span>
                  <span className="text-xs text-gray-400">
                    {conv.user_message.sender_name || conv.user_message.sender_id}
                  </span>
                  {conv.user_message.classified_type && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-blue-50 text-blue-600">
                      {conv.user_message.classified_type}
                    </span>
                  )}
                  {conv.user_message.pipeline_status && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-green-50 text-green-600">
                      {conv.user_message.pipeline_status}
                    </span>
                  )}
                </div>
                <span className="text-xs text-gray-400">{formatDate(conv.user_message.created_at)}</span>
              </div>
              <div className="text-sm text-gray-800 whitespace-pre-wrap">
                {conv.user_message.content}
              </div>
            </div>

            {/* Bot reply */}
            {conv.bot_reply ? (
              <div className="p-4 bg-blue-50/50">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-blue-100 text-blue-700">
                    WorkAgent
                  </span>
                  <span className="text-xs text-gray-400">{formatDate(conv.bot_reply.created_at)}</span>
                </div>
                <FeishuMessagePreview message={conv.bot_reply} />
              </div>
            ) : (
              <div className="p-3 bg-gray-50 text-center text-xs text-gray-400">
                未回复
              </div>
            )}

            {/* Session link */}
            {conv.session_id && (
              <div className="px-4 py-2 bg-gray-50 border-t border-gray-100 text-xs text-gray-400">
                会话 #{conv.session_id}
              </div>
            )}
          </div>
        ))}
        {items.length === 0 && (
          <div className="text-center text-gray-400 py-8">暂无对话记录</div>
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
