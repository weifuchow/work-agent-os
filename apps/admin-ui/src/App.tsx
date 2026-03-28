import { BrowserRouter, Routes, Route } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import Layout from "./components/Layout"
import Dashboard from "./pages/Dashboard"
import Messages from "./pages/Messages"
import Conversations from "./pages/Conversations"
import Sessions from "./pages/Sessions"
import SessionDetail from "./pages/SessionDetail"
import Playground from "./pages/Playground"
import AuditLogs from "./pages/AuditLogs"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/messages" element={<Messages />} />
            <Route path="/conversations" element={<Conversations />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/sessions/:id" element={<SessionDetail />} />
            <Route path="/playground" element={<Playground />} />
            <Route path="/audit-logs" element={<AuditLogs />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
