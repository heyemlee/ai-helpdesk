/**
 * 测试用 render：自动套上 <LangProvider>。
 *
 * 组件里的 useT() 故意在缺少 Provider 时抛错——静默兜底会让「忘了包 Provider」
 * 变成运行时才发现的问题。所以这里覆写 RTL 的 render 而不是放宽 useT，
 * 各测试文件只需把 import 源换成这个模块。
 *
 * 默认语言固定为 zh：现有断言测的是**语义**（「未发起」而不是 0.00、
 * 「已转人工」而不是空），语义与语言无关，把它钉在中文比逐条改成英文更稳。
 * 默认英语与切换行为由 i18n 自己的测试覆盖。
 */
import type { ReactElement, ReactNode } from 'react'
import { render as rtlRender, type RenderOptions } from '@testing-library/react'
import { EmployeeContext } from '@/auth/session'
import { LangProvider, type Lang } from '@/i18n'

export * from '@testing-library/react'

export function render(
  ui: ReactElement,
  { lang = 'zh' as Lang, role = 'admin', wrapper, ...options }: RenderOptions & { lang?: Lang; role?: string } = {},
) {
  // 用 initial 而不是写 localStorage：jsdom 在没有 origin 时根本没有 localStorage
  const Outer = wrapper
  const Wrapper = ({ children }: { children: ReactNode }) =>
    Outer
      ? <LangProvider initial={lang}><Outer><EmployeeContext.Provider value={{ actor_id: 'test', instance: 'staff', auth_version: 1, roles: [role] }}>{children}</EmployeeContext.Provider></Outer></LangProvider>
      : <LangProvider initial={lang}><EmployeeContext.Provider value={{ actor_id: 'test', instance: 'staff', auth_version: 1, roles: [role] }}>{children}</EmployeeContext.Provider></LangProvider>
  return rtlRender(ui, { wrapper: Wrapper, ...options })
}
