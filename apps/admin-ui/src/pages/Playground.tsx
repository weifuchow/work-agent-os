import { useState, useRef, useEffect, useCallback } from "react"
import { useQuery } from "@tanstack/react-query"
import { playgroundChat, type PlaygroundMessage } from "../api/client"
import { Send, Loader2, Settings, Bot, MessageSquare, Wrench, RotateCcw } from "lucide-react"
import { cn } from "../lib/utils"
import axios from "axios"

interface ChatItem {
  role: "user" | "assistant" | "tool"
  content: string
  toolName?: string
  toolInput?: string
}

interface SkillInfo {
  name: string
  description: string
}

export default function Playground() {
  const [chatItems, setChatItems] = useState<ChatItem[]>([])
  const [input, setInput] = useState("")
  const [systemPrompt, setSystemPrompt] = useState("")
  const [model, setModel] = useState("claude-sonnet-4-6")
  const [mode, setMode] = useState<"chat" | "agent">("chat")
  const [skill, setSkill] = useState<string>("")
  const [maxTurns, setMaxTurns] = useState(30)
  const [loading, setLoading] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Fetch available skills
  const { data: skillsData } = useQuery({
    queryKey: ["skills"],
    queryFn: () => axios.get<{ skills: SkillInfo[] }>("/api/agent/skills").then((r) => r.data.skills),
    enabled: mode === "agent",
  })

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [chatItems])

  // Chat mode (Messages API)
  const handleChatSend = async (text: string) => {
    const userItem: ChatItem = { role: "user", content: text }
    const newItems = [...chatItems, userItem]
    setChatItems(newItems)

    const apiMessages: PlaygroundMessage[] = newItems
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role as "user" | "assistant", content: m.content }))

    try {
      const resp = await playgroundChat(apiMessages, systemPrompt, model)
      setChatItems([...newItems, { role: "assistant", content: resp.data.text }])
    } catch (err: any) {
      const errMsg = err?.response?.data?.error || err.message || "请求失败"
      setChatItems([...newItems, { role: "assistant", content: `[错误] ${errMsg}` }])
    }
  }

  // Agent mode (Agent SDK with SSE streaming + session)
  const handleAgentSend = useCallback(async (text: string) => {
    const userItem: ChatItem = { role: "user", content: text }
    setChatItems((prev) => [...prev, userItem])

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const resp = await fetch("/api/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: text,
          system_prompt: systemPrompt,
          max_turns: maxTurns,
          skill: skill || undefined,
          session_id: sessionId || undefined,
        }),
        signal: controller.signal,
      })

      const reader = resp.body?.getReader()
      if (!reader) throw new Error("No reader")

      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split("\n")
        buffer = lines.pop() || ""

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue
          try {
            const event = JSON.parse(line.slice(6))

            if (event.type === "text") {
              setChatItems((prev) => {
                const last = prev[prev.length - 1]
                if (last?.role === "assistant" && !last.toolName) {
                  return [...prev.slice(0, -1), { ...last, content: last.content + event.content }]
                }
                return [...prev, { role: "assistant", content: event.content }]
              })
            } else if (event.type === "tool_use") {
              setChatItems((prev) => [
                ...prev,
                { role: "tool", content: "", toolName: event.tool, toolInput: event.input },
              ])
            } else if (event.type === "tool_result") {
              setChatItems((prev) => {
                const last = prev[prev.length - 1]
                if (last?.role === "tool") {
                  return [...prev.slice(0, -1), { ...last, content: event.content }]
                }
                return [...prev, { role: "tool", content: event.content }]
              })
            } else if (event.type === "result") {
              // Save session_id for continuity
              if (event.session_id) {
                setSessionId(event.session_id)
              }
              setChatItems((prev) => [
                ...prev,
                {
                  role: "tool",
                  content: `完成 | ${event.num_turns} 轮 | ${(event.duration_ms / 1000).toFixed(1)}s | $${event.cost_usd?.toFixed(4) || "?"}`,
                  toolName: `会话: ${event.session_id?.slice(0, 8) || "?"}...`,
                },
              ])
            } else if (event.type === "error") {
              setChatItems((prev) => [
                ...prev,
                { role: "assistant", content: `[错误] ${event.message}` },
              ])
            }
          } catch {}
        }
      }
    } catch (err: any) {
      if (err.name !== "AbortError") {
        setChatItems((prev) => [
          ...prev,
          { role: "assistant", content: `[错误] ${err.message}` },
        ])
      }
    }
  }, [systemPrompt, maxTurns, skill, sessionId])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || loading) return
    setInput("")
    setLoading(true)

    try {
      if (mode === "agent") {
        await handleAgentSend(text)
      } else {
        await handleChatSend(text)
      }
    } finally {
      setLoading(false)
      abortRef.current = null
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleStop = () => {
    abortRef.current?.abort()
    setLoading(false)
  }

  const handleNewConversation = () => {
    setChatItems([])
    setSessionId(null)
  }

  return (
    <div className="flex flex-col h-[calc(100vh-5rem)]">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-bold text-gray-900">模型测试</h2>
          <div className="flex bg-gray-100 rounded-lg p-0.5">
            <button
              onClick={() => setMode("chat")}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                mode === "chat" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"
              )}
            >
              <MessageSquare size={14} /> Chat
            </button>
            <button
              onClick={() => setMode("agent")}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                mode === "agent" ? "bg-white text-blue-700 shadow-sm" : "text-gray-500"
              )}
            >
              <Bot size={14} /> Agent
            </button>
          </div>
          {/* Session indicator */}
          {mode === "agent" && sessionId && (
            <span className="text-xs text-gray-400 bg-gray-100 px-2 py-1 rounded">
              会话: {sessionId.slice(0, 8)}...
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowSettings(!showSettings)}
            className={cn(
              "p-2 rounded-lg transition-colors",
              showSettings ? "bg-blue-50 text-blue-700" : "hover:bg-gray-100 text-gray-500"
            )}
          >
            <Settings size={18} />
          </button>
          <button
            onClick={handleNewConversation}
            className="flex items-center gap-1 px-3 py-1.5 text-sm text-gray-500 hover:bg-gray-100 rounded-lg"
            title="新建对话（清除会话上下文）"
          >
            <RotateCcw size={14} /> 新对话
          </button>
        </div>
      </div>

      {/* Settings panel */}
      {showSettings && (
        <div className="bg-white rounded-xl border border-gray-200 p-4 mb-4 space-y-3">
          {mode === "chat" && (
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">模型</label>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm"
              >
                <option value="claude-sonnet-4-6">Claude Sonnet 4.6</option>
                <option value="claude-opus-4-6">Claude Opus 4.6</option>
              </select>
            </div>
          )}
          {mode === "agent" && (
            <>
              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">Skill（可选）</label>
                <select
                  value={skill}
                  onChange={(e) => setSkill(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm"
                >
                  <option value="">通用 Agent（无指定 Skill）</option>
                  {skillsData?.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name} — {s.description}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">最大轮次</label>
                <input
                  type="number"
                  value={maxTurns}
                  onChange={(e) => setMaxTurns(Number(e.target.value))}
                  min={1}
                  max={100}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm"
                />
              </div>
            </>
          )}
          <div>
            <label className="text-xs font-medium text-gray-500 block mb-1">System Prompt</label>
            <textarea
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={mode === "agent" ? "可选：补充指令..." : "可选：设置系统提示词..."}
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm resize-none h-20"
            />
          </div>
        </div>
      )}

      {/* Chat area */}
      <div className="flex-1 bg-white rounded-xl border border-gray-200 overflow-auto p-4 space-y-3">
        {chatItems.length === 0 && (
          <div className="text-center text-gray-400 py-12">
            <p className="text-lg mb-2">
              {mode === "agent" ? "Agent 模式" : "开始与 Claude 对话"}
            </p>
            <p className="text-sm">
              {mode === "agent"
                ? skill
                  ? `当前 Skill: ${skill}`
                  : "可在设置中选择 Skill，或直接输入任务指令"
                : "在下方输入消息，测试模型能力"}
            </p>
          </div>
        )}
        {chatItems.map((item, i) => {
          if (item.role === "tool") {
            return (
              <div key={i} className="mx-4 p-2.5 bg-amber-50 border border-amber-200 rounded-lg text-xs">
                <div className="flex items-center gap-1.5 text-amber-700 font-medium mb-1">
                  <Wrench size={12} />
                  {item.toolName || "Tool"}
                </div>
                {item.toolInput && (
                  <pre className="text-gray-500 mb-1 whitespace-pre-wrap break-all">{item.toolInput}</pre>
                )}
                {item.content && (
                  <pre className="text-gray-700 whitespace-pre-wrap break-all">{item.content}</pre>
                )}
              </div>
            )
          }
          return (
            <div
              key={i}
              className={cn(
                "max-w-[80%] p-3 rounded-xl text-sm whitespace-pre-wrap",
                item.role === "user"
                  ? "ml-auto bg-blue-600 text-white"
                  : "mr-auto bg-gray-100 text-gray-800"
              )}
            >
              {item.content}
            </div>
          )
        })}
        {loading && (
          <div className="flex items-center gap-2 text-gray-400 text-sm">
            <Loader2 size={16} className="animate-spin" />
            {mode === "agent" ? `Agent${skill ? ` (${skill})` : ""} 执行中...` : "思考中..."}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="mt-3 flex gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            mode === "agent"
              ? skill
                ? `使用 ${skill} 处理... (Enter 发送)`
                : "输入任务指令... (Enter 发送)"
              : "输入消息... (Enter 发送, Shift+Enter 换行)"
          }
          className="flex-1 px-4 py-3 border border-gray-200 rounded-xl text-sm resize-none focus:outline-none focus:border-blue-300"
          rows={1}
        />
        {loading ? (
          <button
            onClick={handleStop}
            className="px-4 bg-red-500 text-white rounded-xl hover:bg-red-600 transition-colors"
          >
            停止
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={!input.trim()}
            className="px-4 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Send size={18} />
          </button>
        )}
      </div>
    </div>
  )
}
