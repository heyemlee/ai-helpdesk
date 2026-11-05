/**
 * 字段说明（表页的"字段"标签页）：数据字典里每个字段的业务名、类型、单位、角色、关联，
 * 以及整张表里它填了多少（移植自旧控制台 exploreTable.js 的 fieldsTab）。
 */
import type { Catalog, CatalogColumn, CatalogTable, TableProfile } from '@/api/sql/types'
import { useT } from '@/i18n'
import { domainTitle, findDomain, findTable } from '@/lib/sql/catalog'
import { exploreHref } from '@/lib/sql/dataView'
import { descriptionOf, formatCount, formatNumber, formatRange, formatShare, labelOf, localized } from '@/lib/sql/format'
import { hasKey } from '@/lib/sql/labels'
import { statsOf, type ProfileColumnStats } from '@/lib/sql/profile'
import { Chip } from '../../ui'
import { Meter } from './charts'

function FieldItem({ catalog, column, stats, total }: {
  catalog: Catalog; column: CatalogColumn; stats: ProfileColumnStats | undefined; total: number | null
}) {
  const { t, lang } = useT()
  const roleKey = `sq.explore.role.${column.role}`
  const kindKey = `sq.explore.kind.${column.kind}`
  const note = descriptionOf(column, lang)
  const usage = localized(column, 'usage', lang)
  // 数据字典里当前语言能读的说明排最前；只有原文的那几段，先给界面语言能读的结构化信息（角色、取值、
  // 关联），原文退到后面。有的数据字典把用途原样接在说明后面：说明里已经有的那句用途不再重复一遍
  const dictionary = [note, usage.text && !note.text.includes(usage.text) ? usage : null]
    .filter((item): item is { text: string; original: boolean } => !!item && !!item.text)
  const paragraph = (item: { text: string; original: boolean }, index: number) => (
    <p key={index} className={item.original ? 'sq-original' : undefined} lang={item.original ? 'zh' : undefined}
       title={item.original ? t('sq.explore.originalNote') : undefined}>{item.text}</p>
  )
  const filled = !!stats && typeof total === 'number' && total > 0
  const range = stats && stats.min !== undefined
    ? (typeof stats.min === 'number'
      ? `${formatNumber(stats.min, lang, { unit: column.unit })} – ${formatNumber(stats.max, lang, { unit: column.unit })}`
      : formatRange({ min: String(stats.min).slice(0, 10), max: String(stats.max).slice(0, 10) }))
    : ''
  const share = filled ? formatShare(stats!.filled, total!, lang) || '0%' : ''
  return (
    <li>
      <div>
        <div className="sq-field-name">{labelOf(column, lang)}</div>
        <div className="sq-tech">{column.name}</div>
        <div className="sq-field-facts">
          {column.status === 'missing' && <Chip tone="caution">{t('sq.explore.missing')}</Chip>}
          {column.primary_key && <Chip>{t('sq.explore.key')}</Chip>}
          {column.status !== 'missing' && column.previewable === false && <Chip tone="caution">{t('sq.explore.notInPreview')}</Chip>}
          {hasKey(kindKey) && column.status !== 'missing' && <Chip>{t(kindKey)}</Chip>}
          {column.unit && <Chip>{t('sq.explore.unit', { unit: column.unit })}</Chip>}
          {column.data_type && <span className="sq-tech">{column.data_type}</span>}
        </div>
      </div>
      <div className="sq-field-desc">
        {dictionary.filter((item) => !item.original).map(paragraph)}
        {hasKey(roleKey) && <p>{t(roleKey)}</p>}
        {column.value_range && (
          <p><span className="sq-muted">{t('sq.explore.values')}</span> <span className="mono">{column.value_range}</span></p>
        )}
        {(column.references ?? []).map((reference, index) => {
          const target = findTable(catalog, reference.database, reference.table)
          const domain = findDomain(catalog, reference.database)
          const name = `${domain ? domainTitle(domain, lang) : reference.database} › ${target ? labelOf(target, lang) : reference.table} › ${reference.column}`
          const remark = localized(reference, 'note', lang)
          return (
            <p key={index}>
              <span className="sq-muted">
                {t(reference.relation === 'cross_database' ? 'sq.explore.linkedAcross' : 'sq.explore.linked')}
              </span>{' '}
              <a href={exploreHref(reference.database, reference.table)}>{name}</a>
              {remark.text && (
                <> <span className={remark.original ? 'sq-original' : 'sq-muted'} lang={remark.original ? 'zh' : undefined}>{remark.text}</span></>
              )}
            </p>
          )
        })}
        {dictionary.filter((item) => item.original).map(paragraph)}
        {!dictionary.length && (
          <p className="sq-muted">{t(column.documented ? 'sq.explore.noDescription' : 'sq.explore.undocumented')}</p>
        )}
      </div>
      <div className="sq-field-fill">
        {filled && (
          <>
            <div className="sq-fill-line">
              <Meter part={stats!.filled} whole={total!} label={t('sq.explore.viz.fillLabel', { share })} />
              <span className="sq-fill-share">{share}</span>
            </div>
            <p className="sq-muted">{t('sq.explore.viz.filled', { filled: formatCount(stats!.filled, lang), total: formatCount(total, lang) })}</p>
            {range && <p className="sq-muted">{t('sq.explore.viz.range')} {range}</p>}
          </>
        )}
      </div>
    </li>
  )
}

export function FieldsTab({ catalog, table, base }: { catalog: Catalog; table: CatalogTable; base: TableProfile | null }) {
  const stats = new Map((base ? statsOf(base) : []).map((column) => [column.name, column]))
  const total = base ? base.total_rows : null
  return (
    <ul className="sq-field-list">
      {(table.columns ?? []).map((column) => (
        <FieldItem key={column.name} catalog={catalog} column={column} stats={stats.get(column.name)} total={total} />
      ))}
    </ul>
  )
}
