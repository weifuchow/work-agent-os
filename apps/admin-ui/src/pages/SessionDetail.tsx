import { useParams, Link } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { fetchSession } from "../api/client"
import { formatDate } from "../lib/utils"
import { ArrowLeft } from "lucide-react"

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>()
  const sessionId = Number(id)

  const { data, isLoading } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => fetchSession(sessionId).then((r) => r.data),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const sess = data!

  return (
    <div>
      <Link to="/sessions" className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 mb-4">
        <ArrowLeft size={16} /> 返回会话列表
      </Link>

      <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
        <h2 className="text-xl font-bold text-gray-900 mb-4">
          {sess.title || sess.session_key}
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-gray-500">状态</span>
            <div className="font-medium">{sess.status}</div>
          </div>
          <div>
            <span className="text-gray-500">优先级</span>
            <div className="font-medium">{sess.priority}</div>
          </div>
          <div>
            <span className="text-gray-500">风险等级</span>
            <div className="font-medium">{sess.risk_level}</div>
          </div>
          <div>
            <span className="text-gray-500">消息数</span>
            <div className="font-medium">{sess.message_count}</div>
          </div>
          <div>
            <span className="text-gray-500">项目</span>
            <div className="font-medium">{sess.project || "-"}</div>
          </div>
          <div>
            <span className="text-gray-500">主题</span>
            <div className="font-medium">{sess.topic || "-"}</div>
          </div>
          <div>
            <span className="text-gray-500">创建时间</span>
            <div className="font-medium">{formatDate(sess.created_at)}</div>
          </div>
          <div>
            <span className="text-gray-500">最后活跃</span>
            <div className="font-medium">{formatDate(sess.last_active_at)}</div>
          </div>
        </div>
      </div>

      {/* Messages in this session */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h3 className="text-sm font-medium text-gray-700 mb-4">会话消息</h3>
        <div className="space-y-3">
          {sess.messages && sess.messages.length > 0 ? (
            sess.messages.map((msg, i) => (
              <div
                key={i}
                className={`p-3 rounded-lg ${
                  msg.role === "assistant"
                    ? "bg-blue-50 ml-8"
                    : "bg-gray-50 mr-8"
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium text-gray-500">
                    {msg.role === "assistant" ? "AI" : msg.sender_name || msg.sender_id}
                  </span>
                  <span className="text-xs text-gray-400">{formatDate(msg.created_at)}</span>
                </div>
                <div className="text-sm text-gray-800 whitespace-pre-wrap">{msg.content}</div>
              </div>
            ))
          ) : (
            <div className="text-center text-gray-400 py-4">暂无关联消息</div>
          )}
        </div>
      </div>
    </div>
  )
}
