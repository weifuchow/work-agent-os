import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import {
  fetchMemoryFiles,
  fetchMemoryFile,
  updateMemoryFile,
  deleteMemoryFile,
  MemoryFile,
} from "../api/client"
import { FolderOpen, FileText, Save, Trash2, Brain } from "lucide-react"
import { cn } from "../lib/utils"

export default function Memory() {
  const queryClient = useQueryClient()
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [editContent, setEditContent] = useState("")
  const [dirty, setDirty] = useState(false)

  const { data: filesData, isLoading } = useQuery({
    queryKey: ["memory-files"],
    queryFn: () => fetchMemoryFiles().then((r) => r.data),
  })

  const { data: fileData, isLoading: isLoadingFile } = useQuery({
    queryKey: ["memory-file", selectedFile],
    queryFn: () => fetchMemoryFile(selectedFile!).then((r) => r.data),
    enabled: !!selectedFile,
  })

  const saveMutation = useMutation({
    mutationFn: () => updateMemoryFile(selectedFile!, editContent),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-files"] })
      queryClient.invalidateQueries({ queryKey: ["memory-file", selectedFile] })
      setDirty(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => deleteMemoryFile(selectedFile!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-files"] })
      setSelectedFile(null)
      setEditContent("")
      setDirty(false)
    },
  })

  // When file data loads, update editor
  const currentContent = fileData?.content ?? ""
  if (fileData && editContent !== currentContent && !dirty) {
    setEditContent(currentContent)
  }

  // Group files by category
  const files = filesData?.files || []
  const grouped: Record<string, MemoryFile[]> = {}
  for (const f of files) {
    const cat = f.category
    if (!grouped[cat]) grouped[cat] = []
    grouped[cat].push(f)
  }

  const categoryLabels: Record<string, string> = {
    projects: "项目知识",
    people: "人员信息",
    general: "通用记忆",
  }

  if (isLoading) return <div className="text-gray-500">加载中...</div>

  return (
    <div className="flex gap-4 h-[calc(100vh-7rem)]">
      {/* File Tree */}
      <div className="w-64 bg-white rounded-xl border border-gray-200 overflow-auto flex-shrink-0">
        <div className="p-3 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <Brain size={16} className="text-gray-600" />
            <span className="font-medium text-sm text-gray-900">记忆文件</span>
          </div>
          <span className="text-xs text-gray-400">{files.length} 个文件</span>
        </div>

        <div className="p-2">
          {Object.entries(grouped).map(([category, catFiles]) => (
            <div key={category} className="mb-3">
              <div className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-gray-500 uppercase">
                <FolderOpen size={12} />
                {categoryLabels[category] || category}
              </div>
              {catFiles.map((f) => (
                <button
                  key={f.path}
                  onClick={() => {
                    if (dirty && selectedFile !== f.path) {
                      if (!confirm("有未保存的修改，确定切换？")) return
                    }
                    setSelectedFile(f.path)
                    setDirty(false)
                  }}
                  className={cn(
                    "w-full flex items-center gap-2 px-3 py-1.5 rounded-md text-left text-sm transition-colors",
                    selectedFile === f.path
                      ? "bg-blue-50 text-blue-700"
                      : "text-gray-700 hover:bg-gray-50"
                  )}
                >
                  <FileText size={14} />
                  <span className="truncate">{f.name}</span>
                </button>
              ))}
            </div>
          ))}

          {files.length === 0 && (
            <div className="text-center text-gray-400 py-4 text-sm">暂无记忆文件</div>
          )}
        </div>
      </div>

      {/* Editor */}
      <div className="flex-1 bg-white rounded-xl border border-gray-200 flex flex-col overflow-hidden">
        {selectedFile ? (
          <>
            <div className="flex items-center justify-between p-3 border-b border-gray-100">
              <div>
                <span className="text-sm font-medium text-gray-900">{selectedFile}</span>
                {dirty && <span className="ml-2 text-xs text-orange-500">未保存</span>}
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => saveMutation.mutate()}
                  disabled={!dirty || saveMutation.isPending}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-40 transition-colors"
                >
                  <Save size={14} />
                  {saveMutation.isPending ? "保存中..." : "保存"}
                </button>
                <button
                  onClick={() => {
                    if (confirm(`确定删除 ${selectedFile}？`)) {
                      deleteMutation.mutate()
                    }
                  }}
                  disabled={deleteMutation.isPending}
                  className="flex items-center gap-1 px-3 py-1.5 text-sm text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-40 transition-colors"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
            <textarea
              value={editContent}
              onChange={(e) => {
                setEditContent(e.target.value)
                setDirty(true)
              }}
              className="flex-1 p-4 font-mono text-sm text-gray-800 resize-none outline-none"
              placeholder={isLoadingFile ? "加载中..." : "文件内容为空"}
            />
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-gray-400 text-sm">
            选择左侧文件进行查看和编辑
          </div>
        )}
      </div>
    </div>
  )
}
