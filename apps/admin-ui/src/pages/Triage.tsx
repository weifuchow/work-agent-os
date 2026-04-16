import { useEffect, useMemo, useState, type ComponentType } from "react"
import { useQuery } from "@tanstack/react-query"
import { useSearchParams } from "react-router-dom"
import { AlertCircle, CheckCircle2, CircleDashed, Search, ShieldAlert } from "lucide-react"
import {
  fetchTriageRunDetail,
  fetchTriageRuns,
} from "../api/client"
import { formatDate } from "../lib/utils"

const REFRESH_MS = 5000

export default function Triage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedSlug = searchParams.get("run") || ""
  const [selectedSlug, setSelectedSlug] = useState("")

  const { data, isLoading } = useQuery({
    queryKey: ["triage-runs"],
    queryFn: () => fetchTriageRuns().then((r) => r.data),
    refetchInterval: REFRESH_MS,
  })

  const runs = data?.items ?? []

  useEffect(() => {
    if (!runs.length) {
      setSelectedSlug("")
      return
    }
    if (requestedSlug && runs.some((item) => item.slug === requestedSlug)) {
      if (selectedSlug !== requestedSlug) {
        setSelectedSlug(requestedSlug)
      }
      return
    }
    if (!selectedSlug || !runs.some((item) => item.slug === selectedSlug)) {
      setSelectedSlug(runs[0].slug)
    }
  }, [runs, selectedSlug, requestedSlug])

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["triage-run", selectedSlug],
    queryFn: () => fetchTriageRunDetail(selectedSlug).then((r) => r.data),
    enabled: Boolean(selectedSlug),
    refetchInterval: REFRESH_MS,
  })

  const stats = useMemo(() => {
    const awaiting = runs.filter((item) => item.phase === "awaiting_input").length
    const ready = runs.filter((item) => item.phase === "ready_for_report").length
    const high = runs.filter((item) => item.confidence === "high").length
    return { total: runs.length, awaiting, ready, high }
  }, [runs])

  if (isLoading) {
    return <div className="text-gray-500">加载中...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-gray-900">日志排障观察台</h2>
          <p className="mt-1 text-sm text-gray-500">
            观察 `.triage` 下的排障状态、缺口、关键词搜索结果和关键证据。
          </p>
        </div>
        <div className="text-xs text-gray-400">自动刷新 {REFRESH_MS / 1000}s</div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <MetricCard label="排障运行" value={stats.total} icon={CircleDashed} tone="bg-slate-50 text-slate-700" />
        <MetricCard label="等待补料" value={stats.awaiting} icon={AlertCircle} tone="bg-amber-50 text-amber-700" />
        <MetricCard label="可出报告" value={stats.ready} icon={CheckCircle2} tone="bg-emerald-50 text-emerald-700" />
        <MetricCard label="高置信" value={stats.high} icon={ShieldAlert} tone="bg-sky-50 text-sky-700" />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium text-gray-700">运行列表</h3>
              <p className="mt-1 text-xs text-gray-400">按更新时间排序，展示 phase、置信度和最近命中。</p>
            </div>
            <div className="text-xs text-gray-400">{runs.length} 条</div>
          </div>

          <div className="mt-4 space-y-3">
            {runs.map((run) => (
              <button
                key={run.slug}
                onClick={() => {
                  setSelectedSlug(run.slug)
                  setSearchParams({ run: run.slug })
                }}
                className={`w-full rounded-3xl border p-4 text-left transition-all ${
                  run.slug === selectedSlug
                    ? "border-gray-900 bg-gray-900 text-white shadow-lg"
                    : "border-gray-200 bg-gray-50 text-gray-900 hover:border-gray-300 hover:bg-white"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold">{run.problem_summary || run.slug}</div>
                    <div className={`mt-1 text-xs ${run.slug === selectedSlug ? "text-gray-300" : "text-gray-500"}`}>
                      {run.project || "未指定项目"} · {run.phase || "unknown"} · {run.confidence || "low"}
                    </div>
                  </div>
                  <div className={`rounded-full px-2 py-1 text-[11px] ${chipTone(run.confidence, run.slug === selectedSlug)}`}>
                    {run.confidence || "unknown"}
                  </div>
                </div>

                <div className={`mt-3 flex flex-wrap gap-2 text-[11px] ${run.slug === selectedSlug ? "text-gray-300" : "text-gray-500"}`}>
                  <span>搜索 {run.search_status || "pending"}</span>
                  <span>证据链 {run.evidence_chain_status || "weak"}</span>
                  <span>缺口 {run.missing_items.length}</span>
                  <span>命中 {run.latest_search?.hits_total ?? 0}</span>
                  {run.route_mode ? <span>路由 {run.route_mode}</span> : null}
                  {run.final_action ? <span>动作 {run.final_action}</span> : null}
                  {run.has_process_trace ? <span>过程已记录</span> : null}
                </div>

                {run.missing_items.length > 0 && (
                  <div className={`mt-3 line-clamp-2 text-xs ${run.slug === selectedSlug ? "text-gray-300" : "text-gray-500"}`}>
                    缺口：{run.missing_items.join("，")}
                  </div>
                )}

                <div className={`mt-3 text-[11px] ${run.slug === selectedSlug ? "text-gray-400" : "text-gray-400"}`}>
                  更新于 {formatDate(run.updated_at)}
                </div>
              </button>
            ))}

            {runs.length === 0 && (
              <div className="rounded-3xl border border-dashed border-gray-200 bg-gray-50 px-4 py-8 text-center text-sm text-gray-400">
                当前没有 `.triage` 运行。
              </div>
            )}
          </div>
        </section>

        <section className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
          {!selectedSlug || detailLoading || !detail ? (
            <div className="text-gray-500">选择一条排障运行查看详情。</div>
          ) : (
            <div className="space-y-6">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div>
                  <h3 className="text-xl font-semibold text-gray-900">{detail.problem_summary || detail.slug}</h3>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-500">
                    <Badge label={`项目 ${detail.project || "未指定"}`} />
                    <Badge label={`阶段 ${detail.phase || "unknown"}`} />
                    <Badge label={`置信度 ${detail.confidence || "low"}`} />
                    <Badge label={`搜索 ${detail.search_status || "pending"}`} />
                    <Badge label={`证据链 ${detail.evidence_chain_status || "weak"}`} />
                  </div>
                </div>
                <div className="text-right text-xs text-gray-400">
                  <div>创建于 {formatDate(detail.created_at)}</div>
                  <div className="mt-1">更新于 {formatDate(detail.updated_at)}</div>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                <InfoBlock title="模块假设" items={detail.module_hypothesis} empty="暂无模块假设" />
                <InfoBlock title="目标日志文件" items={detail.target_log_files} empty="暂无目标文件" />
                <InfoBlock title="缺失项" items={detail.missing_items} empty="当前无缺失项" />
              </div>

              <div className="rounded-3xl border border-gray-200 bg-white p-4">
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">分析过程</div>
                {detail.has_process_trace ? (
                  <div className="mt-3 space-y-4">
                    <div className="flex flex-wrap gap-2 text-xs text-gray-500">
                      {detail.route_mode ? <Badge label={`路由 ${detail.route_mode}`} /> : null}
                      {detail.final_action ? <Badge label={`动作 ${detail.final_action}`} /> : null}
                      {readArtifactValue(detail.final_decision, "project_name") ? (
                        <Badge label={`最终项目 ${readArtifactValue(detail.final_decision, "project_name")}`} />
                      ) : null}
                      {detail.analysis_trace?.runtime ? (
                        <Badge label={`运行时 ${detail.analysis_trace.runtime}`} />
                      ) : null}
                    </div>

                    {detail.analysis_trace?.rollout_path ? (
                      <div className="rounded-2xl bg-gray-50 px-3 py-2 text-xs text-gray-600">
                        rollout: {detail.analysis_trace.rollout_path}
                      </div>
                    ) : null}

                    {detail.analysis_trace?.steps && detail.analysis_trace.steps.length > 0 ? (
                      <div className="space-y-3">
                        {detail.analysis_trace.steps.map((step) => (
                          <div key={`${step.index}-${step.timestamp}`} className="rounded-2xl border border-gray-200 p-4">
                            <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                              <div className="text-sm font-medium text-gray-900">
                                {step.index}. {step.title}
                              </div>
                              <div className="text-xs text-gray-400">
                                {step.kind}{step.timestamp ? ` · ${formatDate(step.timestamp)}` : ""}
                              </div>
                            </div>
                            <pre className="mt-3 overflow-auto rounded-2xl bg-gray-50 p-3 text-xs leading-5 text-gray-700 whitespace-pre-wrap">
                              {step.detail}
                            </pre>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="text-sm text-gray-400">当前没有可展示的过程步骤。</div>
                    )}

                    {detail.analysis_trace?.markdown_content || detail.analysis_trace?.markdown_preview ? (
                      <div className="rounded-2xl bg-gray-50 p-4">
                        <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">Trace 摘要</div>
                        <pre className="mt-3 whitespace-pre-wrap text-sm leading-6 text-gray-700">
                          {detail.analysis_trace?.markdown_content || detail.analysis_trace?.markdown_preview}
                        </pre>
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <div className="mt-3 text-sm text-gray-400">当前没有已落盘的分析过程。</div>
                )}
              </div>

              <div className="rounded-3xl bg-slate-50 p-4">
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">最近搜索</div>
                {detail.latest_search ? (
                  <div className="mt-3 space-y-4">
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <MiniStat label="命中" value={detail.latest_search.hits_total} />
                      <MiniStat label="未命中词" value={detail.latest_search.unmatched_terms.length} />
                      <MiniStat label="Top 文件" value={detail.latest_search.top_files.length} />
                    </div>

                    <div>
                      <div className="text-xs font-medium text-slate-600">命中词</div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {detail.latest_search.matched_terms.length > 0 ? (
                          detail.latest_search.matched_terms.map((term) => (
                            <span key={term} className="rounded-full bg-white px-3 py-1 text-xs text-slate-700">
                              {term}
                            </span>
                          ))
                        ) : (
                          <span className="text-sm text-slate-400">暂无命中词</span>
                        )}
                      </div>
                    </div>

                    <div>
                      <div className="text-xs font-medium text-slate-600">Top 文件</div>
                      <div className="mt-2 space-y-2">
                        {detail.latest_search.top_files.length > 0 ? (
                          detail.latest_search.top_files.map((item) => (
                            <div key={item.path} className="rounded-2xl bg-white px-3 py-2 text-xs text-slate-700">
                              {item.path} · {item.hits} hits
                            </div>
                          ))
                        ) : (
                          <div className="text-sm text-slate-400">暂无文件命中</div>
                        )}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="mt-3 text-sm text-slate-400">还没有搜索结果。</div>
                )}
              </div>

              <div>
                <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
                  <Search size={16} className="text-gray-500" />
                  关键证据
                </div>
                <div className="mt-3 space-y-3">
                  {detail.latest_search?.evidence_hits && detail.latest_search.evidence_hits.length > 0 ? (
                    detail.latest_search.evidence_hits.map((hit, index) => (
                      <div key={`${hit.path}-${hit.line_number}-${index}`} className="rounded-2xl border border-gray-200 p-4">
                        <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                          <div className="text-sm font-medium text-gray-900">{hit.path}</div>
                          <div className="text-xs text-gray-400">
                            line {hit.line_number}{hit.timestamp ? ` · ${hit.timestamp}` : ""}
                          </div>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {hit.matched_terms.map((term) => (
                            <span key={term} className="rounded-full bg-gray-100 px-2.5 py-1 text-[11px] text-gray-700">
                              {term}
                            </span>
                          ))}
                        </div>
                        <pre className="mt-3 overflow-auto rounded-2xl bg-gray-50 p-3 text-xs leading-5 text-gray-700 whitespace-pre-wrap">
                          {hit.excerpt}
                        </pre>
                      </div>
                    ))
                  ) : (
                    <div className="rounded-2xl border border-dashed border-gray-200 bg-gray-50 px-4 py-6 text-sm text-gray-400">
                      当前没有可展示的关键证据。
                    </div>
                  )}
                </div>
              </div>

              <div className="rounded-3xl border border-gray-200 bg-white p-4">
                <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">摘要预览</div>
                <pre className="mt-3 whitespace-pre-wrap text-sm leading-6 text-gray-700">
                  {detail.latest_search?.summary_content || detail.latest_search?.summary_preview || "暂无摘要内容"}
                </pre>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function MetricCard({
  label,
  value,
  icon: Icon,
  tone,
}: {
  label: string
  value: number
  icon: ComponentType<{ size?: number }>
  tone: string
}) {
  return (
    <div className="rounded-3xl border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-500">{label}</span>
        <div className={`rounded-2xl p-2 ${tone}`}>
          <Icon size={18} />
        </div>
      </div>
      <div className="mt-4 text-3xl font-semibold tracking-tight text-gray-900">{value}</div>
    </div>
  )
}

function Badge({ label }: { label: string }) {
  return <span className="rounded-full bg-gray-100 px-3 py-1">{label}</span>
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl bg-white p-3 text-center">
      <div className="text-lg font-semibold text-gray-900">{value}</div>
      <div className="mt-1 text-xs text-gray-500">{label}</div>
    </div>
  )
}

function InfoBlock({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <div className="rounded-3xl border border-gray-200 bg-gray-50 p-4">
      <div className="text-xs font-medium uppercase tracking-[0.18em] text-gray-400">{title}</div>
      <div className="mt-3 space-y-2">
        {items.length > 0 ? (
          items.map((item) => (
            <div key={item} className="rounded-2xl bg-white px-3 py-2 text-sm text-gray-700">
              {item}
            </div>
          ))
        ) : (
          <div className="text-sm text-gray-400">{empty}</div>
        )}
      </div>
    </div>
  )
}

function chipTone(confidence: string, inverse: boolean) {
  if (inverse) {
    if (confidence === "high") return "bg-white/15 text-white"
    if (confidence === "medium") return "bg-white/15 text-white"
    return "bg-white/10 text-white"
  }
  if (confidence === "high") return "bg-emerald-50 text-emerald-700"
  if (confidence === "medium") return "bg-amber-50 text-amber-700"
  return "bg-gray-100 text-gray-600"
}

function readArtifactValue(
  artifact: { payload: Record<string, unknown> } | null | undefined,
  key: string,
) {
  const value = artifact?.payload?.[key]
  return typeof value === "string" ? value : ""
}
