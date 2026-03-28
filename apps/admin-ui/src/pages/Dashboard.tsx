import { useQuery } from "@tanstack/react-query"
import { fetchStats } from "../api/client"
import { MessageSquare, Layers, CheckSquare, ScrollText } from "lucide-react"

export default function Dashboard() {
  const { data, isLoading } = useQuery({
    queryKey: ["stats"],
    queryFn: () => fetchStats().then((r) => r.data),
    refetchInterval: 10000,
  })

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  const stats = data!
  const cards = [
    { label: "消息总数", value: stats.messages, icon: MessageSquare, color: "blue" },
    { label: "工作会话", value: stats.sessions, icon: Layers, color: "green" },
    { label: "待办任务", value: stats.tasks, icon: CheckSquare, color: "orange" },
    { label: "审计日志", value: stats.audit_logs, icon: ScrollText, color: "purple" },
  ]

  const colorMap: Record<string, string> = {
    blue: "bg-blue-50 text-blue-700",
    green: "bg-green-50 text-green-700",
    orange: "bg-orange-50 text-orange-700",
    purple: "bg-purple-50 text-purple-700",
  }

  return (
    <div>
      <h2 className="text-xl font-bold text-gray-900 mb-6">仪表盘</h2>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {cards.map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="bg-white rounded-xl border border-gray-200 p-5">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-gray-500">{label}</span>
              <div className={`p-2 rounded-lg ${colorMap[color]}`}>
                <Icon size={18} />
              </div>
            </div>
            <div className="text-2xl font-bold text-gray-900">{value}</div>
          </div>
        ))}
      </div>

      {/* Classification breakdown */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h3 className="text-sm font-medium text-gray-700 mb-4">消息分类分布</h3>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {Object.entries(stats.classification).map(([type, count]) => (
            <div key={type} className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-lg font-semibold text-gray-900">{count}</div>
              <div className="text-xs text-gray-500">{type}</div>
            </div>
          ))}
          {Object.keys(stats.classification).length === 0 && (
            <div className="text-sm text-gray-400 col-span-5">暂无分类数据</div>
          )}
        </div>
      </div>
    </div>
  )
}
