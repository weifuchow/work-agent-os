import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Bot, Check } from "lucide-react"
import { fetchAgentRuntime, switchAgentRuntime } from "../api/client"
import { cn } from "../lib/utils"

const LABELS: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex Agent",
}

export default function AgentRuntimeSwitcher() {
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ["agent-runtime"],
    queryFn: () => fetchAgentRuntime().then((r) => r.data),
    refetchInterval: 10000,
  })

  const mutation = useMutation({
    mutationFn: (runtime: string) => switchAgentRuntime(runtime),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-runtime"] })
      queryClient.invalidateQueries({ queryKey: ["models"] })
      queryClient.invalidateQueries({ queryKey: ["sessions"] })
      queryClient.invalidateQueries({ queryKey: ["task-contexts"] })
    },
  })

  if (!data) return null

  return (
    <div className="flex items-center gap-1 rounded-lg border border-gray-200 bg-gray-50 p-1">
      <div className="flex items-center gap-1.5 px-2 py-1 text-xs text-gray-500">
        <Bot size={14} />
        <span className="font-medium">Agent</span>
      </div>
      {data.supported.map((runtime) => {
        const active = data.current === runtime
        return (
          <button
            key={runtime}
            onClick={() => mutation.mutate(runtime)}
            disabled={mutation.isPending && active}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
              active
                ? "bg-white text-blue-700 shadow-sm"
                : "text-gray-500 hover:bg-white hover:text-gray-700"
            )}
          >
            <span>{LABELS[runtime] || runtime}</span>
            {active && <Check size={12} />}
          </button>
        )
      })}
    </div>
  )
}
