import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { fetchAuditLogs } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronLeft, ChevronRight } from "lucide-react"

export default function AuditLogs() {
  const [page, setPage] = useState(1)
  const pageSize = 50

  const { data, isLoading } = useQuery({
    queryKey: ["audit-logs", page],
    queryFn: () => fetchAuditLogs(page, pageSize).then((r) => r.data),
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const { items, total } = data!
  const totalPages = Math.ceil(total / pageSize)

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-gray-900">审计日志</h2>
        <span className="text-sm text-gray-500">共 {total} 条</span>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">ID</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">事件类型</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">目标</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">详情</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">操作者</th>
              <th className="text-left px-4 py-3 text-gray-500 font-medium">时间</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((log) => (
              <tr key={log.id} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-gray-400">#{log.id}</td>
                <td className="px-4 py-3">
                  <span className="px-2 py-0.5 rounded-full text-xs bg-blue-50 text-blue-700">
                    {log.event_type}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-600">
                  {log.target_type && `${log.target_type}:${log.target_id}`}
                </td>
                <td className="px-4 py-3 text-gray-500 max-w-sm truncate">{log.detail}</td>
                <td className="px-4 py-3 text-gray-400">{log.operator}</td>
                <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">
                  {formatDate(log.created_at)}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-gray-400">
                  暂无审计日志
                </td>
              </tr>
            )}
          </tbody>
        </table>
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
