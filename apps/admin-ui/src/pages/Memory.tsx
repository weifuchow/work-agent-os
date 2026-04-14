import { useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Brain,
  CalendarClock,
  Plus,
  Save,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react"
import {
  consolidateMemory,
  createMemoryEntry,
  deleteMemoryEntry,
  fetchMemoryEntries,
  fetchMemoryEntry,
  fetchMemoryOverview,
  updateMemoryEntry,
  type MemoryEntryItem,
} from "../api/client"
import { cn } from "../lib/utils"

const SCOPE_OPTIONS = [
  { value: "", label: "全部作用域" },
  { value: "project", label: "项目" },
  { value: "personal", label: "个人偏好" },
  { value: "people", label: "人物" },
  { value: "general", label: "通用" },
]

const CATEGORY_OPTIONS = [
  { value: "", label: "全部类别" },
  { value: "decision", label: "决策" },
  { value: "milestone", label: "里程碑" },
  { value: "issue", label: "问题" },
  { value: "solution", label: "方案" },
  { value: "preference", label: "偏好" },
  { value: "person", label: "人物" },
  { value: "fact", label: "事实" },
  { value: "note", label: "备注" },
]

type ProjectFilterValue = "__all__" | "__personal__" | string

type EditableMemory = {
  id: number | null
  scope: string
  project_name: string
  project_version: string
  project_branch: string
  project_commit_sha: string
  project_commit_time: string
  category: string
  title: string
  content: string
  tags: string
  source_type: string
  source_session_id: number | null
  source_message_id: number | null
  importance: number
  happened_at: string
  valid_until: string
  first_seen_at: string
  last_seen_at: string
  occurrence_count: number
}

const EMPTY_FORM: EditableMemory = {
  id: null,
  scope: "project",
  project_name: "",
  project_version: "",
  project_branch: "",
  project_commit_sha: "",
  project_commit_time: "",
  category: "note",
  title: "",
  content: "",
  tags: "",
  source_type: "manual",
  source_session_id: null,
  source_message_id: null,
  importance: 3,
  happened_at: "",
  valid_until: "",
  first_seen_at: "",
  last_seen_at: "",
  occurrence_count: 1,
}

export default function Memory() {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | "new" | null>(null)
  const [dirty, setDirty] = useState(false)
  const [scope, setScope] = useState("")
  const [category, setCategory] = useState("")
  const [projectFilter, setProjectFilter] = useState<ProjectFilterValue>("__all__")
  const [q, setQ] = useState("")
  const [form, setForm] = useState<EditableMemory>(EMPTY_FORM)

  const listParams = useMemo(() => {
    const params: { project_name?: string; scope?: string; category?: string; q?: string } = {}
    if (scope) params.scope = scope
    if (category) params.category = category
    if (q.trim()) params.q = q.trim()
    if (projectFilter === "__personal__") params.project_name = ""
    if (projectFilter !== "__all__" && projectFilter !== "__personal__") params.project_name = projectFilter
    return params
  }, [category, projectFilter, q, scope])

  const { data: overview } = useQuery({
    queryKey: ["memory-overview"],
    queryFn: () => fetchMemoryOverview().then((r) => r.data),
  })

  const { data: entriesData, isLoading } = useQuery({
    queryKey: ["memory-entries", listParams],
    queryFn: () => fetchMemoryEntries(1, 100, listParams).then((r) => r.data),
  })

  const { data: detailData } = useQuery({
    queryKey: ["memory-entry", selectedId],
    queryFn: () => fetchMemoryEntry(selectedId as number).then((r) => r.data),
    enabled: typeof selectedId === "number",
  })

  useEffect(() => {
    const items = entriesData?.items ?? []
    if (!items.length) {
      if (selectedId !== "new") {
        setSelectedId("new")
        setForm(EMPTY_FORM)
        setDirty(false)
      }
      return
    }
    if (selectedId === null) {
      setSelectedId(items[0].id)
    }
  }, [entriesData, selectedId])

  useEffect(() => {
    if (!detailData || dirty) return
    setForm(toEditable(detailData))
  }, [detailData, dirty])

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = toPayload(form)
      if (form.id) {
        return updateMemoryEntry(form.id, payload).then((r) => r.data)
      }
      return createMemoryEntry(payload as { title: string; content: string }).then((r) => r.data)
    },
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["memory-entries"] })
      queryClient.invalidateQueries({ queryKey: ["memory-overview"] })
      queryClient.invalidateQueries({ queryKey: ["project-insights"] })
      queryClient.invalidateQueries({ queryKey: ["memory-entry", saved.id] })
      setSelectedId(saved.id)
      setForm(toEditable(saved))
      setDirty(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteMemoryEntry(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-entries"] })
      queryClient.invalidateQueries({ queryKey: ["memory-overview"] })
      queryClient.invalidateQueries({ queryKey: ["project-insights"] })
      setSelectedId(null)
      setForm(EMPTY_FORM)
      setDirty(false)
    },
  })

  const consolidateMutation = useMutation({
    mutationFn: () => consolidateMemory().then((r) => r.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-entries"] })
      queryClient.invalidateQueries({ queryKey: ["memory-overview"] })
      queryClient.invalidateQueries({ queryKey: ["project-insights"] })
    },
  })

  const entries = entriesData?.items ?? []
  const projectOptions = overview?.by_project ?? []

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-gray-900">记忆管理</h2>
          <p className="mt-1 text-sm text-gray-500">
            从项目会话、个人偏好和人物信息中增量提取并维护结构化长期记忆。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => consolidateMutation.mutate()}
            disabled={consolidateMutation.isPending}
            className="inline-flex items-center gap-2 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800 transition-colors hover:bg-amber-100 disabled:opacity-50"
          >
            <Sparkles size={16} />
            {consolidateMutation.isPending ? "提取中..." : "从会话提取记忆"}
          </button>
          <button
            onClick={() => {
              if (dirty && !confirm("当前有未保存修改，确定新建？")) return
              setSelectedId("new")
              setForm(EMPTY_FORM)
              setDirty(false)
            }}
            className="inline-flex items-center gap-2 rounded-2xl bg-gray-900 px-4 py-2 text-sm text-white transition-colors hover:bg-black"
          >
            <Plus size={16} />
            新建记忆
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <OverviewPill label="记忆总数" value={overview?.total ?? 0} />
        <OverviewPill label="项目记忆" value={overview?.by_scope.project ?? 0} />
        <OverviewPill label="个人偏好" value={overview?.by_scope.personal ?? 0} />
        <OverviewPill label="人物信息" value={overview?.by_scope.people ?? 0} />
      </div>

      <div className="grid h-[calc(100vh-15rem)] grid-cols-1 gap-4 xl:grid-cols-[360px_1fr]">
        <div className="flex min-h-0 flex-col rounded-3xl border border-gray-200 bg-white shadow-sm">
          <div className="border-b border-gray-100 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
              <Brain size={16} className="text-gray-600" />
              记忆条目
            </div>

            <div className="mt-4 space-y-3">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜索标题、内容、标签"
                  className="w-full rounded-2xl border border-gray-200 bg-gray-50 py-2 pl-9 pr-3 text-sm outline-none transition-colors focus:border-gray-400"
                />
              </div>

              <div className="grid grid-cols-1 gap-2">
                <select
                  value={projectFilter}
                  onChange={(e) => setProjectFilter(e.target.value)}
                  className="rounded-2xl border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-gray-400"
                >
                  <option value="__all__">全部项目</option>
                  <option value="__personal__">非项目 / 个人</option>
                  {projectOptions.map((item) => (
                    <option key={item.project_name} value={item.project_name}>
                      {item.project_name} ({item.count})
                    </option>
                  ))}
                </select>

                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={scope}
                    onChange={(e) => setScope(e.target.value)}
                    className="rounded-2xl border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-gray-400"
                  >
                    {SCOPE_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                  <select
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                    className="rounded-2xl border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-gray-400"
                  >
                    {CATEGORY_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-auto p-3">
            {entries.length > 0 ? (
              <div className="space-y-2">
                {entries.map((entry) => (
                  <button
                    key={entry.id}
                    onClick={() => {
                      if (dirty && entry.id !== selectedId && !confirm("有未保存修改，确定切换？")) return
                      setSelectedId(entry.id)
                      setDirty(false)
                    }}
                    className={cn(
                      "w-full rounded-2xl border p-3 text-left transition-all",
                      selectedId === entry.id
                        ? "border-gray-900 bg-gray-900 text-white"
                        : "border-gray-200 bg-gray-50 text-gray-900 hover:border-gray-300 hover:bg-white",
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">
                          {entry.title || `记忆 #${entry.id}`}
                        </div>
                        <div className={`mt-1 max-h-10 overflow-hidden text-xs ${selectedId === entry.id ? "text-gray-300" : "text-gray-500"}`}>
                          {entry.content}
                        </div>
                      </div>
                      <div className={`rounded-full px-2 py-1 text-[11px] ${selectedId === entry.id ? "bg-white/10 text-white" : "bg-stone-100 text-stone-700"}`}>
                        {entry.category}
                      </div>
                    </div>

                    <div className={`mt-3 flex flex-wrap gap-2 text-[11px] ${selectedId === entry.id ? "text-gray-300" : "text-gray-500"}`}>
                      <span>{entry.project_name || "非项目"}</span>
                      {entry.project_version && <span>版本 {entry.project_version}</span>}
                      {entry.project_branch && <span>分支 {entry.project_branch}</span>}
                      {entry.project_commit_sha && <span>commit {entry.project_commit_sha.slice(0, 8)}</span>}
                      <span>{entry.scope}</span>
                      <span>{formatDate(entry.happened_at || entry.updated_at)}</span>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-gray-400">
                当前筛选条件下没有记忆条目。
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col rounded-3xl border border-gray-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-gray-100 p-4">
            <div>
              <div className="text-sm font-medium text-gray-800">
                {form.id ? `记忆 #${form.id}` : "新建记忆"}
              </div>
              {dirty && <div className="mt-1 text-xs text-orange-500">当前有未保存修改</div>}
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={() => saveMutation.mutate()}
                disabled={!form.title.trim() || !form.content.trim() || saveMutation.isPending}
                className="inline-flex items-center gap-2 rounded-2xl bg-gray-900 px-4 py-2 text-sm text-white transition-colors hover:bg-black disabled:opacity-50"
              >
                <Save size={14} />
                {saveMutation.isPending ? "保存中..." : "保存"}
              </button>

              {form.id && (
                <button
                  onClick={() => {
                    const entryId = form.id
                    if (entryId && confirm(`确定删除记忆 #${entryId}？`)) {
                      deleteMutation.mutate(entryId)
                    }
                  }}
                  disabled={deleteMutation.isPending}
                  className="inline-flex items-center gap-2 rounded-2xl border border-red-200 px-3 py-2 text-sm text-red-600 transition-colors hover:bg-red-50 disabled:opacity-50"
                >
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-auto p-5">
            <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
              <Field label="标题">
                <input
                  value={form.title}
                  onChange={(e) => patchForm(setForm, setDirty, { title: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="项目">
                <input
                  value={form.project_name}
                  onChange={(e) => patchForm(setForm, setDirty, { project_name: e.target.value })}
                  placeholder="allspark / work-agent-os / 留空表示非项目"
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="项目版本">
                <input
                  value={form.project_version}
                  onChange={(e) => patchForm(setForm, setDirty, { project_version: e.target.value })}
                  placeholder="例如 3.0 / v4.8.0.6 / release-2026.04"
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="Git 分支">
                <input
                  value={form.project_branch}
                  onChange={(e) => patchForm(setForm, setDirty, { project_branch: e.target.value })}
                  placeholder="例如 master / release/3.0"
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="Commit SHA">
                <input
                  value={form.project_commit_sha}
                  onChange={(e) => patchForm(setForm, setDirty, { project_commit_sha: e.target.value })}
                  placeholder="例如 a1b2c3d4"
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="提交时间">
                <input
                  type="datetime-local"
                  value={form.project_commit_time}
                  onChange={(e) => patchForm(setForm, setDirty, { project_commit_time: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="作用域">
                <select
                  value={form.scope}
                  onChange={(e) => patchForm(setForm, setDirty, { scope: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                >
                  {SCOPE_OPTIONS.filter((item) => item.value).map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="类别">
                <select
                  value={form.category}
                  onChange={(e) => patchForm(setForm, setDirty, { category: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                >
                  {CATEGORY_OPTIONS.filter((item) => item.value).map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="重要度">
                <input
                  type="number"
                  min={1}
                  max={5}
                  value={form.importance}
                  onChange={(e) => patchForm(setForm, setDirty, { importance: Number(e.target.value) || 1 })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="出现次数">
                <input
                  type="number"
                  min={1}
                  value={form.occurrence_count}
                  onChange={(e) => patchForm(setForm, setDirty, { occurrence_count: Number(e.target.value) || 1 })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="发生时间">
                <input
                  type="datetime-local"
                  value={form.happened_at}
                  onChange={(e) => patchForm(setForm, setDirty, { happened_at: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="有效期至">
                <input
                  type="datetime-local"
                  value={form.valid_until}
                  onChange={(e) => patchForm(setForm, setDirty, { valid_until: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="首次记录">
                <input
                  type="datetime-local"
                  value={form.first_seen_at}
                  onChange={(e) => patchForm(setForm, setDirty, { first_seen_at: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="最近确认">
                <input
                  type="datetime-local"
                  value={form.last_seen_at}
                  onChange={(e) => patchForm(setForm, setDirty, { last_seen_at: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="标签（逗号分隔）">
                <input
                  value={form.tags}
                  onChange={(e) => patchForm(setForm, setDirty, { tags: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>

              <Field label="来源类型">
                <input
                  value={form.source_type}
                  onChange={(e) => patchForm(setForm, setDirty, { source_type: e.target.value })}
                  className="w-full rounded-2xl border border-gray-200 px-3 py-2 text-sm outline-none focus:border-gray-400"
                />
              </Field>
            </div>

            <div className="mt-4 rounded-3xl bg-gray-50 p-4">
              <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-gray-400">
                <CalendarClock size={14} />
                记忆正文
              </div>
              <textarea
                value={form.content}
                onChange={(e) => patchForm(setForm, setDirty, { content: e.target.value })}
                className="min-h-[280px] w-full resize-y rounded-2xl border border-gray-200 bg-white p-4 text-sm text-gray-800 outline-none focus:border-gray-400"
                placeholder="记录项目决策、问题与解决方案、个人偏好、人物信息等。"
              />
            </div>

            {(form.source_session_id || form.source_message_id) && (
              <div className="mt-4 grid grid-cols-1 gap-3 rounded-3xl border border-dashed border-gray-200 p-4 text-sm text-gray-500 md:grid-cols-2">
                <div>来源会话：{form.source_session_id || "-"}</div>
                <div>来源消息：{form.source_message_id || "-"}</div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-2 text-xs font-medium uppercase tracking-[0.18em] text-gray-400">{label}</div>
      {children}
    </label>
  )
}

function OverviewPill({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-gray-900">{value}</div>
    </div>
  )
}

function patchForm(
  setForm: Dispatch<SetStateAction<EditableMemory>>,
  setDirty: Dispatch<SetStateAction<boolean>>,
  patch: Partial<EditableMemory>,
) {
  setForm((current) => ({ ...current, ...patch }))
  setDirty(true)
}

function toEditable(entry: MemoryEntryItem): EditableMemory {
  return {
    id: entry.id,
    scope: entry.scope,
    project_name: entry.project_name,
    project_version: entry.project_version,
    project_branch: entry.project_branch,
    project_commit_sha: entry.project_commit_sha,
    project_commit_time: toInputDateTime(entry.project_commit_time),
    category: entry.category,
    title: entry.title,
    content: entry.content,
    tags: entry.tags.join(", "),
    source_type: entry.source_type,
    source_session_id: entry.source_session_id,
    source_message_id: entry.source_message_id,
    importance: entry.importance,
    happened_at: toInputDateTime(entry.happened_at),
    valid_until: toInputDateTime(entry.valid_until),
    first_seen_at: toInputDateTime(entry.first_seen_at),
    last_seen_at: toInputDateTime(entry.last_seen_at),
    occurrence_count: entry.occurrence_count,
  }
}

function toPayload(form: EditableMemory) {
  return {
    scope: form.scope,
    project_name: form.project_name.trim(),
    project_version: form.project_version.trim(),
    project_branch: form.project_branch.trim(),
    project_commit_sha: form.project_commit_sha.trim(),
    project_commit_time: form.project_commit_time || null,
    category: form.category,
    title: form.title.trim(),
    content: form.content.trim(),
    tags: form.tags.split(",").map((item) => item.trim()).filter(Boolean),
    source_type: form.source_type.trim() || "manual",
    source_session_id: form.source_session_id,
    source_message_id: form.source_message_id,
    importance: form.importance,
    happened_at: form.happened_at || null,
    valid_until: form.valid_until || null,
    first_seen_at: form.first_seen_at || null,
    last_seen_at: form.last_seen_at || null,
    occurrence_count: form.occurrence_count,
  }
}

function toInputDateTime(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ""
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, "0")
  const day = String(date.getDate()).padStart(2, "0")
  const hours = String(date.getHours()).padStart(2, "0")
  const minutes = String(date.getMinutes()).padStart(2, "0")
  return `${year}-${month}-${day}T${hours}:${minutes}`
}

function formatDate(value: string | null) {
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
