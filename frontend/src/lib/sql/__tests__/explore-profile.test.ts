/**
 * 表概况什么时候取、什么时候作废（改写自 test_console_logic.py 的
 * test_profiles_are_fetched_once_and_dropped_with_the_catalog；缓存从手写的表换成了 TanStack Query）：
 * - 服务不给概况（旧版本、管理员关了）时一条请求都不发；
 * - 同一张表问三次（其中一次只要行数）只发一个请求；换一组条件是另一份，同一组条件不重复取；
 * - 目录刷新之后（读取时刻变了）上一份概况作废；
 * - 同时最多 4 个请求在路上。
 */
import { createElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { Catalog } from '@/api/sql/types'
import { findProfile, knownRowCount, profileSupport, useTableProfile, useTableProfiles } from '../profile'

const catalogOf = (generated_at: string, service: Record<string, unknown> | null) =>
  ({ generated_at, domains: [], service }) as unknown as Catalog

const PROFILE = { total_rows: 7, columns: [], facets: [], timelines: [], skipped: [], sql: [] }

function setup(respond: (url: string) => Promise<Response> | Response = () => Response.json(PROFILE)) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? 'GET'} ${url} ${init?.body ?? ''}`)
    return respond(url)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children)
  return { client, wrapper, calls, profileCalls: () => calls.filter((call) => call.includes('/profile')).length }
}

afterEach(() => { vi.unstubAllGlobals() })

describe('表概况的取数与作废', () => {
  it('没有概况接口的旧版本进程、管理员关掉了：一条请求都不发', async () => {
    const { wrapper, profileCalls } = setup()
    const old = catalogOf('t1', { streaming: false })
    const off = catalogOf('t1', { profile: { enabled: false } })
    expect([profileSupport(old.service), profileSupport(off.service), profileSupport(null)]).toEqual(['unsupported', 'off', 'unknown'])
    const a = renderHook(() => useTableProfile(old, 'a_db', 't'), { wrapper })
    const b = renderHook(() => useTableProfile(off, 'a_db', 't'), { wrapper })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect([a.result.current.fetchStatus, b.result.current.fetchStatus]).toEqual(['idle', 'idle'])
    expect(profileCalls()).toBe(0)
  })

  it('同一张表问三次只发一个请求；换一组条件是另一份；目录刷新后上一份作废', async () => {
    const { client, wrapper, profileCalls } = setup()
    const catalog = catalogOf('t1', { profile: { enabled: true } })
    const { result } = renderHook(() => [
      useTableProfile(catalog, 'a_db', 't'),
      useTableProfile(catalog, 'a_db', 't'),
      useTableProfile(catalog, 'a_db', 't', { breakdowns: false }),    // 带分布的那份里就有行数
    ], { wrapper })
    await waitFor(() => expect(result.current.every((query) => query.isSuccess)).toBe(true))
    expect(profileCalls()).toBe(1)
    expect(knownRowCount(client, catalog, 'a_db', 't')).toBe(7)
    expect(findProfile(client, catalog, 'a_db', 'other')).toBeNull()

    const filters = [{ column: 'c', op: 'eq' as const, value: 'x' }]
    const filtered = renderHook(() => [
      useTableProfile(catalog, 'a_db', 't', { filters }),
      useTableProfile(catalog, 'a_db', 't', { filters }),
    ], { wrapper })
    await waitFor(() => expect(filtered.result.current.every((query) => query.isSuccess)).toBe(true))
    expect(profileCalls()).toBe(2)

    // 目录刷新（换了身份、点了刷新）：这些概况是按上一份目录算的
    const reloaded = catalogOf('t2', { profile: { enabled: true } })
    expect(knownRowCount(client, reloaded, 'a_db', 't')).toBeNull()
    expect(findProfile(client, reloaded, 'a_db', 't')).toBeNull()
  })

  it('一口气要几十张表的行数时排队取，同时最多 4 个在路上', async () => {
    let inFlight = 0
    let peak = 0
    const { wrapper, profileCalls } = setup(async () => {
      inFlight += 1
      peak = Math.max(peak, inFlight)
      await new Promise((resolve) => setTimeout(resolve, 5))
      inFlight -= 1
      return Response.json(PROFILE)
    })
    const catalog = catalogOf('t1', { profile: { enabled: true } })
    const pairs = Array.from({ length: 12 }, (_, index) => ({ database: 'a_db', table: `t${index}` }))
    const { result } = renderHook(() => useTableProfiles(catalog, pairs, { breakdowns: false }), { wrapper })
    await waitFor(() => expect(result.current.every((query) => query.isSuccess)).toBe(true))
    expect(profileCalls()).toBe(12)
    expect(peak).toBeLessThanOrEqual(4)
  })
})
