# 数据库侧最小授权：落地流程

> 生成授权 SQL 的工具（`python -m ai_sql_agent.ops least-privilege`）在真实 PostgreSQL 上验证过；
> 这份文档把它变成运维照着做就能执行的流程。
>
> **标着「需要人执行」的步骤不要自动化**：它们要么改生产库的权限，
> 要么需要管理员凭证。谁按下回车、什么时候、结果是什么，都要留痕。

## 0. 为什么要做（一句话）

应用层的授权关卡（`ai_sql_agent.auth.enforcement`）是**第二道**防线。
只读账号只挡写、不挡读密列——一个能连库的只读账号照样 `SELECT unit_cost`。
这一步的目标是：**应用层漏判也越不了权**。

## 1. 前置条件

| 项 | 值 | 谁准备 |
| --- | --- | --- |
| 目标库 | 语义层里声明的每一个业务库（`GET /a2a/databases` 可拉取当前清单） | —— |
| 管理员连接串 | 能 `GRANT` / `CREATE POLICY` 的账号，通常是库 owner | **需要人执行**：DBA 提供，不要写进代码库 |
| 只读角色 | `python -m ai_sql_agent.ops provision` 建出来的那个 | 已有则复用 |
| 策略文件 | `deploy/api_keys.json` 的 `policies` 段 | 业务方确认「谁能看什么」 |

> **PostgreSQL only。** SQLite 没有列级 GRANT 也没有 RLS，演示库因此只有应用层
> 的表/列隔离。**不要**用「在生成的 SQL 上自动加 WHERE」来补 SQLite——
> 那是在模型的输出上做字符串手术，绕过它的方法比它本身多。

## 2. 执行顺序

每个 `(业务库, 角色)` 组合跑一遍。下面以 `erp_db` × `estimating` 为例。

### 2.1 先看要执行什么（不连库改任何东西）

```bash
python -m ai_sql_agent.ops least-privilege \
  --dsn "$ERP_ADMIN_DSN" --keys deploy/api_keys.json \
  --policy estimating --database erp_db --role ai_sql_ro \
  > evidence/least-privilege_erp_db_estimating_$(date +%F).sql
```

产出是纯 SQL。**需要人执行**：把它给 DBA 审一遍再往下走——
里面有 `REVOKE ALL`，审错了会让线上查询集体失权。

### 2.2 记录执行前的状态（回滚要用）

```bash
psql "$ERP_ADMIN_DSN" -Atc "
  SELECT grantee, table_name, privilege_type, string_agg(column_name, ',')
  FROM information_schema.column_privileges
  WHERE grantee = 'ai_sql_ro' GROUP BY 1,2,3 ORDER BY 2;
" > evidence/before_erp_db_$(date +%F).txt
```

**这一步不能跳。** 第 4 节的回滚就是把这份东西放回去。

### 2.3 执行

```bash
python -m ai_sql_agent.ops least-privilege \
  --dsn "$ERP_ADMIN_DSN" --keys deploy/api_keys.json \
  --policy estimating --database erp_db --role ai_sql_ro --apply
```

**需要人执行**，需要管理员权限。建议在业务低峰期，并且一个库一个库来——
一次全上，出问题时分不清是哪个库的授权导致的。

### 2.4 验证（**这一步才是结论**）

```bash
python -m ai_sql_agent.ops verify-privileges \
  --dsn "$ERP_ADMIN_DSN" --readonly-dsn "$ERP_RO_DSN" \
  --keys deploy/api_keys.json --policy estimating --database erp_db \
  --role ai_sql_ro --json \
  > evidence/verify_erp_db_estimating_$(date +%F).json
```

它**连上受限角色自己的连接真查一次**，确认策略禁止的表和列在数据库侧确实被拒。

> 生成 SQL 只是意图。执行过没执行过、执行时报没报错、后来有没有人手工
> `GRANT ALL` 回去——这些只有真查一次才知道。所以**验证的退出码才是结论**，
> 不是 2.3 有没有报错。

退出码：`0` = 该拒的都拒了；`1` = 有漏的，`problems` 字段列出来。

## 3. 证据：存哪、谁看

| 文件 | 内容 | 保留 |
| --- | --- | --- |
| `evidence/least-privilege_<库>_<角色>_<日期>.sql` | 执行了什么 | 长期 |
| `evidence/before_<库>_<日期>.txt` | 执行前的授权快照（回滚用） | 到下一次变更 |
| `evidence/verify_<库>_<角色>_<日期>.json` | **结论**：`enforced` 真假 + 逐项 findings | 长期 |

- **谁看**：每次改 `policies` 段之后由改动的提出人跑一遍 2.4 并把 JSON 附在变更单上；
  `enforced: false` 不允许合并。
- **定期**：建议把 2.4 挂成周期任务（只读、无副作用，生产库可以跑），
  `enforced` 变 false 就告警——它会抓到「有人手工 GRANT 回去了」这类漂移。
- JSON 里的 `checked_at` 是判断证据新鲜度的唯一依据：一份半年前的
  `enforced: true` 不说明今天的状态。

## 4. 回滚

按影响面从小到大：

1. **只回滚列级授权**（最常见：某个角色被砍多了）
   ```bash
   psql "$ERP_ADMIN_DSN" -c 'GRANT SELECT ON <schema>.<table> TO ai_sql_ro;'
   ```
   表级 `GRANT SELECT` 会覆盖列级授权，立即恢复该表的全列可读。

2. **回滚整个库的授权**：把 2.2 那份快照喂回去（人工比对后逐条执行）。

3. **回滚行级安全**
   ```bash
   psql "$ERP_ADMIN_DSN" -c 'ALTER TABLE <schema>.<table> DISABLE ROW LEVEL SECURITY;'
   ```

4. **应用层不受影响**：以上任何一步都不改应用层的授权关卡。
   即使数据库侧全部回滚，`ai_sql_agent.auth.enforcement` 仍在拦——
   回到「只有一道防线」的状态，不是「没有防线」。

## 5. 行级安全（RLS）

`--apply` 会一并执行策略里 `row_filters` 声明的 RLS（设计见 `authz-model.md`）。
两件事必须同时成立，缺一不可：

1. 策略里写了非空 `predicate`；
2. 执行器在事务里设了 `app.user_id`——这一条已经在代码里
   （`src/ai_sql_agent/execution/postgres_executor.py`），无需运维操作。

验证：用两个不同的最终用户各查一次，行数应当不同。
`tests/test_authorization_postgres.py::RowLevelSecurityThroughTheAgentTest`
就是这个断言，CI 里跑。

## 6. 这份流程覆盖不到的

- **迁移账号与运行账号分离**：本流程只管运行账号（只读）。建表/改表用的迁移账号
  是另一套凭证，不在这里。
- **多 schema**：`--schema` 一次只处理一个。多 schema 的库要跑多次。
- **非 PostgreSQL 的业务库**：见第 1 节的框。
