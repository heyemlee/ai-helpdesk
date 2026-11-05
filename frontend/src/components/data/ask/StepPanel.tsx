/**
 * 一步结果的面板：来自哪个库、多少行，三种看法（移植自旧控制台 result.js 的 stepPanel 一组）。
 *
 * 三种看法是同一批行的三种画法，互相切换不丢东西：只有一行时是几个大数字；一列名字加一列数字时是
 * 一张图；任何时候都能看表格。来自数据库的文字一律按文本渲染（React 默认转义）。
 */
import { useMemo, useState, type CSSProperties } from 'react'
import type { Catalog } from '@/api/sql/types'
import { useT } from '@/i18n'
import { domainTitleOf, sourceLabel } from '@/lib/sql/catalog'
import { descriptionOf, formatCell } from '@/lib/sql/format'
import { chartFor, stepViews, type Chart, type StepView } from '@/lib/sql/insight'
import { MAX_FACTS, tableText } from '@/lib/sql/ask-summary'
import type { ResultStep } from '@/lib/sql/model'
import { stepColumns, stepMatrix, type StepColumn } from '@/lib/sql/steps'
import { sortRows } from '@/lib/sql/table'
import { DataTable, Pager, type TableSort } from '../DataTable'
import { DbTile, databaseSlot } from '../DbTile'
import { CopyButton } from '../shared'

const PAGE_SIZE = 25

/** 一段文字大概占几个字符宽：中日韩文字算两个。只用来给图里的名字列留宽度 */
const textWidth = (text: string) =>
  [...text].reduce((sum, char) => sum + (/[⺀-鿿가-힯＀-￯]/.test(char) ? 2 : 1), 0)

export function StepPanel({ step, multiple, catalog }: {
  step: ResultStep
  /** 这一轮有不止一块面板：标题前写上"步骤 N" */
  multiple: boolean
  catalog: Catalog | null | undefined
}) {
  const { t, lang } = useT()
  const columns = useMemo(() => stepColumns(step, catalog, lang), [step, catalog, lang])
  const views = stepViews(step, columns, MAX_FACTS)
  const [chosen, setChosen] = useState<StepView | ''>('')
  // 记下来的选择对不上任何一种看法时（目录刷新后列的角色变了），退回默认的那种
  const view = chosen && views.includes(chosen) ? chosen : views[0]!
  const chart = chartFor(step, columns)
  const title = domainTitleOf(catalog, step.database, lang)
  const label = `${sourceLabel(catalog, step.database, lang)} — ${t('sq.ask.result.rows', { n: step.rowCount })}`

  return (
    <section className={`sq-result-panel db-c${databaseSlot(catalog, step.database)}`} aria-label={label}>
      <div className="sq-result-panel-head">
        <span className="sq-result-panel-title">
          <DbTile catalog={catalog} database={step.database} size="is-sm" />
          <span>{multiple ? `${t('sq.ask.process.step', { n: step.index + 1 })} · ` : ''}{title}</span>
          {title !== step.database && <span className="sq-tech">{step.database}</span>}
        </span>
        <span className="sq-result-panel-count">{t('sq.ask.result.rows', { n: step.rowCount })}</span>
        <span className="sq-result-panel-tools">
          {views.length > 1 && (
            <span className="sq-seg" role="group" aria-label={t('sq.ask.view.switch')}>
              {views.map((name) => (
                <button key={name} type="button" aria-pressed={name === view} onClick={() => setChosen(name)}>
                  {t(`sq.ask.view.${name}`)}
                </button>
              ))}
            </span>
          )}
          <CopyButton text={tableText(step, { t, lang, catalog })} label={t('sq.ask.action.copyTable')} />
        </span>
      </div>
      {view === 'figures' && <Figures step={step} columns={columns} />}
      {view === 'chart' && chart && <ChartView chart={chart} />}
      {view === 'table' && (
        <>
          {chart?.kind === 'columns' && <ChartView chart={chart} />}
          <TableView step={step} columns={columns} label={label} />
        </>
      )}
      <Legend columns={columns} />
    </section>
  )
}

/** 只有一行：每一列一个大数字。文字列（名字、状态）字号小一档，免得一长串字撑满一行 */
function Figures({ step, columns }: { step: ResultStep; columns: StepColumn[] }) {
  const { lang } = useT()
  const row = step.rows[0] ?? {}
  return (
    <dl className="sq-stat-tiles">
      {columns.map((column) => {
        const cell = formatCell(row[column.key], lang, { unit: column.unit, plain: column.plain })
        return (
          <div key={column.key} className="sq-stat-tile">
            <dt className="sq-stat-label" title={column.hint || undefined}>{column.label}</dt>
            <dd className={`sq-stat-value${cell.numeric ? '' : ' is-text'}${cell.empty ? ' is-empty' : ''}`}>
              {cell.text}
              {column.unit && !cell.empty && <span className="sq-stat-unit">{column.unit}</span>}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

/**
 * 一列名字加一列数字：横条，或者（连着的年月）一排柱子。画的就是返回的那几行，条的颜色跟着来源库的
 * 标识色走。横条把每一行的名字和数字都写在条的旁边，单独看不缺东西。柱子上写不下数字（悬停提示在
 * 手机上、用键盘时都看不到），所以柱子只放在表格上面，不单独当一种看法。
 */
function ChartView({ chart }: { chart: Chart<StepColumn> }) {
  const { t, lang } = useT()
  const peak = Math.max(...chart.items.map((item) => item.value))
  const show = (amount: number) => formatCell(amount, lang, { unit: chart.value.unit }).text
  const title = t('sq.ask.chart.caption', { value: chart.value.label, label: chart.label.label })
    + (chart.value.unit ? ` (${chart.value.unit})` : '')

  if (chart.kind === 'columns') {
    // 最高的可能不止一个：并列的都写出来，太多就不写这句（下面的表格里都有）
    const tops = chart.items.filter((item) => item.value === peak)
    const first = chart.items[0]!
    const last = chart.items[chart.items.length - 1]!
    return (
      <div className="sq-answer-chart is-above-table">
        <p className="sq-chart-caption">{title}</p>
        <div className="sq-cols" role="img" aria-label={`${title}: ${first.text} – ${last.text}`}>
          {chart.items.map((item) => (
            <span key={item.text} className="sq-col" title={`${item.text} · ${show(item.value)}`}>
              <i style={{ height: `${item.value ? Math.max(3, (item.value / peak) * 100) : 0}%` }} />
            </span>
          ))}
        </div>
        <div className="sq-axis" aria-hidden="true"><span>{first.text}</span><span>{last.text}</span></div>
        {tops.length <= 3 && (
          <p className="sq-chart-note">
            {t('sq.ask.chart.peak', { label: tops.map((item) => item.text).join(', '), value: show(peak) })}
          </p>
        )}
      </div>
    )
  }

  // 名字、数字这两列各行一样宽，条才对得齐：宽度按最长的那个名字、那个数字算
  const nameWidth = Math.min(30, Math.max(6, ...chart.items.map((item) => textWidth(item.text))))
  const valueWidth = Math.max(...chart.items.map((item) => show(item.value).length))
  return (
    <div className="sq-answer-chart">
      <p className="sq-chart-caption">{title}</p>
      <ul className="sq-bars" style={{ '--name-ch': nameWidth, '--value-ch': valueWidth } as CSSProperties}>
        {chart.items.map((item) => (
          <li key={item.text} className="sq-bar-row">
            <span className="sq-bar-name" title={item.text}>{item.text}</span>
            <span className="sq-bar-track" aria-hidden="true">
              <i style={{ width: `${item.value ? Math.max(1.5, (item.value / peak) * 100) : 0}%` }} />
            </span>
            <span className="sq-bar-value">{show(item.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** 表格：服务端返回的原始顺序；点表头升序，再点降序，第三下回到原始顺序。每页 25 行 */
function TableView({ step, columns, label }: { step: ResultStep; columns: StepColumn[]; label: string }) {
  const { t } = useT()
  const [sort, setSort] = useState<TableSort | null>(null)
  const [page, setPage] = useState(1)

  let rows = stepMatrix(step, columns)
  if (sort) {
    const index = columns.findIndex((column) => column.key === sort.key)
    if (index >= 0) rows = sortRows(rows, index, sort.direction)
  }
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const current = Math.min(Math.max(1, page), pageCount)
  const from = (current - 1) * PAGE_SIZE
  const shown = rows.slice(from, from + PAGE_SIZE)

  const onSort = (key: string) => {
    setSort((was) => (!was || was.key !== key ? { key, direction: 'asc' }
      : was.direction === 'asc' ? { key, direction: 'desc' } : null))
    setPage(1)
  }

  return (
    <>
      <DataTable label={label} rows={shown} sort={sort} onSort={onSort}
        columns={columns.map((column) => ({
          key: column.key, label: column.label, unit: column.unit, numeric: column.numeric,
          plain: column.plain, hint: column.hint,
        }))} />
      {(rows.length > PAGE_SIZE || step.truncated) && (
        <div className="sq-table-foot">
          <span>
            {rows.length > PAGE_SIZE
              ? t('sq.ask.table.range', { from: from + 1, to: from + shown.length, total: rows.length })
              : t('sq.ask.result.rows', { n: rows.length })}
            {step.truncated ? ` · ${t('sq.ask.table.truncatedRows')}` : ''}
            {sort && step.truncated ? ` · ${t('sq.ask.table.sortedPartial')}` : ''}
          </span>
          <Pager page={current} pageCount={pageCount} onPage={setPage} />
        </div>
      )}
    </>
  )
}

/**
 * 这些列是什么意思（数据字典里的说明）。哪种看法下都在面板最下面：看的是大数字或图的时候，
 * 同样需要知道那个数是怎么定义的。数据字典里一列都对不上时没有说明可给，不画。
 */
function Legend({ columns }: { columns: StepColumn[] }) {
  const { t, lang } = useT()
  if (!columns.some((column) => column.entry)) return null
  return (
    <details className="raw sq-legend">
      <summary>{t('sq.ask.result.legend')}</summary>
      <dl className="sq-legend-list">
        {columns.map((column) => {
          const description = column.entry ? descriptionOf(column.entry, lang) : null
          return (
            <div key={column.key} className="sq-legend-row">
              <dt>{column.label}{column.unit ? ` (${column.unit})` : ''}<span className="sq-tech">{column.key}</span></dt>
              <dd>
                {description?.text
                  ? <span lang={description.original ? 'zh' : undefined}>{description.text}</span>
                  : <span className="field-hint">
                    {column.entry ? t('sq.ask.result.noDescription') : t('sq.ask.result.computedColumn')}
                  </span>}
              </dd>
            </div>
          )
        })}
      </dl>
      <p className="field-hint">{t('sq.ask.result.legendNote')}</p>
    </details>
  )
}
