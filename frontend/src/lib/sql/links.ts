/**
 * 表与表怎么连（移植自旧控制台的 links.js）。
 *
 * 线只有两种来源，都是目录里已经给出来的：库内的外键，和语义层登记的跨库关联（每个字段的
 * references）。这里不按字段名猜——两张表各有一个同名字段，不等于它们说的是同一样东西。
 *
 * 用同一个值连在一起的字段归成一组"共用的键"：订单表的订单号连着生产记录的订单号、配送记录的订单号，
 * 这三处就是一组。看一条订单时去另外两张表里找同一个订单号，靠的就是这个分组。
 * 分组是传递的：A 连 B、B 连 C，A 和 C 也在同一组里。
 */
import type { CatalogColumn, CatalogDomain, CatalogTable, ColumnReference } from '@/api/sql/types'

const SEP = '\u0000'
const nodeId = (database: string, table: string, column: string) => [database, table, column].join(SEP)
export const tableKey = (database: string, table: string) => database + SEP + table

export interface LinkNode {
  database: string
  table: string
  column: string
  domain: CatalogDomain
  tableEntry: CatalogTable
  columnEntry: CatalogColumn
  /** 所在的库在目录里的位置 */
  order: number
  degree: number
}

export interface LinkGroup {
  id: string
  /** 连得最多的那一个是这组键的"本家"（通常是主表的主键）：组名用它的名字 */
  hub: LinkNode
  members: LinkNode[]
  databases: string[]
}

export interface LinkEdge {
  from: LinkNode
  to: LinkNode
  relation: string
  reference: ColumnReference
}

export interface Links {
  groups: LinkGroup[]
  byTable: Map<string, Array<{ member: LinkNode; group: LinkGroup }>>
  edges: LinkEdge[]
}

const EMPTY: Links = { groups: [], byTable: new Map(), edges: [] }
const cache = new WeakMap<CatalogDomain[], Links>()

/** domains：/catalog 返回的 domains。结果按这份数组缓存——目录刷新后数组是新的，自然重算。 */
export function buildLinks(domains: CatalogDomain[] | null | undefined): Links {
  if (!Array.isArray(domains)) return EMPTY
  const known = cache.get(domains)
  if (known) return known

  const nodes = new Map<string, LinkNode>()
  domains.forEach((domain, order) => {
    for (const table of domain.tables ?? []) {
      for (const column of table.columns ?? []) {
        nodes.set(nodeId(domain.database, table.name, column.name), {
          database: domain.database,
          table: table.name,
          column: column.name,
          domain,
          tableEntry: table,
          columnEntry: column,
          order,
          degree: 0,
        })
      }
    }
  })

  // 并查集：有关联的两个字段并到一组
  const parent = new Map<string, string>()
  const find = (id: string): string => {
    let root = id
    while (parent.get(root) !== root) root = parent.get(root)!
    let cursor = id
    while (parent.get(cursor) !== root) {
      const next = parent.get(cursor)!
      parent.set(cursor, root)
      cursor = next
    }
    return root
  }

  const edges: LinkEdge[] = []
  const seen = new Set<string>()
  nodes.forEach((node, id) => {
    for (const reference of node.columnEntry.references ?? []) {
      const otherId = nodeId(reference.database, reference.table, reference.column)
      const other = nodes.get(otherId)
      // 对端不在目录里（没有权限，或者那个库读不到）：这条线不画
      if (!other || otherId === id) continue
      // 跨库关联在两端各登记了一次，只算一条
      const pair = [id, otherId].sort().join(SEP + SEP)
      if (seen.has(pair)) continue
      seen.add(pair)
      for (const item of [id, otherId]) if (!parent.has(item)) parent.set(item, item)
      parent.set(find(id), find(otherId))
      node.degree += 1
      other.degree += 1
      edges.push({ from: node, to: other, relation: reference.relation, reference })
    }
  })

  const buckets = new Map<string, LinkNode[]>()
  parent.forEach((_, id) => {
    const root = find(id)
    if (!buckets.has(root)) buckets.set(root, [])
    buckets.get(root)!.push(nodes.get(id)!)
  })

  const groups: LinkGroup[] = [...buckets.values()].map((members) => {
    members.sort((a, b) => a.order - b.order)
    const hub = members.reduce((best, item) => (
      item.degree > best.degree
        || (item.degree === best.degree && item.columnEntry.primary_key && !best.columnEntry.primary_key)
        ? item : best), members[0]!)
    return {
      id: nodeId(hub.database, hub.table, hub.column),
      hub,
      members,
      databases: [...new Set(members.map((item) => item.database))],
    }
  }).sort((a, b) => b.members.length - a.members.length || a.hub.order - b.hub.order)

  const byTable = new Map<string, Array<{ member: LinkNode; group: LinkGroup }>>()
  for (const group of groups) {
    for (const member of group.members) {
      const key = tableKey(member.database, member.table)
      if (!byTable.has(key)) byTable.set(key, [])
      byTable.get(key)!.push({ member, group })
    }
  }

  const result: Links = { groups, byTable, edges }
  cache.set(domains, result)
  return result
}

export interface RelatedTable {
  /** 这张表上的那个字段 */
  column: string
  member: LinkNode
  group: LinkGroup
  /** 同组里别的表上的那个字段 */
  others: LinkNode[]
}

/** 这张表通过哪些字段连到哪些别的表：一个字段一条。 */
export function relatedTables(links: Links, database: string, table: string): RelatedTable[] {
  return (links.byTable.get(tableKey(database, table)) ?? [])
    .map(({ member, group }) => ({
      column: member.column,
      member,
      group,
      others: group.members.filter((item) => !(item.database === database && item.table === table)),
    }))
    .filter((item) => item.others.length > 0)
}

/** 这个库和别的库之间登记了哪些关联（库内外键不算）：按对面的库归拢。 */
export function crossDatabaseLinks(links: Links, database: string):
  Array<{ domain: CatalogDomain; links: Array<{ mine: LinkNode; other: LinkNode; reference: ColumnReference }> }> {
  const found = new Map<string, { domain: CatalogDomain; links: Array<{ mine: LinkNode; other: LinkNode; reference: ColumnReference }> }>()
  for (const edge of links.edges) {
    const mine = edge.from.database === database ? edge.from : edge.to.database === database ? edge.to : null
    if (!mine) continue
    const other = mine === edge.from ? edge.to : edge.from
    if (other.database === database) continue
    if (!found.has(other.database)) found.set(other.database, { domain: other.domain, links: [] })
    found.get(other.database)!.links.push({ mine, other, reference: edge.reference })
  }
  return [...found.values()]
}
