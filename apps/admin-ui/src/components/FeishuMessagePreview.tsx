import type { MessageItem } from "../api/client"

type PreviewMessage = Pick<MessageItem, "message_type" | "content" | "raw_payload">

type CardText = {
  tag?: "plain_text" | "lark_md"
  content?: string
  text_size?: string
  text_color?: string
}

type CardElement = {
  tag?: string
  content?: string
  text?: CardText
  fields?: Array<{ text?: CardText }>
  columns?: Array<{ name?: string; display_name?: string; data_type?: string }>
  rows?: Array<Record<string, unknown>>
}

type CardPayload = {
  schema?: string
  header?: {
    title?: { content?: string }
    template?: string
  }
  body?: {
    elements?: CardElement[]
  }
}

type TransportPayload = {
  msg_type?: string
  content?: string
}

const headerTone: Record<string, string> = {
  blue: "from-sky-50 to-blue-100 text-blue-700 border-blue-100",
  green: "from-emerald-50 to-green-100 text-green-700 border-green-100",
  red: "from-rose-50 to-red-100 text-red-700 border-red-100",
  orange: "from-amber-50 to-orange-100 text-orange-700 border-orange-100",
  purple: "from-violet-50 to-fuchsia-100 text-violet-700 border-violet-100",
}

const textSizeClass: Record<string, string> = {
  heading: "text-base font-semibold",
  heading1: "text-lg font-semibold",
  "heading-1": "text-lg font-semibold",
  heading2: "text-base font-semibold",
  "heading-2": "text-base font-semibold",
  normal: "text-sm",
  notation: "text-xs",
}

const textColorClass: Record<string, string> = {
  default: "text-gray-800",
  blue: "text-blue-700",
  green: "text-emerald-700",
  red: "text-red-700",
  orange: "text-orange-700",
  grey: "text-gray-500",
}

function parseTransportPayload(rawPayload?: string | null): TransportPayload | null {
  if (!rawPayload) return null
  try {
    const parsed = JSON.parse(rawPayload) as TransportPayload
    return parsed && typeof parsed === "object" ? parsed : null
  } catch {
    return null
  }
}

function parseCardPayload(rawPayload?: string | null): CardPayload | null {
  const transport = parseTransportPayload(rawPayload)
  if (!transport || transport.msg_type !== "interactive" || typeof transport.content !== "string") {
    return null
  }
  try {
    const parsed = JSON.parse(transport.content) as CardPayload
    return parsed && typeof parsed === "object" ? parsed : null
  } catch {
    return null
  }
}

function extractImageKey(content: string): string | null {
  const match = content.match(/!\[[^\]]*]\(([^)]+)\)/)
  return match?.[1] ?? null
}

function renderText(text?: CardText, className = "") {
  if (!text?.content) return null
  const sizeClass = textSizeClass[text.text_size ?? "normal"] ?? "text-sm"
  const colorClass = textColorClass[text.text_color ?? "default"] ?? "text-gray-800"
  return (
    <div className={`${sizeClass} ${colorClass} whitespace-pre-wrap ${className}`.trim()}>
      {text.content}
    </div>
  )
}

function renderMarkdownBlock(content: string, keyPrefix: string) {
  const imageKey = extractImageKey(content)
  if (imageKey) {
    return (
      <div className="space-y-3" key={keyPrefix}>
        <div className="text-sm text-gray-800 whitespace-pre-wrap">
          {content.replace(/!\[[^\]]*]\(([^)]+)\)/g, "").trim()}
        </div>
        <img
          src={`/api/feishu-images/${encodeURIComponent(imageKey)}`}
          alt="feishu-card"
          className="w-full rounded-xl border border-gray-200 bg-white"
        />
      </div>
    )
  }

  return (
    <div key={keyPrefix} className="text-sm text-gray-800 whitespace-pre-wrap">
      {content}
    </div>
  )
}

function renderTable(element: CardElement) {
  const columns = element.columns ?? []
  const rows = element.rows ?? []
  if (!columns.length) return null

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            {columns.map((column, index) => (
              <th key={`${column.name ?? index}`} className="border-b border-gray-200 px-3 py-2 text-left font-medium text-gray-600">
                {column.display_name || column.name || `列${index + 1}`}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column, columnIndex) => {
                const cellValue = row[column.name ?? ""] ?? ""
                return (
                  <td key={`${rowIndex}-${column.name ?? columnIndex}`} className="px-3 py-2 align-top text-gray-800">
                    <div className="whitespace-pre-wrap break-words">{String(cellValue)}</div>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function renderElement(element: CardElement, index: number) {
  if (element.tag === "hr") {
    return <div key={index} className="border-t border-gray-200" />
  }

  if (element.tag === "table") {
    return <div key={index}>{renderTable(element)}</div>
  }

  if (element.tag === "markdown") {
    return renderMarkdownBlock(element.text?.content || element.content || "", `markdown-${index}`)
  }

  if (element.tag === "div") {
    const mainText = renderText(element.text)
    const fields = element.fields ?? []
    return (
      <div key={index} className="rounded-xl bg-white/80 px-1 py-0.5">
        {mainText}
        {fields.length > 0 && (
          <div className="mt-2 space-y-2">
            {fields.map((field, fieldIndex) => (
              <div key={`${index}-${fieldIndex}`} className="text-sm text-gray-600 whitespace-pre-wrap">
                {field.text?.content}
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  return null
}

export function FeishuMessagePreview({
  message,
  compact = false,
}: {
  message: PreviewMessage
  compact?: boolean
}) {
  const card = parseCardPayload(message.raw_payload)
  const transport = parseTransportPayload(message.raw_payload)

  if (!card) {
    if (compact) {
      return <div className="truncate text-gray-700">{message.content}</div>
    }
    return <div className="text-sm text-gray-800 whitespace-pre-wrap">{message.content}</div>
  }

  const title = card.header?.title?.content || message.content || "卡片消息"
  const headerClass = headerTone[card.header?.template ?? "blue"] ?? headerTone.blue
  const elements = card.body?.elements ?? []

  if (compact) {
    return (
      <div className="flex items-center gap-2 truncate">
        <span className="rounded-md bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700">卡片</span>
        <span className="truncate text-gray-700">{title}</span>
        {transport?.msg_type === "interactive" && <span className="text-xs text-gray-400">interactive</span>}
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-2xl border border-gray-200 bg-gray-50">
      <div className={`border-b bg-gradient-to-r px-4 py-3 ${headerClass}`}>
        <div className="text-lg font-semibold">{title}</div>
      </div>
      <div className="space-y-4 p-4">
        {elements.map((element, index) => renderElement(element, index))}
      </div>
    </div>
  )
}
