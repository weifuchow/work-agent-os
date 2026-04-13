import { useState, useRef, useEffect } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { fetchAgentRuntime, fetchModels, switchModel } from "../api/client"
import { Cpu, Check, ChevronDown } from "lucide-react"
import { cn } from "../lib/utils"

export default function ModelSwitcher() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [customInput, setCustomInput] = useState("")
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const { data: runtimeData } = useQuery({
    queryKey: ["agent-runtime"],
    queryFn: () => fetchAgentRuntime().then((r) => r.data),
    refetchInterval: 10000,
  })
  const runtime = runtimeData?.current || "claude"

  const { data } = useQuery({
    queryKey: ["models", runtime],
    queryFn: () => fetchModels(runtime).then((r) => r.data),
    refetchInterval: 10000,
    enabled: !!runtime,
  })

  const mutation = useMutation({
    mutationFn: (model: string) => switchModel(model, runtime),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] })
      setOpen(false)
      setCustomInput("")
    },
  })

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [])

  useEffect(() => {
    if (open && inputRef.current) {
      inputRef.current.focus()
    }
  }, [open])

  if (!data) return null

  const current = data.current || data.default || "unknown"
  const allModels = data.models || []
  // Show all models in dropdown, including disabled ones
  const filtered = customInput
    ? allModels.filter((m) => m.id.toLowerCase().includes(customInput.toLowerCase()))
    : allModels
  const customIsNew = customInput && !allModels.some((m) => m.id === customInput.trim())

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          "flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm border transition-colors",
          data.override
            ? "bg-amber-50 border-amber-200 text-amber-700 hover:bg-amber-100"
            : "bg-gray-50 border-gray-200 text-gray-700 hover:bg-gray-100"
        )}
      >
        <Cpu size={14} />
        <span className="font-medium max-w-[200px] truncate">{current}</span>
        {data.override && <span className="text-xs opacity-60">(override)</span>}
        <ChevronDown size={14} className={cn("transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-72 bg-white rounded-lg border border-gray-200 shadow-lg z-50">
          {/* Search / custom input */}
          <div className="p-2 border-b border-gray-100">
            <input
              ref={inputRef}
              type="text"
              value={customInput}
              onChange={(e) => setCustomInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && customInput.trim()) {
                  mutation.mutate(customInput.trim())
                }
              }}
              placeholder="搜索或输入模型 ID..."
              className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-400"
            />
          </div>

          <div className="max-h-60 overflow-y-auto py-1">
            {filtered.map((m) => (
              <button
                key={m.id}
                onClick={() => mutation.mutate(m.id)}
                className={cn(
                  "w-full flex items-center justify-between px-3 py-2 text-sm hover:bg-gray-50 transition-colors",
                  m.id === current && "bg-blue-50"
                )}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span className={cn(
                    "w-1.5 h-1.5 rounded-full flex-shrink-0",
                    m.enabled ? "bg-green-400" : "bg-gray-300"
                  )} />
                  <span className="truncate">{m.label || m.id}</span>
                  <span className="text-xs text-gray-400 flex-shrink-0">{m.provider}</span>
                </div>
                {m.id === current && <Check size={14} className="text-blue-600 flex-shrink-0" />}
              </button>
            ))}

            {/* Custom model option */}
            {customIsNew && (
              <button
                onClick={() => mutation.mutate(customInput.trim())}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-amber-50 text-amber-700 border-t border-gray-100"
              >
                <span className="text-xs bg-amber-100 px-1.5 py-0.5 rounded">自定义</span>
                <span className="truncate">{customInput.trim()}</span>
              </button>
            )}

            {filtered.length === 0 && !customIsNew && (
              <div className="px-3 py-4 text-sm text-gray-400 text-center">无匹配模型</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
