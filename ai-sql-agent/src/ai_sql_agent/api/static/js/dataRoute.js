/* 一张表「正在怎么看」写进地址里：搜索词、筛选条件、排序、页码、标签页。

   #/data/<库>/<表>?q=…&f=<字段>:<条件>:<值>&sort=<字段>.desc&page=2&size=50&tab=fields

   这样刷新不丢筛选，「把我正在看的这批记录发给同事」发的就是这个地址，
   从一条记录跳到另一张表里的对应记录也只是一个普通链接。
   地址里 # 后面的部分不会发给服务端；真正读数据时条件走请求体，由服务端按清单核对。

   筛选条件的三段各自先转义再用冒号连起来（转义之后段内不会再有冒号），
   所以字段名、筛选值里有冒号、空格、中文都不影响拆分。 */

export const PAGE_SIZES = [25, 50, 100];
const OPERATORS = ['eq', 'ne', 'contains', 'starts_with', 'gt', 'gte', 'lt', 'lte', 'is_empty', 'not_empty'];
const TABS = ['records', 'fields'];
const MAX_FILTERS = 5;

export const freshView = () => ({
  tab: 'records', search: '', filters: [], sort: null, page: 1, pageSize: PAGE_SIZES[0],
});

const decode = (text) => {
  try { return decodeURIComponent(text); } catch (_) { return null; }
};

/* 查询串 → 视图状态。认不出来的部分直接丢掉，不报错：地址是人会手改、会被截断的东西。 */
export function parseView(queryString = '') {
  const view = freshView();
  const params = new URLSearchParams(queryString);

  view.search = (params.get('q') || '').slice(0, 100);
  params.getAll('f').slice(0, MAX_FILTERS).forEach((item) => {
    const parts = item.split(':');
    if (parts.length !== 3) return;
    const [column, op, value] = [decode(parts[0]), parts[1], decode(parts[2])];
    if (!column || value === null || !OPERATORS.includes(op)) return;
    view.filters.push({ column, op, value });
  });

  const sort = params.get('sort') || '';
  const match = /^(.*)\.(asc|desc)$/.exec(sort);
  if (match && match[1]) view.sort = { key: match[1], direction: match[2] };

  const page = Number(params.get('page'));
  if (Number.isInteger(page) && page >= 1) view.page = page;
  const size = Number(params.get('size'));
  if (PAGE_SIZES.includes(size)) view.pageSize = size;
  if (TABS.includes(params.get('tab'))) view.tab = params.get('tab');
  return view;
}

/* 视图状态 → 查询串（不带问号）。和默认值一样的不写，地址尽量短。 */
export function formatView(view) {
  const params = new URLSearchParams();
  if (view.search) params.set('q', view.search);
  (view.filters || []).forEach((filter) => {
    params.append('f', [
      encodeURIComponent(filter.column), filter.op, encodeURIComponent(filter.value ?? ''),
    ].join(':'));
  });
  if (view.sort) params.set('sort', `${view.sort.key}.${view.sort.direction}`);
  if (view.page > 1) params.set('page', String(view.page));
  if (view.pageSize !== PAGE_SIZES[0]) params.set('size', String(view.pageSize));
  if (view.tab && view.tab !== TABS[0]) params.set('tab', view.tab);
  return params.toString();
}

/* 数据页里的地址。库、表、查询串都是可选的。 */
export function dataHref(database = '', table = '', view = null) {
  let href = '#/data';
  if (database) href += '/' + encodeURIComponent(database);
  if (database && table) href += '/' + encodeURIComponent(table);
  const query = view ? formatView({ ...freshView(), ...view }) : '';
  return query ? `${href}?${query}` : href;
}
