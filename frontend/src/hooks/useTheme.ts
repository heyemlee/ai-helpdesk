import { useEffect, useState } from 'react'

const KEY = 'helpdesk.theme'
export type Theme = 'light' | 'dark'

/** 跟随系统，用户显式选择后写入 localStorage。深浅两套灰阶在设计系统里互为镜像。 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') return saved
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })

  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  const toggle = () => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark'
      localStorage.setItem(KEY, next)
      return next
    })
  }

  return { theme, toggle }
}
