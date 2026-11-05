# 授权模型：行范围与 impersonation（设计）

> 只有表、列两级策略时，有两件事表达不出来：行范围，以及 impersonation 时最终用户自己的权限。
> 这份设计补这两块。它牵动 `src/ai_sql_agent/auth/objects.py`、`enforcement.py` 与
> key store 的形状，而且**默认值选错，就是把「没配就全放开」的问题换个维度重演**。

## 0. 只有表、列两级时缺什么

| 维度 | 表、列两级策略 | 缺口 |
| --- | --- | --- |
| 库 / 表 | `allow_tables` / `deny_tables` glob | —— |
| 列 | `deny_columns` glob | —— |
| **行** | **没有** | 「不同客户只能看自己的订单」表达不出来 |
| **最终用户身份** | impersonation 换 `user_id`，**权限仍是服务身份的** | alice 和 bob 权限相同 |

第三行的后果：RLS 的过滤条件只能由运维手写，推不出来，所以
`src/ai_sql_agent/ops/least_privilege.py` 生成不了它。
第四行的后果：一把 `can_impersonate` 的服务 Key 替所有员工发请求，
每个员工拿到的都是**服务身份的全部权限**。

## 1. 行范围：`row_filters`

```json
"policies": {
  "estimating": {
    "allow_tables": ["erp_db.*"],
    "row_filters": [
      {"table": "erp_db.job",   "predicate": "owner_id = current_setting('app.user_id', true)"},
      {"table": "erp_db.*",     "predicate": "*"}
    ]
  }
}
```

- `predicate` 是一段 **SQL 布尔表达式**，落到 PostgreSQL 的 RLS 上；
- `predicate: "*"` 是**显式声明「这张表不做行隔离」**；
- 规则按声明顺序取**第一条匹配**（上面的例子：`job` 按 owner 过滤，其余全放）。

### 1.1 最终用户身份怎么进到 SQL 里

`current_setting('app.user_id', true)` 的值由**执行器**在事务里设：

```
BEGIN → SET TRANSACTION READ ONLY → SET LOCAL app.user_id = <principal.user_id> → 查询
```

`SET LOCAL` 是事务级的，随 rollback 一起还原，所以连接池复用不会串身份。
这一步让 RLS 在**通过 agent 发起的查询**上真的生效，而不是只在有人手工连库时生效。

没有绑定授权作用域时设成空串：RLS 的 `USING (owner_id = current_setting(...))`
对空串不匹配任何行 —— fail closed，与执行器那道关卡方向一致。

### 1.2 默认值：**不是「放行所有行」，也不是「拒绝所有行」**

这是本设计最要紧的一处，两个显而易见的选项都不能选：

| 选项 | 后果 |
| --- | --- |
| 没配 = 放行所有行 | **把 `full_access` 的老毛病换个维度复发**：漏配一张表就是全表可见，且无症状 |
| 没配 = 拒绝所有行 | 现有部署一条策略都没有 `row_filters`，翻上去等于**服务立刻不可用** |

所以取第三种：**运行时按「放行」，配置校验时按「必须显式声明」**。

- 运行时：没有匹配的 `row_filters` 条目 → 不加行过滤（与没有行范围时一致，不破坏已有部署）；
- **生产模式启动检查**：策略里任何一张 `allow_tables` 覆盖到的表，
  如果没有任何 `row_filters` 条目匹配它（包括显式的 `"*"`），**拒绝启动**。

于是「这张表不做行隔离」这句话必须是**有人写下来的**，而不是默认值兜出来的。
这与 `roles=()` 的处理是同一条原则：*显式的宽松可以接受，默认的宽松不行。*

开发模式下只告警。

## 2. impersonation：取交集，不是二选一

```json
"users": {
  "alice": {"roles": ["estimating"]},
  "bob":   {"roles": ["workshop"]}
}
```

`ApiKeyStore.resolve(key, requested_user_id)` 在 impersonation 时：

```
最终权限 = 服务身份的策略  ∩  最终用户的策略
```

**取交集不是二选一。** 服务 Key 的策略是「这个应用最多能碰什么」，
最终用户的策略是「这个人最多能碰什么」，两者都是上界，都要守。

### 2.1 交集怎么算

**不做 glob 代数。** `intersect(["erp_db.*"], ["erp_db.job"])` 要算出一个等价的
glob 列表，既复杂又容易错。改成一个组合对象：

```python
IntersectionPolicy(service=..., user=...)
    allows_table(...)      → 两边都放行才放行
    allows_column(...)     → 同上
    restricts_columns(...) → 任一边有限制就算有限制（取更严的）
    row_predicate(...)     → 两个谓词 AND 起来
```

它满足 `SchemaAccessFilter` 协议加上授权关卡用的那几个扩展方法，
所以对 `enforcement.py` 完全透明。

### 2.2 迁移路径（现有 Key 怎么办）

| 配置形态 | 行为 | 生产模式 |
| --- | --- | --- |
| 没有 `users` 段 | 沿用服务身份的策略 | **拒绝启动**，如果存在 `can_impersonate` 的 Key |
| 有 `users` 段，命中 | 取交集 | 正常 |
| 有 `users` 段，未命中 | **拒绝**（这个人的权限没声明过，推不出来） | 正常 |

同样是「显式的宽松可以接受，默认的宽松不行」：
不配 `users` 的老部署照常跑，但**上生产之前必须把这件事写下来**。

## 3. 规则里的名字怎么比：拒绝从宽，放行从严

规则是 `库.表` / `库.表.列` 的 glob。名字的大小写怎么算，目录（`/catalog`）、检索裁剪
和 SQL 授权关卡用的是**同一条规则**（`src/ai_sql_agent/auth/identifiers.py`）——
如果各比各的——关卡把 SQL 里的名字全转成小写、策略按原样比——
`deny_tables: ["shop_db.Orders"]` 在关卡上就永远对不上，被拒的表照样查得出来。

规则跟着数据库自己的语义走：

| 数据库 | 名字怎么算同一个 |
| --- | --- |
| SQLite | 不区分大小写，加不加引号都一样（只对 ASCII 字母） |
| PostgreSQL | 没加引号的折成小写；加了引号的区分大小写——`"Orders"` 与 `orders` 是两张表 |

| 规则 | SQLite | PostgreSQL |
| --- | --- | --- |
| `deny_tables` / `deny_columns` / `row_filters` | 不区分大小写 | **不区分大小写**：`Orders` 这条会把 `"Orders"` 和 `orders` 一起拒掉 |
| `allow_tables` | 不区分大小写 | **必须与存储名大小写一致**：`orders` 放行 `orders`（SQL 里不加引号写成 `Orders` 的也是它），放不到 `"Orders"` 头上 |

为什么两个方向不对称：多拒一张同名的表，代价是有人来问一句；少拒一张，是数据已经
出去了。放行规则写错大小写，后果是「这张表查不了」——看得见，有人会来问。
所以没有做成「全部转小写再比」：那样一条只放行 `orders` 的规则会把 `"Orders"` 也放出去。

写规则时照着数据目录或 `information_schema` 里显示的名字抄即可。
PostgreSQL 分支只有判定逻辑的单元测试，没有在真实库上跑过带引号的混合大小写表。

### 3.1 关卡看得见什么：列引用、`rowid`、引擎内部对象

SQL 授权关卡是靠 SQL 的结构来判的，没有 schema，所以有三条保守规则（实现在
`auth/sql_analysis.py` 与 `auth/enforcement.py`，回归测试在 `tests/test_authorization_gaps.py`）：

- **每个不带前缀的列引用都要被问到**，包括 `HAVING`、与输出别名同名的 `ORDER BY` 项和
  表值函数的实参。分不清它是别名还是真列时按真列问——所以一个输出别名恰好和被禁列
  同名（`SELECT name, 1 AS salary … ORDER BY salary`）会被多拒一次。
  漏掉其中任何一处，`HAVING max(salary) > n` 就能把被禁列的值二分出来。
- **SQLite 的 `rowid` / `oid` / `_rowid_` 是主键列的别名**，关卡不知道哪一列是主键，
  所以只要这张表有任何列级限制（`deny_columns` 里有它），就拒绝对这三个名字的引用。
  PostgreSQL 里 `rowid` 只是个普通列名，不套这条。
- **SQLite 引擎自己的对象对所有角色一律拒绝**，不交给策略：`sqlite_*` 系统表
  （`sqlite_master` 及其别名 `sqlite_schema`）、`dbstat`、表值 `pragma_*` 函数。
  通配放行（`erp_db.*`）会把它们一起放行，表值函数又没有表名，
  `pragma_table_info('任意表')` 因此能列出任何表的列。PostgreSQL 的 `pg_catalog` /
  `information_schema` 不在这条里，仍按策略判定。

- **PostgreSQL：整行引用按 `e.*` 算。** 不带前缀的 `e` 如果恰好是 FROM 里的别名或表名
  （`row_to_json(e)`、`SELECT e FROM t e`、`e::text`、`(e).salary`），取到的是这张表的
  全部列，含被禁列；这张表有任何列级限制就拒绝。派生表 / CTE 的别名不算（它们的列在
  子查询自己那一轮判过了）。
- **把字符串当 SQL 或表名执行的函数一律拒绝**（PostgreSQL 方言）：`query_to_xml` /
  `table_to_xml` / `cursor_to_xml` / `schema_to_xml` / `database_to_xml` 及它们的
  `_and_xmlschema` / `xmlschema` 变体、`ts_stat`、`dblink*`——它们读的表不出现在 AST 里，
  按表和列判永远看不见。`set_config` 也拒：它能覆盖执行器为 RLS 设的
  `app.user_id`。这一条在关卡里重复了 `sql_guard` 的判断，是故意的：守卫在
  `readonly=False` 的执行器上不跑，关卡每次都跑。
  **以上 PostgreSQL 的几条都只验证了关卡的判定，没有在真实 PostgreSQL 上跑过。**

### 3.2 视图

视图会遮住它读的基表：`deny_tables` 禁了 `payroll`，`SELECT * FROM v_payroll` 的 SQL 文本里
却没有这个名字。关卡是纯函数，推不出哪些名字是视图，所以由 `ViewCatalog`
（`auth/views.py`）提供：来自数据库目录（SQLite 的 `sqlite_master`、PostgreSQL 的
`pg_class` + `pg_get_viewdef`，视图定义用同一套 sqlglot 分析解析），随授权作用域交给
关卡（`authorization_scope(..., views=...)`），不是进程全局。

管道启动时为每个库扫一次，之后每次 schema 轮询（`reload_schema`）都重扫——表指纹只看
表，感知不到视图的增删。目录在时，**受限策略**读视图 = 读它的全部基表（视图套视图展开到底）：

- 任一基表被策略拒绝 → 拒绝；
- 基表有列级限制 → 拒绝（视图列对应基表哪一列，关卡看不见，和 `SELECT *` 同一个理由）；
- 视图定义解析不了（`INDEXED BY` 之类 sqlglot 不认的写法）→ 拒绝，原因是「不知道」；
- 目录里没登记的对象（扫描之后才建的表或视图）→ 拒绝，要等下一次扫描（或手动重载）后才可查询；
- 目录扫描失败 → 对受限角色拒绝一切，直到下一次扫描成功。旧目录**不**保留：
  它可能正好缺了这次想看的那个视图。

没有任何 deny 的策略（`full_access()`、`ALLOW_ALL`）没有东西可遮，不受目录约束。
这个库从没登记过目录时（例如直接用执行器的脚本）关卡按老样子判——这是前提而不是
安全属性：直接构造执行器的调用方要自己登记并把目录放进作用域。
PostgreSQL 的目录扫描（`scan_postgres`）和视图展开**没有在真实 PostgreSQL 上验证**。
SQLite 更彻底的做法是让执行器连接装 `set_authorizer`，直接按 SQLite 报告的真实读取
判定，视图、`rowid`、解析差异一并消除——那在 `execution/`，不在这里。

## 4. 不在本设计范围内的

- **glob 代数**：交集用组合对象绕开了。要把交集落回一份可读的 `DataPolicy`
  （比如给运维看「这个人最终能查什么」），需要真的做 glob 求交，是独立一件事。
- **SQLite 的行隔离**：SQLite 没有 RLS。演示库因此只有应用层的表/列隔离，
  行隔离只在 PostgreSQL 上成立。**不要**用「在 SQL 里自动加 WHERE」来补——
  那等于在生成的 SQL 上做字符串手术，绕过它的方法比它本身多。
- **`users` 段的来源**：现在是 API Key 配置文件里的一段。接 LDAP / SSO
  取用户角色是另一件事，接口留在 `ApiKeyStore.user_roles()` 这一个点上。
