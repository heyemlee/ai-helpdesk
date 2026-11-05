/**
 * 一轮问答的结论、复制出去的纯文本、念给读屏的那句话（移植自旧控制台 result.js 的
 * conclusion / summaryText / announcement / tableText）。
 *
 * 页面不显示服务端 answer 字段里那串"已完成数据库查询。查询结果：[JSON]"——那是给下游程序留的文本。
 * 结论由结构化字段（行数、列、单位）直接拼出来：能拼出什么就说什么，拼不出来就只报行数，不编一句话。
 */
import type { Catalog } from '@/api/sql/types'
import type { Lang } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { sourceLabel } from './catalog'
import { formatCell, toTsv } from './format'
import { classify, splitSteps, type Classification, type QueryResult, type ResultStep } from './model'
import { stepColumns, stepMatrix } from './steps'

type T = (key: Key, vars?: Record<string, string | number>) => string

export interface SummaryContext {
  t: T
  lang: Lang
  catalog: Catalog | null | undefined
}

export const MAX_FACTS = 8
const MAX_NAMES = 5

export interface Fact {
  label: string
  value: string
  unit: string
}

export interface Conclusion {
  headline: string
  facts: Fact[]
  names: string
}

/** 结论：一句话头、一行结果时的几个数字、多行时点出的几个名字。全部来自这一轮返回的行。 */
export function conclusion(result: QueryResult, view: Classification, answers: ResultStep[], ctx: SummaryContext): Conclusion {
  const { t, lang, catalog } = ctx
  if (view.kind === 'no_data') return { headline: t('sq.ask.result.noData'), facts: [], names: '' }
  if (view.kind === 'failed') return { headline: t('sq.ask.result.failed'), facts: [], names: '' }

  const rowTotal = answers.reduce((sum, step) => sum + step.rowCount, 0)
  const facts: Fact[] = []
  if (answers.length && answers.every((step) => step.rowCount === 1 && step.rows.length >= 1)) {
    for (const step of answers) {
      for (const column of stepColumns(step, catalog, lang)) {
        const cell = formatCell(step.rows[0]![column.key], lang, { unit: column.unit, plain: column.plain })
        facts.push({ label: column.label, value: cell.text, unit: cell.empty ? '' : column.unit })
      }
    }
  }

  let names = ''
  const last = answers[answers.length - 1]
  if (last && answers.length === 1 && last.rowCount > 1) {
    const columns = stepColumns(last, catalog, lang)
    const naming = columns.find((column) => column.entry?.role === 'output_dimension')
      ?? columns.find((column) => /(^|_)name$/i.test(column.key))
    if (naming) {
      const values = [...new Set(last.rows.map((row) => row[naming.key]).filter((value) => value !== null && value !== undefined))]
      // 名字本身带逗号时（"3/4 in panel, 4 x 8 ft"）换成分号分隔，否则数不清有几个
      const separator = values.some((value) => String(value).includes(',')) ? '; ' : ', '
      names = values.slice(0, MAX_NAMES).map(String).join(separator)
      if (values.length > MAX_NAMES) names += t('sq.ask.result.andMore', { n: values.length - MAX_NAMES })
    }
  }

  let headline: string
  if (view.kind === 'partial') {
    headline = t('sq.ask.result.partial', { ok: result.steps.length - view.failedSteps.length, total: result.steps.length })
  } else if (answers.length > 1) {
    headline = t('sq.ask.result.parts', { n: answers.length, rows: rowTotal })
  } else if (view.truncated) {
    headline = t('sq.ask.result.recordsTruncated', { n: rowTotal })
  } else {
    headline = rowTotal === 1 ? t('sq.ask.result.oneRecord') : t('sq.ask.result.records', { n: rowTotal })
  }
  return { headline, facts: facts.length <= MAX_FACTS ? facts : [], names }
}

/**
 * 复制用的纯文本结论。复制出去的文字会离开这个页面单独流传，页面上的提示必须跟着走：
 * 少一句"结果被截断"或"来自降级链路"，贴到邮件里就成了一个不带条件的结论。
 */
export function summaryText(question: string, result: QueryResult, ctx: SummaryContext): string {
  const { t, lang, catalog } = ctx
  const view = classify(result)
  const caveats: string[] = []
  if (view.degraded) caveats.push(t('sq.ask.notice.degraded.title'))
  if (view.kind === 'conversation') {
    return [question, '', result.answer, '', t('sq.ask.notice.conversation.title'), ...caveats].join('\n')
  }
  const { answers } = splitSteps(result, question)
  const summary = conclusion(result, view, answers, ctx)
  const lines = [question, '', summary.headline + (summary.names ? `: ${summary.names}` : '')]
  for (const fact of summary.facts) lines.push(`${fact.label}: ${fact.value}${fact.unit ? ` ${fact.unit}` : ''}`)
  const sources = [...new Set(answers.map((step) => sourceLabel(catalog, step.database, lang)))]
  if (sources.length) lines.push('', `${t('sq.ask.meta.source')}: ${sources.join(', ')}`)
  if (result.asOf) lines.push(`${t('sq.ask.meta.readAtPlain')}: ${result.asOf}`)
  if (view.kind === 'partial') caveats.unshift(t('sq.ask.notice.partial.title'))
  if (view.truncated) caveats.unshift(t('sq.ask.notice.truncated.title'))
  if (view.needsReview) caveats.unshift(t('sq.ask.notice.review.title'))
  if (caveats.length) lines.push('', ...caveats)
  return lines.join('\n')
}

/** 这一轮有了结果时念给读屏的那句话：状态加结论，页面上最先该知道的那两样 */
export function announcement(status: string, question: string, result: QueryResult | null, ctx: SummaryContext): string {
  const { t } = ctx
  if (status === 'stopped') return t('sq.ask.notice.stopped.title')
  if (status === 'error' || !result) return t('sq.ask.notice.service.title')
  const view = classify(result)
  if (view.kind === 'conversation') return t('sq.ask.status.conversation')
  const { answers } = splitSteps(result, question)
  const summary = conclusion(result, view, answers, ctx)
  const parts = [summary.headline + (summary.names ? `: ${summary.names}` : '')]
  if (view.needsReview) parts.push(t('sq.ask.status.review'))
  if (view.truncated) parts.push(t('sq.ask.status.truncated'))
  if (view.degraded) parts.push(t('sq.ask.status.degraded'))
  return parts.join('. ')
}

/** 一步结果复制成制表符分隔（贴进表格软件就是一张表）；列头带单位，值是原始值 */
export function tableText(step: ResultStep, ctx: SummaryContext): string {
  const columns = stepColumns(step, ctx.catalog, ctx.lang)
  return toTsv(
    columns.map((column) => (column.unit ? `${column.label} (${column.unit})` : column.label)),
    stepMatrix(step, columns),
  )
}
