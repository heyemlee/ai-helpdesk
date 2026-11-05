# 跨仓库 golden fixtures

两侧共用的**同一批**载荷样本。清单只有一份：[`index.json`](index.json)。

## 目录

| 路径 | 是什么 |
| --- | --- |
| `data-part/valid/` | artifact data part 正例 |
| `data-part/schema-invalid/` | **必须被 schema 拒绝**的载荷 |
| `data-part/trust-negative/` | **schema 通过、但业务上不可作出处**的载荷 |
| `progress-event/valid/` | progress 事件信封正例 |
| `progress-event/schema-invalid/` | **必须被拒绝**的信封 |
| `terminal-event/valid/` | 终态事件信封正例：`completed` / `failed` / `canceled`（v1.13 新增） |
| `terminal-event/schema-invalid/` | **必须被拒绝**的终态信封（v1.13 新增） |

`trust-negative` 与 `schema-invalid` 分开是有意的，这是本目录最重要的一条结构：

- **`schema-invalid`** 防的是**形状坏了**——缺字段、类型不对、自相矛盾。机器全能拦。
- **`trust-negative`** 防的是**形状完好但数据不能用**——查询失败了、带风险、
  降级重放、被截断、零行。**schema 一个都拦不住**，只能靠业务判据。

第二类才是这条链路上真正会出事的地方：它们看起来完全正常。
`data-part/trust-negative/01-failed-step.json` 就是最好的例子——
SQL 全挂了，但 `queried_database=true`、`has_risk=false`、`degraded=false`，
三个信任布尔一个都不否定它。

## 文件格式

**每个 fixture 文件就是载荷本身，没有包装层。** 元数据（期望校验结果、期望的
信任判定、这条用例在防什么）全在 `index.json` 里。这样两侧都能把文件直接喂进
自己的消费路径，不用先剥一层。

`index.json` 的每条用例：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定标识，测试报告里用它 |
| `group` | 对应 `CONSUMER-TESTS.md` 的 A–F 分组 |
| `file` | 相对本目录的路径 |
| `schema` | 相对仓库根的 schema 路径 |
| `expect` | `valid` / `invalid`——**期望的 schema 校验结果** |
| `trust` | `usable_as_source` / `partial` / `no_data` / `not_usable` / `n/a`——**期望的业务判定** |
| `why` | 这条用例在防什么。**不写清楚就等于没有这条用例** |

`expect` 与 `trust` 是两个正交的维度，不要混。`dp-trust-03` 是
`expect: valid` + `trust: not_usable`——**合法但不可信**，正是最危险的那一格。

> **这里原先举的是 `dp-trust-01`，v1.12 换掉了。** 它在 v1.12 随
> `execution_status` 提级改判为 `expect: invalid`，不再落在那一格里。
> 换个例子就行，但这件事本身说明了为什么这两维不能混：**同一份载荷的
> `expect` 会随契约面变，`trust` 不会**——`dp-trust-01` 的业务判定从头到尾
> 都是 `not_usable`，变的只是 schema 还拦不拦得住它。

## 加用例的规矩

1. **先加清单条目，再加文件。** `tools/check_contract.py` 会检查两边对得上：
   文件没进清单、清单指向不存在的文件，都会失败。
2. **`why` 要写成一句能被反驳的话**，不要写"测试缺字段的情况"。
   写"缺 `has_risk` 时调用方今天会把它当成无风险并采信"——这样它错了才看得出来。
3. **反例必须真的被拒。** 检查脚本会验证 `expect: invalid` 的用例确实校验失败；
   一条不报错的反例等于没有在保护任何东西。
4. **不要把 fixture 拷进实现仓库。** 拷贝就是副本，副本会漂——
   这份契约已经为副本漂移翻过一次车（见契约附录 B）。用 submodule 或 CI 里
   checkout 到固定 tag。

## 自检

```bash
pip install jsonschema referencing
python3 tools/check_contract.py fixtures
```
