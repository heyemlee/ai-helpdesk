/**
 * 库的标识色块（移植自旧控制台 exploreParts.js 的 databaseSlot / dbTile）。问数和数据浏览共用。
 *
 * 和整个数据页一样，这里没有任何库名、表名：颜色按目录里的顺序分，服务端没给的画不出来。
 */
import type { Catalog } from '@/api/sql/types'
import { sortedDomains } from '@/lib/sql/catalog'

/**
 * 库的标识色：按目录里的顺序，前四个库各一个颜色（那四个色两两可辨，是校验过的一组），
 * 第五个起不上色——不为了凑数再生成颜色，色块里的缩写照样分得开。
 */
export function databaseSlot(catalog: Catalog | null | undefined, database: string): number {
  const index = sortedDomains(catalog).findIndex((domain) => domain.database === database)
  return index >= 0 && index < 4 ? index + 1 : 0
}

/** 色块里的缩写：库标识符第一段的前三个字母（契约规定库标识符形如"系统_db"） */
export function databaseCode(database: string): string {
  const head = String(database || '').split(/[^A-Za-z0-9]+/).find(Boolean) || String(database || '?')
  return head.slice(0, 3).toUpperCase()
}

export function DbTile({ catalog, database, size = '' }: {
  catalog: Catalog | null | undefined
  database: string
  size?: '' | 'is-lg' | 'is-sm' | 'is-xs'
}) {
  return (
    <span className={`sq-db-tile db-c${databaseSlot(catalog, database)}${size ? ` ${size}` : ''}`} aria-hidden="true">
      {databaseCode(database)}
    </span>
  )
}
