# 两侧 CI 必须跑的契约用例

> 配套：`a2a-streaming-contract.md`（v1.13）、`schema/`、`fixtures/`。
> 本文只说**跑什么、断言什么**，不规定用什么测试框架。
>
> 上面这个版本号由 `tools/check_contract.py version` 与契约状态行比对。
> **它曾经停在 v1.5 停了五个版本**（v1.6–v1.10），调用方在 v1.6 就把它
> 写进交回契约的反馈清单，仍然没人改——因为当时没有任何机器在看这一行。

## 为什么要有这份文件

前四版的教训都是同一句话的不同说法：**靠人读文档去保证两侧一致，保不住。**
v1.2 虚报、v1.3 推翻、v1.4 的证据表证错了东西——三次都不是因为谁不认真，
是因为"契约有没有被兑现"这件事**当时没有任何机器在检查**。

所以形状类的断言全部搬到 `schema/` + `fixtures/` 里，两侧 CI 各自跑同一组。
用例分叉就是副本漂移换个地方复发，所以**清单只有一份**：`fixtures/index.json`。

## 一条硬规则：**三个**承载者用同一组用例（v1.8：由两条改为三条）

三个承载者的 data part **完全同构**（§4.4.1）。这不是一句愿望——它是 §6.2
回滚能成立的前提：回滚会依次落在 `send` 和直连 `/query` 上，
任何一个的载荷和 `stream` 不一样，回滚就是静默降级。

所以 **A / B / C / D 四组用例必须在三个承载者上各跑一遍**，用同一份 fixture、
同一份断言。差别只在**从哪里把 data part 取出来**：

| 承载者 | data part 在哪 |
| --- | --- |
| `message/send` | `result.artifacts[0].parts[]` 里 `kind == "data"` 那一项的 `.data` |
| `message/stream` | `artifact-update` 事件的 `result.artifact.parts[]` 里同样那一项 |
| **`POST /query`（直连）** | 响应体顶层。**注意 `step_logs` ↔ `steps` 的过渡期**：服务端两个都发，调用方两个都认、`steps` 优先（§4.4.1） |

> **`/query` 是 v1.8 才纳入的**，而它是今天 `SQL_AGENT_TRANSPORT` 的默认值——
> 也就是说在此之前，**跑生产流量的那条路一直没有契约用例覆盖**。
> 优先把它接上，它的缺陷（`step_logs` 取不到 ⇒ 每次查询都不作出处）是静默的。

> 取出来之后的一切断言必须逐字相同。**如果你发现某组用例只能在一个承载者上跑，
> 那就是 §4.4 的同构承诺出问题了，去查同构，不要给用例开分支。**
>
> E / F 两组（progress 事件、事件顺序与终态）只适用于 A2A 那两条——
> `/query` 是同步无事件的，没有进度事件可言。这是范围差异，不是同构破例，
> 界线见 §4.4.1 末段。

## 用例分组

| 组 | 内容 | 服务端（ai-sql-agent） | 调用方（ai-helpdesk） |
| --- | --- | --- | --- |
| **A** | data part 正例 | 必跑 | 必跑 |
| **B** | data part schema 反例（缺字段 / 字符串布尔 / 自相矛盾） | 必跑 | **必跑，且是本组最重要的消费方** |
| **C** | 信任反例（schema 通过但不可作出处） | 必跑 | 必跑 |
| **D** | `database` 标识符 | 必跑 | 必跑 |
| **E** | progress 事件信封 | 必跑 | 必跑 |
| **F** | 事件顺序与终态（§4.2 / §5.1）；v1.13 起终态信封有 fixture | 必跑 | 必跑 |

### A 组：data part 正例

**断言：** fixture 过 `schema/artifact-data-part.schema.json`；
三个承载者（§4.4.1）取出来的 data part 逐字相等。

- 服务端：真跑一次查询，把产出的 data part 拿去过 schema。**不要拿 fixture 反过来喂自己。**
- 调用方：把 fixture 喂进 `_normalize()` 一类的归一函数，断言产出的三个布尔是**真布尔**且值正确。

### B 组：schema 反例——**必须被拒**

`fixtures/data-part/schema-invalid/` 下六个用例，**外加 v1.12 改判进来的 `dp-trust-01`**
（共七条）。**这一组是 §9.1 缺口的回归测试。**

> **`dp-trust-01` 为什么在这一组，而文件却还在 `trust-negative/` 下。**
> v1.12 把 `execution_status` 提级为 `required` 之后，这份不带该字段的载荷
> **不再合法**，唯一的拒绝理由就是缺它——所以它按性质属于 B 组。
> **文件没有跟着挪目录**：两侧都按字面路径读它，挪一次就会红在别人仓库里。
> 分组看 `fixtures/index.json` 的 `group`，不要按目录名推断。

**服务端断言：** 自己产出的 data part 永远不长成这六种样子。
最省的做法是在 A 组那次真实查询后顺手再过一遍 schema——反例本身服务端不用构造。

**调用方断言（两条，缺一不可）：**

1. **校验必须失败。** 把 fixture 喂进消费路径，schema 校验要报错。
2. **回落方向必须是"不可信"。** 这一条比第 1 条更重要——
   校验失败之后走哪条分支才是真正的安全属性。断言：
   - 缺 `has_risk` / `degraded` → **判为不可信**（不是"无风险、未降级"）；
   - `"false"` 这类字符串 → **判为不可信**（不是 `bool("false") == True` 那个结果）；
   - 三个全缺 → 判为不可信。

> **写这条断言的时候注意反过来验一次。** 一个"永远返回不可信"的实现能过 B 组
> 但会让 A 组挂——**两组必须同时绿**，只绿一组说明判据被写死了。

### C 组：信任反例——**校验通过，但不可作出处**

`fixtures/data-part/trust-negative/` 下的用例，**v1.12 起是六条**：`02`–`07`。
这一组是本文最容易被跳过、也最值钱的一组：**它们全都是合法载荷**，
schema 拦不住，只能靠业务判据。

> **`01-failed-step` 在 v1.12 离开了本组，改判到 B 组。** 它没有 `execution_status`，
> 而该字段已提级为 `required`，所以它不再是"合法载荷"——不满足本组的定义性质。
> 本组 `failed` 这一档由 `07-failed-step-with-status` 接手（同一份载荷，带着字段）。
> 详见下面「v1.9 的分法」表下方的 v1.12 更正。

| fixture | 期望的处置 |
| --- | --- |
| `07-failed-step-with-status` | 不可作出处。唯一一步 `success=false`，三布尔全"干净"；`execution_status=failed` 由服务端下发 |
| `02-partial-success` | 部分可用。成功那步的数据不得整段丢弃（丢了比现状更糟，§4.6.1） |
| `03-has-risk` | 不可作出处 |
| `04-degraded-history-replay` | 不可作出处。`state` 仍是 `completed`（§4.6.8），只能靠 `degraded` 区分 |
| `05-truncated` | 数据可用，但**禁止任何总量表述**（"共有 N 条""全部""一共"） |
| `06-no-data` | 与 `failed` **分开**。"查到了，答案是没有" ≠ "没查成" |

**`01-failed-step` 这一条被改过两次，v1.9 是第三次——把前两次的错都改掉。**

- **v1.6 说"这条用例应当是红的，标 `xfail`"** —— 错。这份 fixture 带着完整的
  `steps[]`，按 §4.6.7 的过渡判据业务结论今天就能判对，`xfail` 会 XPASS。
- **v1.7 改成"业务结论必过 + 判定来源标 `xfail(strict=True)`"** —— **还是错，而且错得更隐蔽**：
  那条断言的输入是这份 fixture，而 fixture **没有 `execution_status` 字段**，
  是一份冻结的载荷。服务端上线之后真实流量的 `status_source` 会变成 `contract`，
  **但这份 fixture 永远是 `derived`**。于是 `xfail(strict=True)` 永远不 XPASS，
  "摘掉它"这个条件**永远不成立**。两侧实现时都撞上了，服务端把它写进了反馈。

**根因（值得记住）：fixture 是"契约长什么样"的样本，不该同时承担
"实现做到哪一步"的进度标记。** 两件事绑在同一份文件上，就是这次撞车的原因。
一份冻结的载荷能证明判据对不对，永远证明不了服务端今天发了什么。

### v1.9 的分法：按**断言对象**分，不是按断言内容分

| 断言对象 | 用例 | 状态 | 断什么 |
| --- | --- | --- | --- |
| **fixture**（冻结载荷） | `dp-trust-01`（无 `execution_status`） | **必过** | ① 业务结论：不可作出处；② `status_source == "derived"`——**如实断言，这是正确期望，不是缺陷** |
| **fixture** | `dp-trust-07`（同载荷 + `execution_status`） | **必过** | ① 业务结论相同；② `status_source == "contract"`。证明**两条路径都通** |
| **自己真实产出的载荷** | 各侧自己写 | 服务端上线前 `xfail` | `status_source == "contract"`。服务端断言自己产出的 data part；调用方断言一次真实（或录制的）服务端响应 |

**进度标记只能挂在第三行**——只有活载荷会随实现改变。前两行是判据的正确性，
今天就该全绿，任何时候都不许放宽。

> ### ⚠️ v1.12 更正：上表第一行与下面那条关闭判据，在同一份文档里是打架的
>
> 第一行要求 `dp-trust-01` **保持没有** `execution_status`，用来覆盖推导路径
> （`status_source == "derived"`），还写着"任何时候都不许放宽"。
> 而下一节的关闭判据要求"给现存 fixture **补上**该字段"。
> **补了它，第一行那条覆盖就没了；不补，提级之后它就是非法载荷。**
> v1.9 写关闭判据时没注意到会撞掉自己刚设计的分组——两条都是 v1.9 同一版写的。
>
> **v1.12 的取舍：不补字段，把它改判到 B 组。**
> 它的职责从"缺字段会被静默推导"变成"缺字段会被挡住"，唯一的拒绝理由就是缺这个字段。
> 上表第一行因此作废：**提级之后不存在"合法但没有 `execution_status`"的载荷**，
> 它要断言的那条路径已经不是契约路径了。
>
> | 断言对象 | 用例 | 状态 | 断什么（v1.12 起） |
> | --- | --- | --- | --- |
> | **fixture**（冻结载荷） | `dp-trust-01`（无 `execution_status`） | **必过** | ① schema 校验**必须失败**，且理由就是缺这个字段；② 回落判定不可作出处 |
> | **fixture** | `dp-trust-07`（同载荷 + `execution_status`） | **必过** | ① 业务结论：不可作出处；② `status_source == "contract"` |
> | **自己真实产出的载荷** | 各侧自己写 | **必过**（两侧均已上线） | `status_source == "contract"` |
>
> **推导能力两侧都别删。** 作废的是"推导出来的结论可以照常采信"，不是推导本身——
> 不合规载荷（服务端 bug、版本错配、中间件吞字段）真的会出现，那时还得靠 `steps[]`
> 说清楚"查挂了"而不是崩掉。只是结论一律按不可信处置。详见契约 §4.6.7。

### 缺口关闭的机器判据（不靠任何一侧的 xfail）

`execution_status` 在 `schema/artifact-data-part.schema.json` 里由**可选提级为 `required`**
（走 §6 契约变更），同时给现存 fixture 补上该字段。**那一个契约仓库的提交就是关闭证据**,
`tools/check_contract.py` 能验，不依赖任何一侧的测试标记，也不会因为谁 rebase 而失效。

> **✅ v1.12 已达成。** `required` 里现在含 `execution_status`，
> `tools/check_contract.py` 断言这一点。「给现存 fixture 补上该字段」按上面的更正
> 执行为**十五份补、`dp-trust-01` 改判**，`dp-trust-07` 本来就带着。
>
> **另外守了一条 v1.11 就预判到的坑：** 多一条必填 = 每个既有反例都多一个被拒理由，
> 「这条反例真的在保护它声称保护的东西」会因此悄悄失效。六份反例都补了该字段，
> 实测各自仍只因原来那一个理由被拒；自检脚本现在断言——**除 `dp-trust-01` 外，
> 任何反例的拒绝理由里出现 `execution_status` 即失败**。

### D 组：`database` 标识符

**服务端：** 自己发出的每一个 `database` 都匹配 `schema/database-id.schema.json` 的
根 `pattern`（`^[a-z][a-z0-9_]*_db$`），且落在
`$defs["current-2026-09-04"]`（或当时生效的等价清单）里。

**调用方：** 中文映射表必须覆盖 `$defs["known"]` 的全部；
遇到清单外的标识符**不得崩溃**，退化为显示原始标识符并记一条日志。
这一条要有**正面用例**：造一个清单外的合法标识符（如 `hr_db`）走一遍展示路径，
断言它显示成原始值而不是抛异常——因为 v1.6 起**新增标识符不算契约变更**，
这个分支是常规路径，不是异常路径。

> **§4.5 在 v1.6 变了（原缺口已关）：** `database` 的契约面现在是**形状**
> （`^[a-z][a-z0-9_]*_db$`）+ 一份可拉取的生效清单，不再是钉死的枚举。
> 断言"实际发出什么"用 `current-*`；断言"调用方能认多少"用 `known`
> （它比 `current-*` 多两个退役标识符 `mes_db`/`pm_db`，因为 benchmark 夹具里还有）。
> **不要拿 `known` 去断言运行时会发出什么**——退役的那两个不会出现在任何部署里。

### E 组：progress 事件信封

`fixtures/progress-event/` 下八个用例，过 `schema/progress-event.schema.json`。

**服务端额外断言（fixture 覆盖不到，要用真实的一次流）：**

- 发出的 `stage` 全部落在 `schema/stage.schema.json` 的
  `$defs["emitted-2026-09-04"]` 里——**不是 `contract-declared`**。
  两者差一个 `normalization`（§4.5 缺口）。`normalization` 上线后改断言集合，
  改集合的那个提交就是"这条缺口关闭"的证据。
- `memory_context` 等内部 stage **不得**出现（§4.5）。
- 进度事件的 `metadata` **不得**携带 `route` / `confidence` / `tables` / `changed`——
  这些要到 stage 结束才有值，出现即说明把事后信息塞进了事前事件。

**调用方额外断言：**

- 未知 `stage` 必须被**忽略**而不是报错（§4.2「不要对事件条数做任何假设」的同源要求：
  服务端加一个 stage 不该让调用方挂）。
- `metadata` 缺 `step` / `database` 时不得抛异常（§4.3「必须容忍缺失"）。
- **`repair` 的 `step` 严格递增**；同一个 `step` 收到第二个 `repair` 事件按异常处理（§4.5）。

### F 组：事件顺序与终态

**终态信封（v1.13 起有 fixture）：** `fixtures/terminal-event/` 下五个用例，过 `schema/terminal-event.schema.json`。
三个正例是 `completed` / `failed` / `canceled`；两个反例分别是 `state: "cancelled"`（多一个 l——`metadata.error`
里取消原因的写法，不是取值）和终态 `final: false`。

**顺序类断言**仍然 fixture 覆盖不到（要一整条流），两侧各自用自己的测试替身构造，见下表。

**服务端额外断言（v1.13）：**

- 发出去的终态帧（`completed` / `failed` / `canceled` 三种都造一次）过 `schema/terminal-event.schema.json`；
- 经服务端取消接口取消一条还开着的流，终态是 `canceled`，`metadata.error` 形如 `cancelled: api`，之前不发 `artifact-update`；
- `message/send` 被取消时，返回的 Task `status.state` 是 `canceled`；
- 调用方断开后，run 在下一个检查点被取消（§5.4）；
- 限流、配额、流式准入满了：429 + JSON-RPC `-32029` + `Retry-After`，在流打开之前（§3.2 / §5.5）。

**调用方额外断言（v1.13）：**

- 流式终态 `canceled` → 判失败、回落（§5.1）；
- `message/send` 返回的 Task 是 `canceled` → 判失败、回落。

| 断言 | 出处 |
| --- | --- |
| 首帧是 `task` | §4.2 |
| 终态事件**有且只有一个**，且是最后一个 | §4.2 |
| `artifact-update` 在终态之前 | §4.2 |
| 流中途出错时**补一个 `failed` 终态**再关，不靠断连表示失败 | §5.2 |
| 终态是 `canceled` → **判失败**（v1.13） | §4.4 / §5.1 |
| 只收到 `artifact-update`、没收到终态 → **判失败** | §5.1 |
| 流正常结束但没有终态 → **判失败** | §5.1 |
| 响应 `Content-Type` 是 `application/json` → 按错误处理，**不进 SSE 解析循环** | §3.2 |
| 流打开**之前**的 `-32004` → 可以改发 `send`；流**中途**任何失败 → **不得**重发 | §5.4 / §6.2 |

> 最后一条是配额安全属性，不是功能属性。**它退化的时候不会有任何症状**——
> 只会在账单上多出一份。所以必须有用例。

## 怎么跑

契约仓库自己：

```bash
pip install jsonschema referencing
python3 tools/check_contract.py
```

两侧仓库：把本仓库作为 submodule 或在 CI 里 checkout 到固定 tag，
然后读 `fixtures/index.json`（`expect` = `valid` / `invalid`，`trust` = 期望的信任判定），
把 `file` 指向的载荷喂进自己的消费路径。**不要把 fixture 拷进自己仓库**——
拷贝就是副本，副本会漂，这份契约已经为此翻过一次车（附录 B）。

引用时钉在 tag 上，不要跟 `main`：

```bash
git clone --depth 1 --branch v1.13 https://github.com/heyemlee/a2a-contract.git .contract
```

## 这份文件没有覆盖什么

诚实列出来，免得被当成"跑完就都验过了"：

- **超时与预算（§1 / §5）** —— 需要计时和真实延迟，不适合放进单测。
  v1.6 把 45s 补成了合规的第二档，所以"代码跑契约没有的值"这个缺口已经关了；
  但**档位切换本身仍然没有任何自动检查**：没有东西在防"切到第三档时忘了抬外层
  工具超时"（外层 ≤ send 时外层先杀，改 send 完全无效）。这一条只能靠人对照，
  或者由调用方自己加一条启动断言：`外层工具超时 > send 预算`。
- **回滚演练（§6.2）** —— 要真的翻开关、要陈旧 Card，是实机演练不是 CI。
- **容量数字（§8.4）** —— 要压测。§8.4 里的两个 QPS 都是推算。
- **`as_of` / `currency` / `source_records`（§4.6.4–4.6.6）** —— 服务端还没有这些概念，
  schema 里是可选字段，**现在写用例只会验到空气**。等实现后补，
  补的时候把它们从可选提级为必填，那是一次 §6 契约变更。
