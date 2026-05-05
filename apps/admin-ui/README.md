# Work Agent OS 管理后台

`work-agent-os` 的 React/Vite 管理控制台。

## 技术栈

- React 19
- TypeScript
- Vite
- React Router
- TanStack Query
- Tailwind CSS 4
- lucide-react
- axios

## 页面

页面源码位于 `src/pages/`：

- `Dashboard.tsx`：系统概览和统计。
- `Messages.tsx`：原始飞书消息。
- `Conversations.tsx`：对话视图。
- `Sessions.tsx`：工作会话列表。
- `SessionDetail.tsx`：会话详情、消息和 artifacts。
- `AuditLogs.tsx`：审计日志。
- `Memory.tsx`：结构化记忆。
- `Playground.tsx`：模型测试对话。
- `Triage.tsx`：triage/review 产物浏览。

公共 UI 和 API helper：

- `src/api/client.ts`
- `src/components/Layout.tsx`
- `src/components/ModelSwitcher.tsx`
- `src/components/AgentRuntimeSwitcher.tsx`
- `src/components/FeishuMessagePreview.tsx`

## 本地开发

先启动后端：

```bash
python -m uvicorn apps.api.main:app --port 8000
```

再启动前端：

```bash
cd apps/admin-ui
npm install
npm run dev
```

dev server 的 API 代理配置在 Vite 配置中维护。

## 构建和检查

```bash
npm run build
npm run lint
npm run preview
```

## 后端 API

前端调用 `/api` 下的接口，主要实现位于 `apps/api/routers/admin.py`。当前主要模块：

- messages
- conversations
- sessions / task contexts
- audit logs
- agent runs / inflight status
- model/runtime switching
- memory entries
- triage/review artifacts
- project insights
- playground chat

页面行为应和后端实际 response shape 对齐，不要沿用 Vite 模板说明。
