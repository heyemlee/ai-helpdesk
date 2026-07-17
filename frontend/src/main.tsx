import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 控制台面向调试场景，默认不做激进的后台重取，
      // 需要实时性的地方（Monitor）在各自的 hook 里显式开轮询。
      refetchOnWindowFocus: false,
      staleTime: 3_000,
      retry: false,
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('找不到 #root 挂载点')

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
