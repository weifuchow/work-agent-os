import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import {
  Activity,
  BrainCircuit,
  FolderKanban,
  MessageSquareMore,
  Sparkles,
  UserRound,
} from "lucide-react"
import {
  fetchProjectInsights,
  generateProjectSummary,
} from "../api/client"

const PERIOD_OPTIONS = [7, 30, 90]

export default function Dashboard() {
  const [days, setDays] = useState(30)
  const [selectedProject, setSelectedProject] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["project-insights", days],
    queryFn: () => fetchProjectInsights(days).then((r) => r.data),
    refetchInterval: 15000,
  })

  const projects = data?.projects ?? []

  useEffect(() => {
    if (!projects.length) {
      setSelectedProject("")
      return
    }
    if (!selectedProject || !projects.some((item) => item.name === selectedProject)) {
      setSelectedProject(projects[0].name)
    }
  }, [projects, selectedProject])

  const currentProject = useMemo(
    () => projects.find((item) => item.name === selectedProject) ?? projects[0] ?? null,
    [projects, selectedProject],
  )

  const summaryMutation = useMutation({
    mutationFn: (projectName: string) => generateProjectSummary(projectName, days).then((r) => r.data),
  })

  if (isLoading || !data) {
    return <div className="text-gray-500">加载中...</div>
  }

  const cards = [
    {
      label: "已注册项目",
      value: data.overview.registered_projects,
      icon: FolderKanban,
      tone: "bg-amber-50 text-amber-700",
    },
    {
      label: `${days} 天项目会话`,
      value: data.overview.project_sessions,
      icon: MessageSquareMore,
      tone: "bg-emerald-50 text-emerald-700",
    },
    {
      label: "活跃项目会话",
      value: data.overview.active_project_sessions,
      icon: Activity,
      tone: "bg-sky-50 text-sky-700",
    },
    {
      label: "结构化记忆",
      value: data.overview.structured_memories,
      icon: BrainCircuit,
      tone: "bg-stone-100 text-stone-700",
    },
  ]

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-gray-900">项目仪表盘</h2>
          <p className="mt-1 text-sm text-gray-500">
            以项目为中心查看会话、问题热点、长期记忆和个人偏好。
          </p>
        </div>

        <div className="flex items-center gap-2 rounded-2xl border border-gray-200 bg-white p-1.5">
          {PERIOD_OPTIONS.map((value) => (
            <button
              key={value}
              onClick={() => setDays(value)}
              className={`rounded-xl px-3 py-1.5 text-sm transition-colors ${
                value === days
                  ? "bg-gray-900 text-white"
                  : "text-gray-500 hover:bg-gray-100"
              }`}
            >
              近 {value} 天
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {cards.map(({ label, value, icon: Icon, tone }) => (
          <div key={label} className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-500">{label}</span>
              <div className={`rounded-2xl p-2 ${tone}`}>
                <Icon size={18} />
              </div>
            </div>
            <div className="mt-4 text-3xl font-semibold tracking-tight text-gray-900">{value}</div>
          </div>
        ))}
      </div>

      <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
          <Sparkles size={16} className="text-amber-600" />
          高频问题热点
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {data.hot_topics.length > 0 ? (
            data.hot_topics.map((item) => (
              <span
                key={`${item.project_name}-${item.topic}`}
                className="rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs text-amber-800"
              >
                {item.project_name} · {item.topic} · {item.count}
              </span>
            ))
          ) : (
            <span className="text-sm text-gray-400">当前没有足够的热点主题数据。</span>
          )}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium text-gray-700">项目概览</h3>
              <p className="mt-1 text-xs text-gray-400">按会话量、活跃度与记忆规模快速定位重点项目。</p>
            </div>
            <div className="text-xs text-gray-400">{projects.length} 个项目</div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
            {projects.map((project) => (
              <button
                key={project.name}
                onClick={() => setSelectedProject(project.name)}
                className={`rounded-3xl border p-4 text-left transition-all ${
                  currentProject?.name === project.name
                    ? "border-gray-900 bg-gray-900 text-white shadow-lg"
                    : "border-gray-200 bg-gray-50 text-gray-900 hover:border-gray-300 hover:bg-white"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-base font-semibold">{project.name}</div>
                    <div className={`mt-1 text-xs ${currentProject?.name === project.name ? "text-gray-300" : "text-gray-500"}`}>
                      {project.description || "暂无项目描述"}
                    </div>
                    {(project.git_version || project.git_branch || project.git_commit_sha) && (
                      <div className={`mt-2 flex flex-wrap gap-2 text-[11px] ${currentProject?.name === project.name ? "text-gray-300" : "text-gray-500"}`}>
                        {project.git_version && <span>版本 {project.git_version}</span>}
                        {project.git_branch && <span>分支 {project.git_branch}</span>}
                        {project.git_commit_sha && <span>commit {project.git_commit_sha.slice(0, 8)}</span>}
                      </div>
                    )}
                  </div>
                  {!project.path_exists && (
                    <span className={`rounded-full px-2 py-1 text-[11px] ${
                      currentProject?.name === project.name ? "bg-white/15 text-white" : "bg-red-50 text-red-600"
                    }`}>
                      路径缺失
                    </span>
                  )}
                </div>

                <div className="mt-4 grid grid-cols-3 gap-2 text-center">
                  <MetricPill
                    label="会话"
                    value={project.session_count}
                    inverse={currentProject?.name === project.name}
                  />
                  <MetricPill
                    label="记忆"
                    value={project.memory_count}
                    inverse={currentProject?.name === project.name}
                  />
                  <MetricPill
                    label="活跃"
                    value={project.active_recent_sessions}
                    inverse={currentProject?.name === project.name}
                  />
                </div>

                <div className={`mt-4 text-xs ${currentProject?.name === project.name ? "text-gray-300" : "text-gray-500"}`}>
                  高频主题：{project.top_topics[0]?.topic || "暂无"}
                </div>
              </button>
            ))}
          </div>
        </section>

        <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
            <UserRound size={16} className="text-teal-700" />
            个人偏好 / 非项目闲聊
          </div>
          <div className="mt-4 grid grid-cols-3 gap-3">
            <SmallStat label="会话" value={data.personal.session_count} />
            <SmallStat label="记忆" value={data.personal.memory_count} />
            <SmallStat label="活跃" value={data.personal.active_recent_sessions} />
          </div>
          <div className="mt-5">
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">偏好记忆</div>
            <div className="mt-3 space-y-3">
              {data.personal.preferences.length > 0 ? (
                data.personal.preferences.map((item) => (
                  <div key={item.id} className="rounded-2xl bg-teal-50/60 p-3">
                    <div className="text-sm font-medium text-gray-900">{item.title}</div>
                    <div className="mt-1 text-xs text-gray-500">{item.content}</div>
                  </div>
                ))
              ) : (
                <div className="text-sm text-gray-400">还没有沉淀出个人偏好记忆。</div>
              )}
            </div>
          </div>
        </section>
      </div>

      {currentProject && (
        <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <h3 className="text-xl font-semibold text-gray-900">{currentProject.name}</h3>
              <p className="mt-1 max-w-3xl text-sm text-gray-500">
                {currentProject.description || "暂无项目描述。"}
              </p>
              {(currentProject.git_version || currentProject.git_branch || currentProject.git_commit_sha) && (
                <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-500">
                  {currentProject.git_version && <span className="rounded-full bg-gray-100 px-3 py-1">版本 {currentProject.git_version}</span>}
                  {currentProject.git_branch && <span className="rounded-full bg-gray-100 px-3 py-1">分支 {currentProject.git_branch}</span>}
                  {currentProject.git_commit_sha && <span className="rounded-full bg-gray-100 px-3 py-1">commit {currentProject.git_commit_sha.slice(0, 8)}</span>}
                  {currentProject.git_commit_time && <span className="rounded-full bg-gray-100 px-3 py-1">提交 {formatTime(currentProject.git_commit_time)}</span>}
                </div>
              )}
            </div>
            <button
              onClick={() => summaryMutation.mutate(currentProject.name)}
              disabled={summaryMutation.isPending}
              className="inline-flex items-center gap-2 self-start rounded-2xl bg-gray-900 px-4 py-2 text-sm text-white transition-colors hover:bg-black disabled:opacity-50"
            >
              <Sparkles size={16} />
              {summaryMutation.isPending ? "生成中..." : "AI 总结问题类"}
            </button>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-[0.95fr_1.05fr]">
            <div className="space-y-6">
              <div>
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">问题分类分布</div>
                <div className="mt-3 space-y-2">
                  {Object.entries(currentProject.classification).length > 0 ? (
                    Object.entries(currentProject.classification).map(([key, value]) => (
                      <BarRow key={key} label={key} value={value} max={Math.max(...Object.values(currentProject.classification))} />
                    ))
                  ) : (
                    <div className="text-sm text-gray-400">当前没有分类统计。</div>
                  )}
                </div>
              </div>

              <div>
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">高频问题</div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {currentProject.top_topics.length > 0 ? (
                    currentProject.top_topics.map((item) => (
                      <span
                        key={item.topic}
                        className="rounded-full border border-gray-200 bg-gray-50 px-3 py-1 text-xs text-gray-700"
                      >
                        {item.topic} · {item.count}
                      </span>
                    ))
                  ) : (
                    <span className="text-sm text-gray-400">暂无高频问题。</span>
                  )}
                </div>
              </div>

              <div>
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">记忆分布</div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {Object.entries(currentProject.memory_by_category).length > 0 ? (
                    Object.entries(currentProject.memory_by_category).map(([key, value]) => (
                      <span
                        key={key}
                        className="rounded-full bg-stone-100 px-3 py-1 text-xs text-stone-700"
                      >
                        {key} · {value}
                      </span>
                    ))
                  ) : (
                    <span className="text-sm text-gray-400">该项目还没有结构化记忆。</span>
                  )}
                </div>
              </div>

              <div className="rounded-3xl bg-gradient-to-br from-amber-50 via-white to-orange-50 p-4">
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-amber-700">AI 问题总结</div>
                <div className="mt-3 whitespace-pre-wrap text-sm leading-6 text-gray-700">
                  {summaryMutation.data?.project_name === currentProject.name
                    ? summaryMutation.data.summary
                    : "点击右上角按钮，按当前时间窗口对该项目的问题类和高频议题做一次模型总结。"}
                </div>
              </div>
            </div>

            <div className="space-y-6">
              <div>
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">近期会话</div>
                <div className="mt-3 space-y-3">
                  {currentProject.recent_sessions.length > 0 ? (
                    currentProject.recent_sessions.map((session) => (
                      <div key={session.id} className="rounded-2xl border border-gray-200 p-4">
                        <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                          <div>
                            <div className="text-sm font-medium text-gray-900">
                              {session.title || session.topic || `Session #${session.id}`}
                            </div>
                            <div className="mt-1 text-xs text-gray-500">
                              {session.topic || "无主题"} · {session.status} · {session.message_count} 条消息
                            </div>
                          </div>
                          <div className="text-xs text-gray-400">
                            {formatTime(session.last_active_at)}
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="text-sm text-gray-400">当前时间窗口没有会话记录。</div>
                  )}
                </div>
              </div>

              <div>
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">最近沉淀的项目记忆</div>
                <div className="mt-3 space-y-3">
                  {currentProject.memory_highlights.length > 0 ? (
                    currentProject.memory_highlights.map((item) => (
                      <div key={item.id} className="rounded-2xl bg-stone-50 p-4">
                        <div className="text-sm font-medium text-gray-900">{item.title}</div>
                        <div className="mt-1 text-xs text-gray-500">
                          {item.category} · {formatTime(item.updated_at)}
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="text-sm text-gray-400">该项目还没有可展示的记忆亮点。</div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  )
}

function MetricPill({ label, value, inverse = false }: { label: string; value: number; inverse?: boolean }) {
  return (
    <div className={`rounded-2xl px-3 py-2 ${inverse ? "bg-white/10 text-white" : "bg-white text-gray-900"}`}>
      <div className={`text-[11px] ${inverse ? "text-white/70" : "text-gray-500"}`}>{label}</div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
    </div>
  )
}

function SmallStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl bg-gray-50 p-3 text-center">
      <div className="text-lg font-semibold text-gray-900">{value}</div>
      <div className="mt-1 text-xs text-gray-500">{label}</div>
    </div>
  )
}

function BarRow({ label, value, max }: { label: string; value: number; max: number }) {
  const width = max > 0 ? `${Math.max((value / max) * 100, 8)}%` : "0%"
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs text-gray-500">
        <span>{label}</span>
        <span>{value}</span>
      </div>
      <div className="h-2 rounded-full bg-gray-100">
        <div className="h-2 rounded-full bg-gray-900" style={{ width }} />
      </div>
    </div>
  )
}

function formatTime(value: string | null | undefined) {
  if (!value) return "暂无时间"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date)
}
