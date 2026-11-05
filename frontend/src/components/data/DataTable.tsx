/**
 * 表格：问数结果与数据浏览共用一套画法（移植自旧控制台的 table.js）。
 *
 * 列头第一行是给人看的名字（带单位），第二行是技术字段名——员工看前者，学习和排查时对后者；
 * 两者都在，不用切换模式。单元格一律按文本渲染（React 默认转义），服务端给的值不当 HTML。
 */
import type { KeyboardEvent } from 'react'
import { useT } from '@/i18n'
import { formatCell } from '@/lib/sql/format'

export interface TableColumn {
  key: string
  label: string
  unit?: string
  numeric?: boolean
  /** 编号、主键这类：数字不加千分位 */
  plain?: boolean
  hint?: string
  /** 这一列在行数组里的位置。收起了几列之后，列的顺序和行里的位置就对不上了 */
  index?: number
  /** 取值 → 第几个分类色（1 起）。有的话这一列画成带色点的小标签 */
  slots?: Map<string, number>
  /** 长内容截成一行（完整内容在悬停提示里），不撑高整行 */
  clip?: boolean
  sortable?: boolean
}

export interface TableSort {
  key: string
  direction: 'asc' | 'desc'
}

/** 一个取值前面带色点的小标签。颜色只是帮眼睛扫，认的还是字 */
export function ValuePill({ text, slot }: { text: string; slot: number }) {
  return <span className="sq-pill"><span className={`viz-dot viz-s${slot}`} aria-hidden="true" />{text}</span>
}

export function DataTable({ columns, rows, label, sort = null, onSort, onRowClick, selected = -1 }: {
  columns: TableColumn[]
  rows: unknown[][]
  /** 表格区域的无障碍名字 */
  label: string
  sort?: TableSort | null
  /** 给了才画成可点的列头 */
  onSort?: (key: string) => void
  /** 给了之后每一行可点、可用键盘打开 */
  onRowClick?: (index: number) => void
  selected?: number
}) {
  const { t, lang } = useT()
  const onKey = (event: KeyboardEvent<HTMLTableRowElement>, index: number) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onRowClick?.(index)
    }
  }
  return (
    <div className={`sq-table-wrap${onRowClick ? ' is-browse' : ''}`} tabIndex={0} role="region" aria-label={label}>
      <table className="data sq-table">
        <thead>
          <tr>
            {columns.map((column) => {
              const active = sort?.key === column.key
              const sortable = !!onSort && column.sortable !== false
              const title = <>{column.label}{column.unit && <span className="sq-th-unit"> ({column.unit})</span>}</>
              return (
                <th key={column.key} scope="col" className={column.numeric ? 'n' : undefined}
                    aria-sort={sortable ? (active ? (sort!.direction === 'desc' ? 'descending' : 'ascending') : 'none') : undefined}
                    title={column.hint || undefined}>
                  {sortable
                    ? (
                      <button type="button" className="sq-th-btn" onClick={() => onSort!(column.key)}
                              title={t('sq.table.sortBy', { column: column.label })}>
                        {title}
                        <span className="sq-sort-mark" aria-hidden="true">{active ? (sort!.direction === 'desc' ? '▼' : '▲') : ''}</span>
                      </button>
                    )
                    : title}
                  {column.label !== column.key && <span className="sq-th-sub mono">{column.key}</span>}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => {
            const cells = columns.map((column, position) => {
              const value = row[column.index ?? position]
              const cell = formatCell(value, lang, { unit: column.unit, plain: column.plain, kind: column.numeric ? 'number' : '' })
              const clipped = column.clip && cell.text.length > 36
              const classes = [
                column.numeric || cell.numeric ? 'n' : '',
                cell.empty ? 'sq-empty-cell' : '',
                column.clip ? 'sq-clip' : cell.text.length > 80 ? 'sq-wrap' : '',
              ].filter(Boolean).join(' ')
              const slot = column.slots && !cell.empty ? column.slots.get(String(value)) ?? 0 : 0
              return (
                <td key={column.key} className={classes || undefined} title={clipped ? cell.text : undefined}>
                  {cell.empty
                    ? <span aria-label={t('sq.table.empty')}>—</span>
                    : slot ? <ValuePill text={cell.text} slot={slot} /> : cell.text}
                </td>
              )
            })
            return onRowClick
              ? (
                <tr key={rowIndex} className="is-clickable" tabIndex={0} aria-current={rowIndex === selected || undefined}
                    onClick={() => onRowClick(rowIndex)} onKeyDown={(event) => onKey(event, rowIndex)}>
                  {cells}
                </tr>
              )
              : <tr key={rowIndex}>{cells}</tr>
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 翻页。只有一页时不画 */
export function Pager({ page, pageCount, onPage }: { page: number; pageCount: number; onPage: (page: number) => void }) {
  const { t } = useT()
  if (pageCount <= 1) return null
  return (
    <span className="sq-pager">
      <button type="button" className="btn btn-secondary btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        {t('sq.table.prev')}
      </button>
      <span>{t('sq.table.pageOf', { page, pages: pageCount })}</span>
      <button type="button" className="btn btn-secondary btn-sm" disabled={page >= pageCount} onClick={() => onPage(page + 1)}>
        {t('sq.table.next')}
      </button>
    </span>
  )
}
