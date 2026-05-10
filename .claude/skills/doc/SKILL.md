---
name: doc
description: Use when creating, editing, or summarizing Word documents and .docx files, especially when a Feishu reply should include a generated Word attachment alongside a concise card summary.
---

# DOCX Skill

## Workflow

1. Use `python-docx` for new `.docx` files and straightforward edits.
2. Use the exact target path and filename provided by the orchestrator when present; otherwise write final files under the current session workspace output directory, usually `workspace/output/`.
3. Keep the Feishu card concise: show the conclusion, outline, important context, and file list.
4. Put the full document content in the `.docx` attachment instead of expanding long text in the card.
5. For revisions, overwrite or create the latest requested file and declare only the latest version unless the user asks for historical versions.
6. Return the file through the reply attachment contract:

```json
{
  "reply": {
    "type": "feishu_card",
    "content": "卡片摘要",
    "attachments": [
      {
        "path": "output/example.docx",
        "title": "Word 文档",
        "description": "完整内容见附件"
      }
    ]
  }
}
```

## Quality Checks

- Open or inspect the generated file with `python-docx` before delivery.
- Verify headings, paragraphs, tables, and lists exist as intended.
- Use the orchestrator-provided filename when present; do not invent a different filename for a declared target artifact.
- Do not call a project agent just to create a generic document; use project context only when the user asks for project-specific design, code, or analysis.
