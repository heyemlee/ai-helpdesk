/* 表与表怎么连。

   线只有两种来源，都是目录里已经给出来的：库内的外键，和语义层登记的跨库关联
   （每个字段的 references）。这里不按字段名猜——两张表各有一个同名字段，
   不等于它们说的是同一样东西。

   用同一个值连在一起的字段归成一组「共用的键」：订单表的订单号连着生产记录的订单号、
   配送记录的订单号，这三处就是一组。看一条订单时去另外两张表里找同一个订单号，
   靠的就是这个分组。分组是传递的：A 连 B、B 连 C，A 和 C 也在同一组里。 */

const SEP = '\u0000';
const nodeId = (database, table, column) => [database, table, column].join(SEP);
export const tableKey = (database, table) => database + SEP + table;

const cache = new WeakMap();

/* domains: /catalog 返回的 domains。结果按这份数组缓存——目录刷新后数组是新的，自然重算。 */
export function buildLinks(domains) {
  if (!Array.isArray(domains)) return { groups: [], byTable: new Map(), edges: [] };
  if (cache.has(domains)) return cache.get(domains);

  const nodes = new Map();
  domains.forEach((domain, order) => {
    (domain.tables || []).forEach((table) => {
      (table.columns || []).forEach((column) => {
        nodes.set(nodeId(domain.database, table.name, column.name), {
          database: domain.database,
          table: table.name,
          column: column.name,
          domain,
          tableEntry: table,
          columnEntry: column,
          order,
          degree: 0,
        });
      });
    });
  });

  // 并查集：有关联的两个字段并到一组
  const parent = new Map();
  const find = (id) => {
    let root = id;
    while (parent.get(root) !== root) root = parent.get(root);
    let cursor = id;
    while (parent.get(cursor) !== root) {
      const next = parent.get(cursor);
      parent.set(cursor, root);
      cursor = next;
    }
    return root;
  };

  const edges = [];
  const seen = new Set();
  nodes.forEach((node, id) => {
    (node.columnEntry.references || []).forEach((reference) => {
      const otherId = nodeId(reference.database, reference.table, reference.column);
      const other = nodes.get(otherId);
      // 对端不在目录里（没有权限，或者那个库读不到）：这条线不画
      if (!other || otherId === id) return;
      // 跨库关联在两端各登记了一次，只算一条
      const pair = [id, otherId].sort().join(SEP + SEP);
      if (seen.has(pair)) return;
      seen.add(pair);
      [id, otherId].forEach((item) => { if (!parent.has(item)) parent.set(item, item); });
      parent.set(find(id), find(otherId));
      node.degree += 1;
      other.degree += 1;
      edges.push({ from: node, to: other, relation: reference.relation, reference });
    });
  });

  const buckets = new Map();
  parent.forEach((_, id) => {
    const root = find(id);
    if (!buckets.has(root)) buckets.set(root, []);
    buckets.get(root).push(nodes.get(id));
  });

  const groups = [...buckets.values()].map((members) => {
    members.sort((a, b) => a.order - b.order);
    // 连得最多的那一个是这组键的「本家」（通常是主表的主键）：组名用它的名字
    const hub = members.reduce((best, item) => (
      item.degree > best.degree || (item.degree === best.degree && item.columnEntry.primary_key && !best.columnEntry.primary_key)
        ? item : best), members[0]);
    return {
      id: nodeId(hub.database, hub.table, hub.column),
      hub,
      members,
      databases: [...new Set(members.map((item) => item.database))],
    };
  }).sort((a, b) => b.members.length - a.members.length || a.hub.order - b.hub.order);

  const byTable = new Map();
  groups.forEach((group) => {
    group.members.forEach((member) => {
      const key = tableKey(member.database, member.table);
      if (!byTable.has(key)) byTable.set(key, []);
      byTable.get(key).push({ member, group });
    });
  });

  const result = { groups, byTable, edges };
  cache.set(domains, result);
  return result;
}

/* 这张表通过哪些字段连到哪些别的表：
   [{ column, group, others: [同组里别的表上的那个字段] }]，一个字段一条。 */
export function relatedTables(links, database, table) {
  return (links.byTable.get(tableKey(database, table)) || [])
    .map(({ member, group }) => ({
      column: member.column,
      member,
      group,
      others: group.members.filter((item) => !(item.database === database && item.table === table)),
    }))
    .filter((item) => item.others.length);
}

/* 这个库和别的库之间登记了哪些关联（库内外键不算）：按对面的库归拢。 */
export function crossDatabaseLinks(links, database) {
  const found = new Map();
  links.edges.forEach((edge) => {
    const mine = edge.from.database === database ? edge.from : edge.to.database === database ? edge.to : null;
    if (!mine) return;
    const other = mine === edge.from ? edge.to : edge.from;
    if (other.database === database) return;
    if (!found.has(other.database)) found.set(other.database, { domain: other.domain, links: [] });
    found.get(other.database).links.push({ mine, other, reference: edge.reference });
  });
  return [...found.values()];
}
