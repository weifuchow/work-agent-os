import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { fetchAuditLogs, type AuditLogItem } from "../api/client"
import { formatDate } from "../lib/utils"
import { ChevronLeft, ChevronRight, ChevronDown, ChevronUp } from "lucide-react"

function tryFormatJson(detail: string): { isJson: boolean; formatted: string } {
  try {
    const obj = JSON.parse(detail)
    return { isJson: true, formatted: JSON.stringify(obj, null, 2) }
  } catch {
    return { isJson: false, formatted: detail }
  }
}

const eventTypeColors: Record<string, string> = {
  pipeline_agent_call: "bg-purple-50 text-purple-700",
  pipeline_agent_result: "bg-indigo-50 text-indigo-700",
  pipeline_completed: "bg-green-50 text-green-700",
  pipeline_failed: "bg-red-50 text-red-700",
  message_received: "bg-blue-50 text-blue-700",
  message_processed: "bg-cyan-50 text-cyan-700",
  message_classified: "bg-teal-50 text-teal-700",
  pipeline_skipped: "bg-gray-50 text-gray-500",
}

function AuditLogRow({ log }: { log: AuditLogItem }) {
  const [expanded, setExpanded] = useState(false)
  const { isJson, formatted } = tryFormatJson(log.detail)
  const colorClass = eventTypeColors[log.event_type] || "bg-blue-50 text-blue-700"

  return (
    <>
      <tr
        className="hover:bg-gray-50 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="px-4 py-3 text-gray-400">#{log.id}</td>
        <td className="px-4 py-3">
          <span className={`px-2 py-0.5 rounded-full text-xs ${colorClass}`}>
            {log.event_type}
          </span>
        </td>
        <td className="px-4 py-3 text-gray-600">
          {log.target_type && `${log.target_type}:${log.target_id}`}
        </td>
        <td className="px-4 py-3 text-gray-500 max-w-sm truncate">
          {isJson ? (
            <span className="text-gray-400 italic">JSON 详情</span>
          ) : (
            log.detail
          )}
        </td>
        <td className="px-4 py-3 text-gray-400">{log.operator}</td>
        <td className="px-4 py-3 text-gray-400 text-xs whitespace-nowrap">
          {formatDate(log.created_at)}
        </td>
        <td className="px-4 py-2 text-gray-300">
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} className="px-4 py-3 bg-gray-50 border-b border-gray-200">
            <pre className="text-xs text-gray-700 whitespace-pre-wrap break-all max-h-96 overflow-y-auto font-mono bg-white rounded-lg p-3 border border-gray-200">
              {formatted}
            </pre>
          </td>
        </tr>
      )}
    </>
  )
}

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
              <th className="w-8"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {items.map((log) => (
              <AuditLogRow key={log.id} log={log} />
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-gray-400">
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
