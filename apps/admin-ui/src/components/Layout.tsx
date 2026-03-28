import { NavLink, Outlet } from "react-router-dom"
import {
  LayoutDashboard,
  MessageSquare,
  MessagesSquare,
  Layers,
  FlaskConical,
  ScrollText,
} from "lucide-react"
import { cn } from "../lib/utils"

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "仪表盘" },
  { to: "/conversations", icon: MessagesSquare, label: "对话记录" },
  { to: "/messages", icon: MessageSquare, label: "消息记录" },
  { to: "/sessions", icon: Layers, label: "工作会话" },
  { to: "/playground", icon: FlaskConical, label: "模型测试" },
  { to: "/audit-logs", icon: ScrollText, label: "审计日志" },
]

export default function Layout() {
  return (
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-56 bg-white border-r border-gray-200 flex flex-col">
        <div className="p-4 border-b border-gray-200">
          <h1 className="text-lg font-bold text-gray-900">Work Agent OS</h1>
          <p className="text-xs text-gray-500">管理后台</p>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors",
                  isActive
                    ? "bg-blue-50 text-blue-700 font-medium"
                    : "text-gray-600 hover:bg-gray-100"
                )
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <div className="p-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
