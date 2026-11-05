/**
 * 一次 run 的事件日志 → "一次查询的经过"页上的时间线。
 *
 * 事件是平的一串（seq 递增）：stage.started / stage.finished 成对出现，中间夹着这个阶段里的
 * model.call（一次模型调用）和 tool.sql（一次 SQL 执行）。按阶段折成一行一行：每行一个阶段
 * （按 step 重复的阶段每步一行），下面挂它里面发生的调用。
 *
 * 只认事件自己说的：stage 和 step 都来自事件本身，不按顺序猜归属。对不上任何一行的事件
 * （服务端以后加了新类型、日志被截断）单独成行，不丢。
 */
import type { RunEvent } from '@/api/sql/types'

export interface TimelineRow {
  /** React 的 key：阶段 + 步骤 + 第几次出现 */
  key: string
  stage: string
  step: number | null
  started: RunEvent | null
  finished: RunEvent | null
  /** 这一行里的 model.call、tool.sql 和别的事件，按 seq */
  events: RunEvent[]
}

export interface RunTimeline {
  rows: TimelineRow[]
  started: RunEvent | null
  finished: RunEvent | null
  cancelRequested: RunEvent | null
}

const sameSlot = (row: TimelineRow, event: RunEvent) => row.stage === event.stage && row.step === (event.step ?? null)

export function buildTimeline(events: RunEvent[]): RunTimeline {
  const sorted = [...events].sort((a, b) => a.seq - b.seq)
  const rows: TimelineRow[] = []
  const open: TimelineRow[] = []
  const counts = new Map<string, number>()
  let started: RunEvent | null = null
  let finished: RunEvent | null = null
  let cancelRequested: RunEvent | null = null

  const newRow = (event: RunEvent): TimelineRow => {
    const slot = `${event.stage}#${event.step ?? ''}`
    const n = (counts.get(slot) ?? 0) + 1
    counts.set(slot, n)
    const row: TimelineRow = { key: `${slot}#${n}`, stage: event.stage, step: event.step ?? null,
      started: null, finished: null, events: [] }
    rows.push(row)
    return row
  }

  for (const event of sorted) {
    if (event.type === 'run.started') { started = event; continue }
    if (event.type === 'run.finished') { finished = event; continue }
    if (event.type === 'run.cancel_requested') { cancelRequested = event; continue }
    if (event.type === 'stage.started') {
      const row = newRow(event)
      row.started = event
      open.push(row)
      continue
    }
    // 最近打开的同一个阶段同一步：嵌套的阶段（比如执行里的修复）各归各的
    const index = open.map((row) => sameSlot(row, event)).lastIndexOf(true)
    if (event.type === 'stage.finished') {
      if (index === -1) {
        newRow(event).finished = event
      } else {
        open[index]!.finished = event
        open.splice(index, 1)
      }
      continue
    }
    const row = index !== -1 ? open[index]! : newRow(event)
    row.events.push(event)
  }
  return { rows, started, finished, cancelRequested }
}

/** 一个数字字段；不是数字就是 null（attrs 是服务端的自由结构，不信它的类型） */
export const numberAttr = (attrs: Record<string, unknown>, key: string): number | null =>
  (typeof attrs[key] === 'number' && Number.isFinite(attrs[key]) ? (attrs[key] as number) : null)

export const stringAttr = (attrs: Record<string, unknown>, key: string): string =>
  (typeof attrs[key] === 'string' ? (attrs[key] as string) : '')

/** 这一行的耗时：stage.finished 里服务端量的，没有就是 null */
export const rowDuration = (row: TimelineRow): number | null =>
  (row.finished ? numberAttr(row.finished.attrs, 'duration_ms') : null)

/**
 * 这一行的结局：ok / error / cancelled。没有 stage.finished 的行：run 还在跑、这一行又开了头，是 running；
 * 别的情况（run 已经结束，或者是不属于任何阶段的零散事件）说不出结局，返回空串，页面上不画状态。
 * 以前一律写 running，结束了的 run 里不在阶段内的那次模型调用会一直挂着"进行中"。
 */
export const rowStatus = (row: TimelineRow, live = false): string => {
  if (row.finished) return stringAttr(row.finished.attrs, 'status') || 'ok'
  return live && row.started ? 'running' : ''
}

/** run 的总耗时（毫秒）：结束时间减开始时间，任一缺失就是 null */
export function runDuration(createdAt?: string | null, finishedAt?: string | null): number | null {
  const start = Date.parse(createdAt ?? '')
  const end = Date.parse(finishedAt ?? '')
  return Number.isNaN(start) || Number.isNaN(end) ? null : Math.max(0, end - start)
}
