/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  /** SQL Agent 接口的前缀，默认 /sql-api（生产由控制台的 nginx 转发、本地由 vite 转发） */
  readonly VITE_SQL_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
