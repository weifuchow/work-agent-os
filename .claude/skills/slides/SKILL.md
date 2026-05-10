---
name: slides
description: Use when creating, editing, or summarizing PowerPoint slide decks and .pptx files, especially when a Feishu reply should include a generated PPT attachment alongside a concise card summary.
---

# Slides Skill

## Workflow

1. Use `python-pptx` for new `.pptx` files and straightforward edits.
2. Use the exact target path and filename provided by the orchestrator when present; otherwise write final files under the current session workspace output directory, usually `workspace/output/`.
3. Keep the Feishu card concise: show the deck purpose, slide outline, key points, and file list.
4. Put the full slide deck in the `.pptx` attachment instead of expanding slide content in the card.
5. For revisions, overwrite or create the latest requested deck and declare only the latest version unless the user asks for historical versions.
6. Return the file through the reply attachment contract:

```json
{
  "reply": {
    "type": "feishu_card",
    "content": "卡片摘要",
    "attachments": [
      {
        "path": "output/example.pptx",
        "title": "PPT 演示文稿",
        "description": "完整页面见附件"
      }
    ]
  }
}
```

## Quality Checks

- Re-open the generated file with `python-pptx` before delivery.
- Verify slide count, titles, body text, notes, and image references.
- Use the orchestrator-provided filename when present; do not invent a different filename for a declared target artifact.
- Use a simple, consistent layout unless the user provides a design template.
- Do not call a project agent just to create a generic deck; use project context only when the user asks for project-specific design, code, or analysis.
