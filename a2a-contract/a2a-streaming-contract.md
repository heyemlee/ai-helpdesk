# A2A 流式契约：`message/stream`

> 状态：**v1.13：调用方断开后服务端在下一个检查点停下（§5.4），新增传输终态 `canceled`（§4.4），限流类 429 的错误码改为 `-32029`（§3.2 / §5.5）。** 七条缺口仍和 v1.12 一样全部关闭；判据仍是「提交可复核 + 本机按 CI 入口复现全绿」。
>
> **v1.13 是一次 §6 契约变更**：三项都改了我方承诺的行为，两侧都要跟着动 pin。
>
> | 项 | 契约 | 服务端（ai-sql-agent `5f1d154`） | 调用方（ai-helpdesk `5a1aa66`） |
> | --- | --- | --- | --- |
> | 断开即取消 | §5.4 改写；`message/stream`、`message/send`、`/query` 三个承载者一起写进来 | `AI_SQL_CANCEL_ON_DISCONNECT` 默认开；`tests/test_run_cancel.py::A2ACancelTest` | 无：断开之后它不再读任何东西 |
> | `canceled` 终态 | §4.2 / §4.4 / §5.1；新增 `schema/terminal-event.schema.json` 与 `fixtures/terminal-event/` 五个用例 | 流式终态和 send 返回的 Task 都发 `canceled`；`tests/test_contract_fixtures.py::TerminalFrameTest` | 不改代码（非 `completed` 一律回落）；补了 `canceled` 的消费用例 |
> | `-32029` | §3.2 / §5.5 写明码值 | 限流、配额、流式准入满了：429 + `-32029`；`tests/test_a2a_server.py` | 不改代码（它不按 `-32001` 分支） |
>
> **本机复现（2026-06-09；两侧 pin 指 v1.13，契约目录指向本版）：**
>
> - 契约 `tools/check_contract.py` 全过；
> - ai-sql-agent `pytest`：1235 passed、27 skipped。跳过的都要 `AI_SQL_PGVECTOR_DSN`；PostgreSQL 取消测试另外在 PostgreSQL 16 上跑过；
> - ai-helpdesk 后端：ruff 干净，`pytest` 1346 passed、30 skipped；接上本版契约后，`test_contract_fixtures.py` 和 `test_a2a_client.py` 98 passed。
>
> **GitHub CI：** ai-sql-agent 的 Actions 现在能跑了；ai-helpdesk 这次没有核对。
> 两侧随本版推送的那次 CI 结果写不进这一行：打 tag 必须是这一版的最后一个动作（`docs/maintenance.md`）。
>
> **v1.12 只做一件事：把 §4.4 欠的那一步补上。** v1.10 与 v1.11 都写着「七条**全部**
> 关闭」，那句话说过头了——§4.4 的关闭判据是 v1.9 特意挪到契约仓库这一侧的一个动作
> （`execution_status` 由可选提级为 `required`、现存 fixture 补齐该字段），
> 而那一步直到 v1.11 都没做。2026-05-06 先发了一版**只改文字**的更正，本版把动作做掉。
>
> 现在它在 `required` 里：
>
> ```bash
> python3 -c "import json;print(json.load(open('schema/artifact-data-part.schema.json'))['required'])"
> # ['queried_database', 'has_risk', 'degraded', 'execution_status', 'trace_id', 'route', 'steps']
> ```
>
> **这是一次 §6 契约变更**（必填字段是硬门禁），两侧都要跟着动，不是纯文字版。
> 十七份 data-part fixture 里补了十五份，`dp-trust-07` 本来就带着，
> **只有 `dp-trust-01` 故意不补**——它没有这个字段正是它现在的职责：
>
> **`dp-trust-01` 由 C 组改判到 B 组。** 提级之前它是 C 组核心用例（合法载荷，
> 靠 §4.6.7 的过渡判据从 `steps[]` 推导，`status_source == "derived"`）；提级之后
> 过渡判据作废，这份载荷**不再合法**，唯一的拒绝理由就是缺这个字段。它从
> 「缺字段会被静默推导」的样本，变成「缺字段会被挡住」的反例。**文件路径没动**——
> 两侧按字面路径读它，挪文件会红在别人仓库里。
>
> **那六个既有反例逐条重验过了。** 多一条必填会给每个反例多一个被拒理由，
> 「这条反例真的在保护它声称保护的东西」就会悄悄失效。所以六份都补了该字段，
> 实测各自仍只因原来那一个理由被拒；`tools/check_contract.py` 现在把这条守死了
> （除 `dp-trust-01` 外，任何反例的拒绝理由里出现 `execution_status` 即失败）。
>
> **v1.11 只做一件事：让「钉在 tag 上」这条规矩重新可执行。**
> 契约面——`schema/` 与全部 fixture 载荷——与 v1.10 **逐字节相同**
> （`git diff v1.10 v1.11 -- schema/ fixtures/` 只有 `index.json` 的版本号一行）。
> 差别只在 `fixtures/index.json` 的 `contract_version`：**tag `v1.10` 里它停在 `v1.9`**，
> 因为三笔「v1.10 补正」落在打完 tag 之后。而下游正是按这个字段判断
> 「我拉到的是不是我钉的那一版」（`ai-sql-agent/tests/test_contract_fixtures.py`
> 的 `test_contract_version_is_the_one_we_pinned`），所以**两侧钉 `v1.10` 会红**，
> 而 `v1.10` 已经推送、**不能移动**（附录 B：钉住的引用必须一直取得到当时那一份）。
> 于是补一个自洽的 tag，两侧的 pin 指到这里。**这不是一次 §6 契约变更**——
> 契约面没动，两侧无需改任何消费代码。
>
> 这一行改过三次口径，每次都是上一次说过头了：v1.2 只写「已实现」不写证据 →
> v1.3 核对 GitHub 后推翻 → v1.4 附证据表重新声明。**v1.5 是第三次收口，
> 而这次的病因是新的：不是"没有证据"，是"证据证的不是契约条款"。**
>
> v1.4 那张表证明的是**端到端链路能跑通**——发得出事件、收得到终态、前端画得出步骤。
> 这件事是真的，下面原样保留。但它不等于**条款逐条兑现**：链路跑通只要求事件流的
> *形状*对，条款兑现要求每一个数值、每一个字段、每一个状态映射都对。
> 2026-04-28 回到两侧代码逐条复核，下表这些条款没有兑现。
>
> **v1.10：七条缺口全部关闭——但判据是降级的，这一点必须一起读。**
>
> > **2026-05-06 更正：上面这句说过头了，应为「六条关闭，第七条实现已合入、契约侧待提级」。**
> > §4.4 那条的关闭判据是契约仓库自己的一个动作（`execution_status` 提级为 `required`），
> > 那一步当时没做。这句原文保留，是为了让「v1.10 当时是这么写的」可追溯——
> > 附录 A 的 v1.10 行同理，不回改。
> >
> > **✅ v1.12 已把那一步做掉**，七条现在是真的全关。经过见本文开头。
>
> 两侧的提交都推上去了，且**都已落到各自的默认分支**——ai-helpdesk `main`、
> ai-sql-agent 的 PR heyemlee/ai-sql-agent#1 已**合入 `main`**（合入后对应 `22060ee`），
> 本机按各自 CI 入口复现全绿：helpdesk **675 + 75 + 48 passed，生产构建成功**;
> sql-agent **`Ran 550 tests … OK`**（真实 PostgreSQL 16）;契约自检全过。
>
> **但原计划的判据「两侧 CI 在 GitHub 上绿一次」没有达成，而且一次都没发生过。**
> 三个仓库（含一个刚建刚推的 workflow）的 Actions 都是 `enabled: true`、workflow
> `active`、run 数 **0**——账号级问题，仓库里改不动。所以实际判据是
> **「提交可推送复核 + 本机复现全绿」**，它证明了检查内容是对的，
> **没证明检查会被强制执行**。详见状态行下方的降级声明与复现方法。
>
> **v1.9：改掉一个改过两次还是错的地方，并记录两侧都已提交。**
>
> `dp-trust-01` 的那条指示 v1.6 写错、v1.7 改了还是错——**v1.7 的错更隐蔽**：
> 它让「判定来源」的 `xfail(strict=True)` 断言一份**冻结的 fixture**，
> 而那份 fixture 里永远没有 `execution_status`，服务端上线也改不了它。
> 于是 xfail 永远不 XPASS，"摘掉它"这个条件**永远不成立**。两侧实现时都撞上了。
>
> 根因值得记住：**fixture 是"契约长什么样"的样本，不该同时承担"实现做到哪一步"
> 的进度标记。** v1.9 按**断言对象**重分——fixture 断判据（今天就该全绿）、
> 活载荷断进度（只有它会随实现改变），并把缺口关闭的判据挪成一个
> **机器可验的契约仓库动作**（`execution_status` 提级为 `required`），
> 不再依赖任何一侧的测试标记。新增 `dp-trust-07` 覆盖契约路径。
>
> 另澄清 `currency_source` / `status_source` 都是**调用方给自己结论贴的来源标签**，
> 不要求服务端回传——服务端不知道调用方的部署配了什么。
>
> **v1.8：契约一直漏掉了跑生产流量的那条路。**
> 调用方交回的两条反馈都成立，都改契约：
>
> 1. **直连 `POST /query` 从来不在契约里，可它是 `SQL_AGENT_TRANSPORT` 的默认值。**
>    契约叫「A2A 流式契约」，于是把非 A2A 的那条当成别人的事——而 §6.2 的回滚承诺
>    恰恰依赖它。已经造成一个静默缺陷：`/query` 把同一个对象序列化成 `step_logs`，
>    A2A 叫 `steps`，调用方取不到就按"缺失即不可信"处置，**每一次直连查询都不再是出处，
>    而链路看起来完全正常**。v1.8 把载荷与承载者分开（§4.4.1），三个承载者同一套判据。
> 2. **§4.6.5「不得回退到全局币种常量」这条禁令过头了。** 照它执行等于删掉那个
>    当初用来堵一次真实事故的常量，而删掉它并不会让中文默认写「元」这个行为消失,
>    只会让它变回隐式。已改为按来源三分——**禁止的是"把配置当成事实"，不是"使用配置"**。
>
> **v1.7：两侧照 v1.6 各自实现了一轮，回头暴露出契约自己的三个错。**
> 这一版先改契约的错，再记录两侧的进度——顺序不能反，因为两侧都是**撞上契约的错
> 之后各自绕开的**，而两侧绕法一致纯属运气好。三个错：
>
> 1. **§4.6.7 把「已要求但没送到」和「还没实现」混成一件事。** `execution_status`
>    今天标着 🔨，每一份线上载荷都缺它；照那张表逐字执行 → 所有查询判 `failed`
>    → **A 组正例全红**。一份自己过不了自家用例的规范，是规范写错了。已补过渡判据，
>    并把两侧不约而同发明的 `status_source` 收进契约。
> 2. **CONSUMER-TESTS 让 `dp-trust-01` 标 `xfail` 的指示是错的。** 那份 fixture 带着
>    完整 `steps[]`，业务结论今天就能判对，`xfail` 会 XPASS。已拆成「业务结论必过 +
>    判定来源 xfail(strict)」——缺口的实质不是"结论错"，是"结论由谁定义"。
> 3. **§4.5 的 stage 表被当成了顺序承诺**，而实际顺序是 `routing → normalization → …`。
>    已注明行序不表示顺序；顺带补上 `dp-bad-06` 一直缺的正文依据。
>
> **v1.6 不是第四次收口，是把 v1.5 挂起来的两个双边决定做掉：**
> §1③ 的 send 预算补上第三档（把代码现值 45s 扶正为契约值，并补上"外层工具超时
> 必须严格大于 send 预算"这条会让切档静默失效的联动）；§4.5 的 `database`
> 从钉死的枚举改成**形状 + 可拉取清单**，并更正名字——追下去发现旧表的
> `mes_db` 是 `prod_db` 的旧名、`pm_db` 的职责本就在 `erp_db`，
> 两个都**从未在任何部署里存在过**，而真实存在的 `svc_db` 契约里一直没有。
> 这两条的"性质"列因此从「要改」变成「已改」。**其余五条不变。**
>
> ### ⚠️ 判据降级的声明（v1.10）：**GitHub CI 一次都没跑过**
>
> > **✅ 已解除：三个仓库的 GitHub Actions 都在跑了**（`gh run list -R heyemlee/<仓库>` 可查）。这个框按「关闭时不删框」的规矩保留，
> > 下面的原文一字不改。
> >
> > **仍然成立的那一半：CI 只报告，不拦截。** 三个仓库的 `main` 都没有分支保护，ruleset 为空，
> > 提交直接推到 `main`，CI 在推送之后才跑——红了也已经在 `main` 上，打 tag 也不等它。
> > 所以下文「它证明了检查内容是对的，没证明检查会被强制执行」今天依然对；
> > 合 `main`、打 tag 之前在本机把 `python3 tools/check_contract.py` 跑到全绿，仍是唯一会拦人的那道门。
>
> > **2026-05-06 更正：下一段开头的「七条缺口在 v1.10 全部关闭」说过头了**，应为
> > 「六条关闭，第七条实现已合入、契约侧待提级」——§4.4 那条的关闭判据是契约仓库
> > 自己的一个动作（`execution_status` 提级为 `required`），当时没做。原文保留，
> > 理由同 §「v1.10 回顾」那处：让「v1.10 当时是这么写的」可追溯。
> > **本节其余部分不受影响**——判据降级讲的是「关的那些是怎么关的」，
> > 与关了几条是两件事。
> >
> > **✅ v1.12 已把 §4.4 那一步做掉**，条数更正到此结束。
> > **但判据降级这件事没变**：GitHub CI 至今一次没跑过，下面整段仍然有效，不许删。
>
> 七条缺口在 v1.10 全部关闭，但关闭判据**不是**原计划的「两侧 CI 在 GitHub 上绿一次」——
> 那件事至今**没有发生过一次**，而且不是这三个仓库的问题：
>
> | 仓库 | repo 级 Actions | workflow 注册 | 实际 run 数 |
> | --- | --- | --- | --- |
> | a2a-contract | `enabled: true` | `active`，`updated` = 推送当天 | **0** |
> | ai-sql-agent | `enabled: true` | `active` | **0** |
> | ai-helpdesk | `enabled: true` | `active` | **0** |
>
> 三个仓库、包含一个**刚建刚推**的 workflow，GitHub 读到了却一次没执行。
>
> **2026-05-05 用 `gh workflow run` 手动触发，拿到了确切原因：**
>
> ```
> HTTP 422: Actions has been disabled for this user.
> ```
>
> 是**账号级禁用**，不是仓库设置（三个仓库的 repo 级 Actions 都是 `enabled: true`）。
> 而且**不是计费问题**——三个仓库全是 public，public 仓库的 Actions 分钟数无限免费，
> 不存在超额停用。剩下的可能是 GitHub 侧对账号施加的限制，
> **没有自助开关，只能走 GitHub Support**。
>
> **所以实际采用的判据是：提交已推送（第三方可 fetch）+ 本机按各自 CI 入口复现全绿。**
> 这比原判据弱——**它证明了检查内容是对的，没证明检查会被强制执行**。
> 在 GitHub Actions 恢复之前，这些门禁拦不住任何人：不是 required check，
> 提交里带着红的东西也照样能合。**这一句不要在 CI 恢复之前删掉。**
>
> ### 怎么重新验一遍（v1.10 新增，复现这份判据）
>
> ```bash
> # 契约仓库
> pip install jsonschema referencing && python3 tools/check_contract.py
>
> # ai-helpdesk（脚本与 .github/workflows/ci.yml 一一对应）
> bash scripts/ci-local.sh
>
> # ai-sql-agent
> docker run -d --name pg -e POSTGRES_USER=ai_sql -e POSTGRES_PASSWORD=ai_sql \
>   -e POSTGRES_DB=ai_sql -p 5432:5432 pgvector/pgvector:pg16
> git clone --depth 1 --branch <本契约的 tag> <a2a-contract> .contract
> AI_SQL_PGVECTOR_DSN=postgresql://ai_sql:ai_sql@localhost:5432/ai_sql \
>   A2A_CONTRACT_DIR=$PWD/.contract python -m unittest discover -s tests
> ```
>
> **三个坑，踩过了记在这里**（2026-05-05 实测）：
>
> 1. **ai-sql-agent 必须在没有 `.env` 的环境里跑。** 那个仓库的「与本机配置隔离」
>    写在 `tests/conftest.py`，是 **pytest fixture**——而 CI 入口是
>    `python -m unittest discover`，**unittest 不加载 conftest**。本机有 `.env` 时
>    实测 **29 failures + 3 errors**，断言变成了"你的 `.env` 配了什么"。
>    GitHub Actions 上没有 `.env` 所以碰不到。
> 2. **PostgreSQL 必须映射到宿主机 5432。** `tests/test_ops_readonly.py` 构造只读角色
>    DSN 时端口是写死的，换端口那两条连不上。
> 3. **契约 tag 要和被测仓库钉的那个一致。** ai-sql-agent 的
>    `A2A_CONTRACT_TAG` 目前是 `v1.8`；给它 v1.9 时**只有版本断言那一条失败，
>    其余全过**（包括 v1.9 新增的 `dp-trust-07`）——说明消费端已兼容，只差改 pin。
>
> ### 仍然成立的部分：链路证据（v1.4 的表，口径改为「链路」）
>
> | 项 | 位置 | 怎么验 |
> | --- | --- | --- |
> | 服务端 `message/send` / `message/stream` | `ai-sql-agent` 的 `ai_sql_a2a/server.py` | `cd ai-sql-agent && .venv/bin/python -m pytest tests/test_a2a_server.py -q` → 2026-04-28 实跑 **12 passed** |
> | 进度事件来源 | `ai_sql_core/trace.py` 的 `stage()` 事件汇 | 正是 §8.2 挑的那个钩子，pipeline 侧零改动（消费点在 `ai_sql_a2a/server.py:295-298` 的 `sink`） |
> | 调用方 client | `ai-helpdesk` 的 `backend/tools/a2a_client.py` | `backend/tests/test_a2a_client.py` |
> | 调用方 `-32004` 降级 | `ai-helpdesk/backend/tools/sql_agent_client.py:251-258` | 单测 + 实机演练（见 §6.2） |
> | 前端进度展示 | `frontend/src/components/MessageBubble.tsx` | `frontend/src/components/__tests__/MessageBubble.test.tsx` |
>
> 端到端实测（客服 → A2A → 模拟 ERP，2026-04-27）：
>
> ```
> [ 2.6s] 正在判断问题类型      [21.3s] 正在生成查询（ERP 订单库）
> [ 7.8s] 正在检索知识库        [24.9s] 正在查询（ERP 订单库）
> [ 7.8s] 正在规划查询步骤      [31.5s] meta → 38.5s 第一个字 → 41.5s 完成
> ```
>
> **§2 那段「委派期间零字节」的空白确实填上了**——服务端侧相邻事件最长间隔 9.5s，
> 远低于 §1 的 20s 静默阈值。
>
> > 连带补一件 v1.4 没说的事：**A2A 还不是调用方的默认路径。**
> > `SQL_AGENT_TRANSPORT` 默认仍是 `query`（`ai-helpdesk/backend/tools/sql_agent_client.py:152`），
> > 上面那次实测是显式切到 `a2a` 跑的。按 §1「上线顺序」的判据，
> > **流式尚未"上线并承接流量"**——这不是缺陷，但它决定了下表第一行该怎么读。
>
> ### 未兑现的条款（v1.5 新增，逐条对应小节编号）
>
> 每一条都在正文对应小节里就地标了 **⚠️ 缺口**，两处必须同时改。
>
> **改法（2026-05-06 更正）：关闭时不删框，在框顶补一行「✅ 已关闭 + 证据」。**
> 原文写的是「改完两处一起删」，而 v1.10 关闭七条时**只改了本表，正文四个框一个没动**，
> 两处就此对不上，直到 2026-05-06 才发现。「一起删」既没人真去执行，
> 删了也让「这里曾经是个缺口」不可追溯。留框加标记，两件事都解决。
> 「性质」列区分两种完全不同的处置：**实现待办** = 契约的设计意图保留、代码要追上；
> **契约要改** = 代码没错，是契约把事实写错了或写得过于绝对。
>
> | 条款 | 契约怎么写 | 代码实际是什么 | 证据（2026-04-28 复核） | 性质 |
> | --- | --- | --- | --- | --- |
> | §1③ / §5.1 | `message/send` 固定预算 **90s**（上线前 25s / 上线后 90s 两档） | 默认 **45s**——两档里都没有这个值 | `ai-helpdesk/backend/tools/a2a_client.py:80`（`send_timeout_s: float = 45.0`）、`:273-274`（env 默认 `"45"`，注释自承「上线后改 90」，没改） | ✅ **契约已改（v1.6）**：补第三档「已实现未承接流量 = 45s」，代码现值即合规值，**无需改代码**。新增待办：切档时外层工具超时（今天 47s）必须与 send 一起动 |
> | §4.4 → §4.6 | `failed` = 任一步骤失败 / 执行出错 / 被拒 | **只有 pipeline 抛异常才发 `failed`**；查询全挂照发 `completed`，且 `queried_database` 也是 `true`——**调用方没有任何字段能发现查挂了** | `ai_sql_a2a/server.py:277`、`:317`；`grep -n failed ai_sql_a2a/server.py` 全文只有这两处。实测与改判理由见 §4.4 缺口框与 §4.6.8 | **契约已改（v1.5）+ 实现已就位待提交（v1.7）**：ai-sql-agent 已实现 `execution_status` 与三个汇总数（`ai_sql_api/result_summary.py` 的 `compute_execution_status`，`/query` 与 A2A 共用同一个 `data_part()`），ai-helpdesk 已按 §4.6.7 过渡判据消费（`backend/core/evidence.py`）。🔸 **实现已合入（v1.10），契约侧待提级（2026-05-06 更正）**：ai-helpdesk `main` = `e594655`（17 笔，已推送）；ai-sql-agent 的 12 笔已随 PR heyemlee/ai-sql-agent#1 **合入 `main`**（合入后对应 `22060ee`）。本机按各自 CI 入口复现：helpdesk 后端 **675 passed / 2 skipped / 1 xfailed** + 契约用例 **75 passed / 1 xfailed**；sql-agent **`Ran 550 tests … OK`**（真实 PostgreSQL 16）。复现方法见下方「怎么重新验一遍」。✅ **已关闭（v1.12）**：v1.9 给这条定的机器判据——`execution_status` 由可选提级为 `required`、现存 fixture 补齐该字段——已达成，**改 `required` 那一行的提交就是关闭证据**，`tools/check_contract.py` 现在断言它在 `required` 里。十五份 fixture 补了字段，`dp-trust-01` 故意不补并由 C 组改判 B 组（它现在是「缺字段会被挡住」的反例），六个既有反例逐条重验过拒绝理由没被污染 |
> | §4.5 | 九个 stage **全发**（§9 #3 答复"全发"） | 发八个，**`normalization` 不在映射表里** | `ai_sql_a2a/server.py:41-50`（`_STAGE_MAP` 八条）、`:40`（注释自承「契约 §8.2 待办」）；根因 `ai_sql_pipeline/text2sql_pipeline.py:245-257` 仍是裸 `log_event("stage.normalization")` 且只在 `changed` 时记 | **实现已就位待提交（v1.7）**：`_STAGE_MAP` 已九条齐全，`text2sql_pipeline.py` 已改真 `stage()`。✅ **已关闭（v1.10）**：`8340323`（已随 PR #1 合入 `main`）。`schema/stage.schema.json` 的 `emitted-*` 已更新为九个——**改那个集合的这一次提交就是关闭证据**，`tools/check_contract.py` 现在断言实发集合等于 `contract-declared` |
> | §4.5 | `database` 钉死四个：`erp_db`/`mes_db`/`pm_db`/`wms_db`，"不会在非契约变更中改名或新增" | 标识符集合**随部署配置走**。当前部署是 `erp_db`/`prod_db`/`svc_db`/`wms_db`；`mes_db`/`pm_db` 在运行时根本不存在 | `ai-sql-agent/.env:58` → `AI_SQL_SEMANTIC_DIR=sim_erp/semantic`；`ls sim_erp/semantic/` = `erp_db/prod_db/svc_db/wms_db.yaml`；`mes_db`/`pm_db` 只出现在 benchmark 数据集（`benchmark/runner.py:246`）。调用方已单方面加了两个标签：`ai-helpdesk/backend/tools/a2a_client.py:55-56` | ✅ **契约已改（v1.6）**：契约面降为形状 + 可拉取清单；`mes_db`→`prod_db` 改名、`pm_db` 删除（职责在 `erp_db`）、`svc_db` 补入。调用方那两个标签**追认为合规**，不再欠一次变更 |
> | §7 | `tasks/get` "多副本下会大概率 `-32001`" | **恒返回 `-32004`**，与副本数无关；服务端根本没有任务表 | `ai_sql_a2a/server.py:359-362`（`tasks/get` 与 `cancel`/`resubscribe` 同列在不支持表里）；`grep -n '_TASKS\|task_store' ai_sql_a2a/server.py` 无命中 | **契约要改** |
> | §7.1 | "我方结果在它那边的去向是 `grounding_sources`，**不是 prompt 正文**（已查实）" | **两个都进。** 同一段文本先进 prompt 上下文，再按开关进 `grounding_sources` | `ai-helpdesk/backend/api/main.py:676-678`（`order_text` 进 `context_parts` → `full_context`）、`:698-700`（同一个 `order_text` 再进 `grounding_sources`） | **契约要改**（结论待重新论证，见 §7.1） |
> | §9.1 ① | 三个布尔是信任门槛：`queried_database==True` 且 `has_risk==False` 且 `degraded==False` | **(a)** 缺字段一律 `False`——`has_risk`/`degraded` 缺失即"无风险、未降级"（fail-open）；**(b)** `bool()` 不校验类型，`"false"` → `True`；**(c)** 真正的门槛根本不是三布尔与：只有 `queried_database` 是闸 | `ai-helpdesk/backend/tools/a2a_client.py:250-266`（`_normalize`）；门槛在 `backend/api/main.py:1041`（唯一的 `return`-空分支）、`:1062`、`:1067`（`has_risk`/`degraded` 只追加一段提示文本，**照样返回 `True` 进 sources**）。复现见 §9.1 缺口框 | **实现已就位待提交（v1.7）**：ai-helpdesk 新增 `backend/core/evidence.py`——`strict_bool()` 只认 JSON 布尔（治 b），`assess()` 按 §4.6.7 缺失即不可信（治 a），`main.py` 的闸改为风险/降级/缺字段一律 `is_evidence=False` 不进 `grounding_sources`（治 c），两条 transport 共用同一个 `assess()`。✅ **已关闭（v1.10）**：`40be899`（已随 `main` 推送至 `e594655`）。本机复现：契约 B 组（缺字段 / 字符串布尔）与 C 组（形状合法但不可作出处）**75 passed / 1 xfailed** |
>
> 另有三处**措辞与治理**问题，同版一并改（§7 / §8.1 / §8.4 / 附录 B）：
>
> | 位置 | 问题 | 证据 |
> | --- | --- | --- |
> | §8.1 | 表里三段引文**从未进过任何提交**——v1.3 已指出，v1.4 没处理，条目还挂着 | `cd ai-sql-agent && git log --oneline -S"秒级同步"` / `-S"seconds-scale"` 均为空；两个 README 现在 `stream`/`A2A` 一次都不出现（`grep -ci stream README.md` → 0） |
> | §8.4 | 引用了一个不存在的文件 `ai_sql_core/shared_state.py`；`2.3 QPS` 是推算不是实测 | `find ai-sql-agent -name 'shared_state*'` 无命中；真身在 **另一个仓库**：`ai-helpdesk/backend/core/shared_state.py:22` |
> | 附录 B | 要求"引用时带 commit"，但仓库**零 tag**，两侧引用的还是旧版本 | `git tag -l` 空；`ai-sql-agent/ai_sql_a2a/__init__.py:4` 写「v1.3」、`ai-helpdesk/backend/tools/a2a_client.py:4` 写「v1.3」、`sql_agent_client.py:9` 带的 commit 是 `9081030`（= v1.2） |
>
> **这一版只改文档陈述，让文档与现实对齐；没有为了"让契约成立"去动另外两个仓库的任何一行代码。**
> 建议怎么改写在各缺口框里，落地是双边事件，由两侧各自排期。
>
> 本文的"我方"= ai-sql-agent（A2A 服务端），"调用方"= ai-helpdesk（A2A 客户端）。
>
> **位置**：本文是**唯一权威副本**，放在两个仓库之外的独立版本库里
> （`a2a-contract`）。两侧都不再各存一份——契约同时约束双方，留在任何一侧
> 都会变成"某一方的文档"，改起来就有主客之分。两边引用本文时带上 commit。

---

## 1. 前提：调用方那边已经成立的事实

> **这三条的身世**：v1.0–v1.2 写作「调用方已经做完并验证过的事实」，v1.3 核对后
> 发现 ①② 不成立、③ 只成立一半，降级为**前置条件**。v1.4 里它们**真的成立了**
> ——是照着这份契约补出来的，不是一开始就有的。

| # | 前提 | 状态（2026-04-27） | 对契约的约束 |
| --- | --- | --- | --- |
| ① | 调用方有 SSE 心跳 | ✅ 准备阶段静默满 15s 发一条 `: ping`（`_pump_until_done`） | **我方不发心跳、不发空事件**。见 §3.4 |
| ② | 调用方有 progress 事件通道，形态是「阶段标签 + 可选文案」 | ✅ `/chat/stream` 新增 `progress` 事件，前端铺成步骤列表 | 我方事件必须能直接映射过去，不要求它重写。见 §4.5 |
| ③ | 消费端是 `httpx.AsyncClient`，**超时按路径分档**，任何失败静默回落 | ✅ 流式静默 20s / 硬顶 90s，send 固定预算无静默规则 | 不引入新依赖/新协议栈；中途失败必须能干净回落。预算口径见下 |

**调用方超时策略（v1.2：按路径分档，取代 v1.1 的"两条路共用一套自适应"）：**

两条路的可观测性根本不同，不能共用一套判据：streaming 上我方持续发进度事件，
**静默本身就是异常信号**；`message/send` 上我方**按设计全程零事件**，
**静默是正常形态**。

**A. `message/stream`（流式）——自适应：**

| 规则 | 值 | 对我方的含义 |
| --- | --- | --- |
| 收到任何事件就续命 | — | **进度事件本身就是续命信号**，这是流式在这里的第二个作用 |
| 静默超过这么久就放弃 | **20s** | 我方相邻两个 stage 的间隔必须远小于 20s |
| 总时长硬上限 | **90s** | 超过就放弃，无论有没有事件 |

**B. `message/send`（非流式）——固定预算：**

| 规则 | 值 | 说明 |
| --- | --- | --- |
| 固定总预算 | **今天 45s，终值 90s** | **无静默规则。** 零事件是这条路的正常形态，不是故障信号。这个值按下方「上线顺序」的三个档位切换，**不是一个常数**——终值 90s 与 streaming 的硬上限对齐 |

> **为什么 send 不能套用"静默 20s"。** send 全程不发任何事件——这是它的设计
> （§6.1），不是缺陷。把静默规则套上去，等于给它一个 **20s 的硬预算**：
> 比它原先的固定 25s **更严**，而实测 p90 就是 17.7s、max 68.3s（§2）。
>
> 后果不是"稍微紧一点"。`message/send` 是 §6.1 **永久保留**的路径，
> 也是 §6.2 **回滚的落点**——套用静默规则会让回滚变成**静默降级**：
> 关掉流式开关之后，大量本来正常的查询会在 20s 被判死回落。
> 一条用来兜底的路径，不能比它兜的那条路更容易死。
>
> 所以 send 只有一个固定总预算（终值 90s，与 streaming 的硬上限对齐），不设静默阈值。
> 至于今天该取哪个值，见下方「上线顺序」——**放宽的前提是用户能看到进度**，
> 这一条与"不能比它兜的那条路更容易死"同时成立，三个档位就是这两条的交集。

拍板理由：宁可让用户多等，也不要静默烧配额（§5.4）。

> ### 上线顺序（硬要求）：先能看到进度，再放宽超时
>
> 上面的 send 预算**不是一个值，是随阶段切换的档位**。在用户看不到任何进度的
> 前提下放宽它，只是把今天的"25s 空白屏"换成"90s 空白屏"，**比现状更糟**。
>
> 档位有三个，不是两个——v1.6 补上了中间那一档（理由见下方「已决」框）。
>
> | # | 阶段 | 判据 | send 预算 | 调用方外层工具超时 |
> | --- | --- | --- | --- | --- |
> | 一 | 流式未实现 | 服务端没有 `message/stream` | 固定 **25s** | > 25s |
> | 二 | **已实现、尚未承接流量** | 有实现，但 `SQL_AGENT_TRANSPORT` 仍是 `query`（或开关未开） | 固定 **45s** | > 45s（今天是 47s ✅） |
> | 三 | 已承接流量 | 生产流量真的走 `a2a` + 流式 | 固定 **90s** | **必须一起抬到 > 90s** |
>
> 判据是**服务端流式真的上线并承接流量**，不是"服务端实现完了"——
> 实现完成 ≠ 上线，中间还隔着部署。**今天是第二档**
> （ai-helpdesk `39f9b89`：`backend/app/tools/sql_agent_client.py:188`，`SQL_AGENT_TRANSPORT` 默认 `query`）。
>
> #### 外层工具超时必须严格大于 send 预算（v1.6 新增）
>
> 上表最后一列是 v1.6 补的，因为它是一个**会让切档静默失效**的坑：
> 调用方在 A2A 客户端之外还有一层工具超时（ai-helpdesk `39f9b89`：`backend/app/api/main.py:428`，
> 算式在 `backend/app/core/preflight.py:193-195`：`SQL_AGENT_TIMEOUT_S` + 2s，今天 **47s**）。
> 两个值的关系决定了超时由谁先判：
>
> - **外层 > send**：send 先超时，抛出的是 A2A 的错误，调用方知道是"下游没答完"，
>   可以按 §5 回落。今天 47 > 45，顺序是对的。
> - **外层 ≤ send**：外层先杀，send 的预算等于没生效——**改 send 的值不会有任何效果**。
>
> 所以第三档那一行的"必须一起抬"不是建议：**只把 send 改成 90s 而外层还是 47s，
> 等于把 send 预算悄悄钉死在 47s。** 切档是两个数一起动，不是一个。
>
> #### ✅ 已决（v1.6）：补上第三档，把代码现值扶正为契约值
>
> **v1.5 把这条记为缺口并列了两个选项（补档 / 改回 25s），v1.6 选了补档。
> 上表已是三档，下面保留当时的分析，因为它就是这个决定的理由。**
>
> `send_timeout_s` 默认 **45s**（ai-helpdesk `39f9b89`：`backend/app/tools/a2a_client.py:96`；
> env `SQL_AGENT_A2A_SEND_TIMEOUT_S` 默认也是 `"45"`，见 `:290-291`，
> 注释自承「上线后改 90」——没改）。**45 既不是上线前该有的 25，也不是上线后该有的 90。**
>
> 两件事要分开看，不要笼统说成"违约"：
>
> 1. **上线判据这一侧，契约对、代码也没违约。** `SQL_AGENT_TRANSPORT` 默认仍是 `query`
>    （ai-helpdesk `39f9b89`：`backend/app/tools/sql_agent_client.py:188`），流式还没承接流量，
>    所以现在**本来就不该**是 90s。
> 2. **但上表说这个阶段是 25s，代码是 45s。** 差额不是笔误：25s 是旧值，
>    45s 是实现时按 p90 17.7s（§2）自己放宽的。真正缺的是一档状态——
>    **「已实现、尚未承接流量」**。这一档 v1.2 写上线顺序时还不存在，v1.4 才第一次出现。
>
> **决定（v1.6）：补档，不是把代码改回 25s。** 三条理由：
>
> 1. **45s 不是拍脑袋。** 它是按 p90 17.7s（§2）放宽出来的；25s 对 p90 17.7s
>    只有 1.4× 余量，实测 max 68.3s 更是远在其外——25s 会砍掉大量正常完成的查询。
> 2. **那一档状态是真实存在的。** 「已实现、尚未承接流量」不是borderline 情形，
>    它是今天的状态，而且流式从实现到承接流量之间必然要经过它。
>    v1.2 写上线顺序时这一档还不存在，所以漏了；漏的是档，不是值。
> 3. **直接跳到 90s 会违反本节自己的硬要求。** 今天默认 transport 是 `query`，
>    用户看不到任何进度；在零反馈下把等待从 45s 放宽到 90s，正是本节开头
>    "把 25s 空白屏换成 90s 空白屏"要防的事。
>
> **仍然受约束的部分：** 切到第三档（90s）依旧要满足"流式真的承接流量"，
> 不因补档而放宽；且必须与外层工具超时一起动（见上）。
>
> 代码侧因此**无需改动** `send_timeout_s=45.0`——它现在是合规值。
> 待办从"改代码"变成"切档时记得两个数一起改"，落在 §8.2。

调用方当前实测时序（委派给我方的那段是静默期）：

```
[0.00s] event: progress   ← 检索知识库
[0.40s] event: progress   ← 正在查 ERP 订单库
[1.40s] : ping            ┐
[2.40s] : ping            ├ 委派给我方的静默期，今天这里是零字节
[3.40s] : ping            ┘
[3.90s] event: meta / event: routing
[6.10s] event: delta / event: result
```

> **这段时序是设计稿，不是实测。** 图里的 `: ping` 和 `event: progress` 都还
> 不存在（前提①②未成立），今天的真实形态是：从提问到出答案，中间**整段**
> 都是零字节，不只是委派的那几秒。

**本契约要填的就是那段静默期。** 不是延长预算，是把预算填满——在新口径下更进一步：填满预算是这条链路能活下来的**前提**（§2）。

---

## 2. 为什么值得做（我方这侧的数据）

审计库里 82 次真实模型调用（非 Mock）的实测：

| 路由 | 次数 | 平均耗时 |
| --- | --- | --- |
| `database_query`（查库） | 7 | **17.7s** |
| `data_qa`（对话） | 75 | 8.6s |

全量 p50 6.6s / p90 17.7s / max 68.3s。

也就是说：**查库路径的典型耗时是 17.7s，而这段时间调用方那边是零字节。** 这是两边加起来最值得补的一处。

在新口径（**流式**：静默 20s 判死 / 总长 90s 封顶）下：

- **实测全部落在预算内**——max 68.3s < 90s。原先固定 25s 时"有相当比例的查询本来就超时"这条**已经不成立**，降级为极端长尾才会命中；
- 但 **17.7s 的典型耗时远超 20s 静默阈值的量级**，所以流式不是锦上添花：没有进度事件，一次正常的查库查询就会因为静默被判死。**进度事件现在是这条链路的存活条件，不只是体验优化。**

这是取消固定 25s 之后，流式变得更重要而不是更不重要的原因。

---

## 3. 传输层

### 3.1 端点与请求

沿用现有端点，只增加一个 method。**不新开路径、不新开端口、不换协议。**

```http
POST /a2a HTTP/1.1
Content-Type: application/json
Accept: text/event-stream
X-API-Key: <同 message/send 的那把 key>
X-Trace-Id: <可选，调用方的链路 ID>

{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "message/stream",
  "params": {
    "message": {
      "kind": "message",
      "role": "user",
      "messageId": "...",
      "contextId": "<可选，会话 ID>",
      "parts": [{"kind": "text", "text": "SO-2024-001 现在什么状态"}],
      "metadata": {"user_id": "<可选>"}
    }
  }
}
```

`params.message` 的解析规则与 `message/send` **完全一致**（`parts` 里的 text 拼接、`contextId` 当会话、`metadata.user_id` 当身份），此处不重复定义。

成功时响应：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Trace-Id: <本次链路 ID>
```

httpx 侧就是 `client.stream("POST", url, json=..., headers=...)` 然后 `aiter_lines()`，不需要任何新依赖。

### 3.2 鉴权

**与 `message/send` 同一把 key、同一套拒绝逻辑**（`require_api_key`）。A2A 不是旁路，这条不变。

关键约定：**鉴权失败、限流、参数错误一律发生在流打开之前**，返回普通 JSON 响应，不是 SSE：

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"jsonrpc":"2.0","id":"req-1","error":{"code":-32600,"message":"..."}}
```

> **调用方要注意的一点：** 拿到响应后先看 `Content-Type`，是 `application/json` 就按错误处理并回落，不要直接进 SSE 解析循环。这是刻意设计——你不该为了知道"我没通过鉴权"而去解析一个事件流。

限流（429）同理，见 §5.5。

错误码（v1.13 写明）：

| 情况 | HTTP | JSON-RPC `error.code` |
| --- | --- | --- |
| 鉴权失败（缺 key / key 无效 / 无权限） | 401 / 403 | `-32600` |
| 限流、每日配额、流式并发准入满了 | 429（带 `Retry-After`） | **`-32029`** |
| 流式开关关着、`tasks/*` | 200 | `-32004`（§6.2 / §7） |

> **v1.13 把 429 的码从 `-32001` 改成了 `-32029`。** `-32001` 在 A2A 0.2 规范里是
> TaskNotFoundError，§7 自己也按这个意思用它（「`-32001`（任务不存在）值得重试或降级」）。
> 一旦支持任何 `tasks/*` 方法，同一个码就会有两种意思。`-32029` 落在服务端自定义的
> `-32000..-32099` 区间，并避开规范已经占用的 `-32001..-32007`。
> 调用方不按 `-32001` 分支（它只特判流打开之前的 `-32004`），所以这次改码不用动调用方代码。

### 3.3 trace_id

trace_id 在流式下走三个地方，值完全相同：

| 位置 | 说明 |
| --- | --- |
| 请求头 `X-Trace-Id` | 调用方可透传自己的链路 ID，我方原样沿用 |
| 响应头 `X-Trace-Id` | SSE 的响应头在正文之前发出，所以这个头照常可用 |
| **每一个事件的 `metadata.trace_id`** | 新增。见下 |

第三条是为流式专门加的：SSE 消费循环里通常只留 payload、不留响应头，把 trace_id 放进每个事件，调用方随手记一条日志就能和我方服务端日志对上，不用回头翻 response 对象。

排查时：拿这个 trace_id 到我方日志里检索，能取到完整的 stage 时序、SQL、命中表、token 用量。

### 3.4 不做心跳（明确）

**我方不发 `: ping`、不发空事件、不发任何无语义的保活帧。**

理由，按事实①：

1. 调用方已有 15s 心跳，存活性在**它对它自己客户端**那一段已经解决；
2. **调用方的判死条件是"静默 20s"，不是"连接活了多久"。** 保活帧确实能续命，但那正是不该做的事——保活帧续的是"连接还在"，而调用方要判断的是"对面还在推进"。用无语义的帧去骗过一个存活判定，等于把 20s 静默阈值变成摆设：真卡死时它会一直续命到 90s 硬上限才放弃；
3. 进度事件本身就是续命信号，而且是**有语义**的那种。查库路径上相邻两个 stage 的最大间隔远小于 20s（最长的一段是 planning→generation 的模型调用，量级在秒），所以正常查询不会撞到静默阈值；真撞上了，那就是真卡住了，**应该**让它判死。

所以保活在这条链路上不只是重复发明，**还会主动削弱调用方的故障检测**。契约里不定义 keep-alive，调用方也不要为它写解析分支。

> 换句话说：我方"没话说"这件事本身就是信息。把它填成 ping 就是把信息删掉。

---

## 4. 事件

### 4.1 信封

每个 SSE `data:` 承载一个完整的 JSON-RPC 2.0 响应，`id` 与请求一致（A2A 规范形态）：

```
data: {"jsonrpc":"2.0","id":"req-1","result":{ ...事件... }}

```

事件按 `result.kind` 分三类：`task`（首帧快照）、`status-update`（进度与终态）、`artifact-update`（结果载荷）。

### 4.2 事件顺序（**保证**）

```
1×  task              首帧：taskId / contextId，state=submitted
N×  status-update     进度，final=false，state=working
1×  artifact-update   结果载荷，lastChunk=true
1×  status-update     终态，final=true，state=completed|failed|canceled
```

（`canceled` 是 v1.13 新增的，取值见 §4.4。没有载荷的终态——`failed`、`canceled`——之前不发 `artifact-update`。）

保证的部分：
- 首帧一定是 `task`；
- 终态事件一定是最后一个，且**有且只有一个**；
- `artifact-update` 一定在终态事件之前；
- 进度事件之间保持 pipeline 的真实先后顺序。

不保证的部分：进度事件的**数量和种类**。同一个问题两次跑，走没走短路径、修没修 SQL，进度事件都会不一样。**调用方不要对事件条数做任何假设，尤其不要用条数判断进度百分比。**

### 4.3 进度事件

```json
{
  "kind": "status-update",
  "taskId": "…",
  "contextId": "…",
  "final": false,
  "status": {
    "state": "working",
    "timestamp": "2026-08-25T12:00:00.400Z",
    "message": {
      "kind": "message", "role": "agent", "messageId": "…",
      "taskId": "…", "contextId": "…",
      "parts": [{"kind": "text", "text": "正在查 ERP 订单库"}]
    }
  },
  "metadata": {
    "stage": "execution",
    "step": 1,
    "database": "erp_db",
    "trace_id": "e620e55f2e294505"
  }
}
```

对应事实②要的三样东西：

| 调用方需要 | 在哪 |
| --- | --- |
| 稳定的机器可读 stage 标识 | `metadata.stage` ← **这是契约面，见 §4.5** |
| 可选的人类可读描述 | `status.message.parts[0].text` |
| 是否为终态 | `final`（进度事件恒为 `false`） |

`metadata` 放在**事件根**而不是 `status.message.metadata` 里：调用方读一跳就拿到，不用 `status → message → metadata` 走三跳。这是 A2A 允许的扩展位。

`metadata` 里除 `stage`/`trace_id` 外都是可选字段，**调用方必须容忍缺失**（例如 `step`/`database` 只在按步骤重复的 stage 上出现）。

### 4.4 结果载荷与终态

`artifact-update` 的 `artifact` 与今天 `message/send` 返回的 Task 里那个 **完全同构**——文本 part（答案）+ data part（`route` / `confidence` / `queried_database` / `has_risk` / `degraded` / `degrade_reason` / `selected_databases` / `trace_id` / `steps[]`）。

**这是刻意的：调用方走 send 还是走 stream，能做的信任判断必须一模一样。** 不存在"流式拿到的信息更少/更多"。


终态事件：

```json
{
  "kind": "status-update", "taskId": "…", "contextId": "…",
  "final": true,
  "status": {"state": "completed", "timestamp": "…"},
  "metadata": {"trace_id": "…"}
}
```

`state` 是**传输终态**，只回答"载荷有没有完整送达"，**不回答"查询成不成功"**
（v1.5 改写，理由见 §4.6.8）：

| state | 含义（传输终态） | 什么时候发 |
| --- | --- | --- |
| `completed` | **载荷完整送达**：artifact 已发出，pipeline 正常返回。**不表示查询成功** | pipeline 未抛异常地跑完 |
| `failed` | **没有可用载荷**：pipeline 抛异常、准入被拒、流中途出错（§5.2） | 只有这些情况 |
| `canceled` | **没有可用载荷**：流打开期间，任务被**本连接以外的途径**取消（v1.13 新增） | 服务端取消接口、运维取消、服务关停。**本连接自己断开时不发**——这时已经发不出去（断开之后我方怎么做见 §5.4） |

> **`canceled` 的几点约定（v1.13）：**
>
> - 拼写跟 A2A 0.2 规范的 TaskState 走，是 `canceled`，不是 `cancelled`。取值本来就在规范里，不越出 §10 的 0.2 子集。
> - `metadata.error` 照带，说明是谁取消的，形如 `cancelled: api`。这里的 `cancelled:` 是取消原因的写法，不是 `state` 的取值。
> - v1.13 之前，同样的情况发的是 `failed` + `metadata.error = "cancelled: …"`。那在 v1.12 下合法，现在改发 `canceled`。
> - `message/send` 返回的 Task，`status.state` 用同一套三个取值。
> - 形状由 `schema/terminal-event.schema.json` 约束（v1.13 新增；v1.12 之前终态没有 schema，只靠两侧各自的测试守着），样本在 `fixtures/terminal-event/`。

> v1.0–v1.4 这张表写的是「`completed` = 所有步骤成功 / `failed` = 任一步骤失败」，
> 那是**业务**终态的定义，被放进了一个**传输**字段。代码从来不是那样实现的，
> 而且**也不该**那样实现——见 §4.6.8 的四条理由。

**业务终态在 artifact 的 data part 里**，见 **§4.6**：`execution_status`（跑成什么样）、
逐步骤 `success`、`degraded`、`has_risk`、`result_truncated`。
**只读 `state` 判不出"查挂了"**——查询全挂时 `state` 照样是 `completed`。

> `degraded=true`（模型不可用、走了历史 SQL 重放）**仍然是 `completed`**，降级标记在 artifact 的 data part 里。这与 `message/send` 今天的行为一致，不要在流式下改判。

#### ⚠️ 缺口（v1.5）：业务终态在链路上根本没有字段承载

**性质：契约要改（本版已改）+ 实现待办。**
判定过程与理由见 §4.6.8 末尾的「由此改判」。

> **✅ 已关闭（v1.12）。这个框按 v1.11 定的规矩保留，不摘——只加关闭标记与证据。**
> 两侧实现早已合入 `main`（ai-helpdesk `40be899`、ai-sql-agent `22060ee`），
> 但 v1.9 为这条缺口定下的关闭判据是一个**契约仓库自己的动作**——
> `execution_status` 由可选提级为 `required`、现存 fixture 补齐该字段。
> 判据当初特意挪到这一侧，就是为了不依赖任何一侧的测试标记，所以两侧的合入记录
> 顶不了它；那一步拖到 v1.12 才做，v1.10/v1.11 期间「七条全部关闭」是说过头了
> （2026-05-06 已就地更正）。
>
> **关闭证据**：`schema/artifact-data-part.schema.json` 的 `required` 现在含
> `execution_status`，`tools/check_contract.py` 断言这一点——**改那一行的提交就是证据**，
> 不依赖任何一侧的测试标记，也不会因为谁 rebase 而失效。

**先说事实。** `state` 的实际判据是「pipeline 有没有抛异常」，不是「有没有步骤失败」：
`ai_sql_a2a/server.py:277` 的 `failed` 只出现在 `except Exception` 分支里，
`:317` 对正常结束的 `done` **硬编码** `"completed"`；
`grep -n failed ai_sql_a2a/server.py` 全文只有这两处。

2026-04-28 实测（把 `summarize_result` 打桩成 `failed_steps=1`、`steps[0].success=False`
的摘要，两条 transport 各发一次）：

```
send   终态 state = completed     steps[0].success = False     data part 里没有 failed_steps
stream 终态 state = completed     task → 6×status-update → artifact-update → status-update(final)
```

**SQL 全跑失败了，调用方收到的仍然是 `completed`，而且 `queried_database` 也是 `true`**
（它的定义只是"产生了 pipeline result"，`ai_sql_pipeline/dynamic_service.py:34-36`）。
两个字段都不否定，**调用方今天没有任何字段能发现这次查询挂了。**

**但方向不是"把 `state` 改成 `failed`"。** 那样会让"2 步挂 1 步"按 §5.1 整段回落，
把成功那半份真数据一起扔掉，比现状更糟。真正缺的是**业务终态这一层**——
契约从来没给过。所以 v1.5 做了两件事：

1. **改契约**：上表改写为传输终态（已完成），业务终态另立 **§4.6**；
2. **留缺口**：§4.6 的 `execution_status` 与三个汇总数**还没实现**，
   在它们发出来之前这个洞是敞开的。改判不降低紧迫性。

#### 4.4.1 业务载荷与承载它的 transport 是两件事（v1.8 新增）

**契约面是 data part 本身**——字段名、语义、以及 §4.6 那套信任判据。
**它与谁承载无关。** A2A 只是承载者之一，不是这些规则的来源。

这不是抽象洁癖，是因为**今天的承载者有三个，不是两个**：

| 承载者 | data part 在哪 | 谁在用 |
| --- | --- | --- |
| `message/stream` | `artifact-update` 事件的 `result.artifact.parts[]` | `SQL_AGENT_TRANSPORT=a2a` + 流式开关开 |
| `message/send` | `result.artifacts[0].parts[]` | 同上，流式关或 `-32004` 降级后 |
| **`POST /query`（直连）** | 响应体顶层 | **`SQL_AGENT_TRANSPORT` 的默认值**，也是整条 A2A 关掉时的落点 |

**第三行一直在契约的保护范围之外，而 §6.2 的回滚承诺依赖它。**
这是 v1.0–v1.7 的一个盲区：契约叫「A2A 流式契约」，于是把非 A2A 的那条路
当成了别人的事——可它才是今天跑生产流量的那条。

##### 已经造成的具体缺陷：同一个对象，两个名字

`/query` 把逐步骤日志序列化成 **`step_logs`**（`ai-sql-agent/ai_sql_api/app.py` 的
`QueryResponse`），A2A 那两条叫 **`steps`**。**同一个 `StepLogResponse`，只是名字不同。**

后果不是"字段名不好看"：调用方按 §4.6.7 处理缺失字段，`steps` 取不到 ⇒
按「缺失 = 不可信」处置 ⇒ **每一次直连查询都不再是出处，而链路看起来完全正常**。
没有报错、没有降级事件、没有任何症状。而 §6.2 的回滚恰好把流量切到这条路上——
**回滚会静默地让所有查询失去出处资格**，正是 §6.2 反复要防的那种"静默降级"。

##### 处置：不承认第三种形状

**字段名是契约面。** 所以：

| 谁 | 做什么 |
| --- | --- |
| 服务端 | `/query` 的 `step_logs` **改名为 `steps`**。过渡期**两个字段同时发**（同一份数据序列化两次），不破坏现有消费方 |
| 调用方 | 过渡期**两个名字都认，`steps` 优先**；不要只认一个 |
| 弃用 `step_logs` | 走 §6 的契约变更，届时两侧同时删 |

**为什么不是"在契约里承认第三种形状、把 `step_logs` ↔ `steps` 的对应写进契约面"**
——那是把分叉制度化：同一个对象背两个名字，此后**每一个新的消费方都要重踩一次**，
而契约会永久背着一张翻译表。翻译表是漂移的温床，这份契约已经为副本漂移翻过一次车（附录 B）。
一次改名的成本是有限的、一次性的；一张永久翻译表的成本是无限的、每次新增消费方都付一遍。

##### 由此明确本契约的适用范围

**§4.4 / §4.6 / §9.1（载荷形状与信任判据）适用于全部三个承载者。**
§3（传输层）、§4.1–4.3 与 §4.5（事件与 stage）只适用于 A2A 那两条——
`/query` 是同步无事件的，本来就没有进度事件可言。

---

### 4.5 stage 映射表 ← **本文核心**

**契约面是下表的 `stage` 值，不是我方内部的 stage 名。** 内部重构（比如把 `sql_generation` 改名）不得改变这一列；这一列要改就是一次契约变更，走 §6。

| 我方 `metadata.stage` | 出现时机 | 会重复？ | 附带字段 | 我方默认文案 | 调用方 progress 建议 |
| --- | --- | --- | --- | --- | --- |
| `normalization` ⚠️ **未发送** | 每次（查库路径） | 否 | — | 正在理解问题 | **建议不展示**（内部动作，用户看不出差别） |
| `routing` | 每次 | 否 | — | 正在判断问题类型 | 可对应你那条 `event: routing` |
| `retrieval` | 仅查库路径 | 否 | `with_memory` | 正在检索知识库 | ← 对应你 `[0.00s]` 那条 |
| `planning` | 仅查库路径 | 否 | `short_path` | 正在规划查询步骤 | 展示 |
| `generation` | 每个查询步骤 | **是**（按 `step`） | `step`, `database`, `dialect` | 正在生成查询（ERP 订单库） | 展示 |
| `execution` | 每个查询步骤 | **是** | `step`, `database` | 正在查 ERP 订单库 | ← 对应你 `[0.40s]` 那条 |
| `repair` | 仅 SQL 出错时 | **是，但每个 `step` 至多一次** | `step`, `database` | 查询出错，正在重试 | 展示（这是最值得让用户看到的一条） |
| `validation` | 每个成功步骤 | **是** | `step`, `database` | 正在校验结果 | 建议不展示 |
| `answering` | 仅对话路径 | 否 | — | 正在组织回答 | 展示 |

> **关于"附带字段"这一列的一个诚实说明。** 进度事件在 stage **开始时**发出——
> 这是流式的全部意义，等 stage 结束再发就又变回事后通知了。所以附带字段只可能是
> 进入该 stage 时**已经知道**的那些。像 `routing` 的 `route`/`confidence`、
> `retrieval` 命中的表名，都要到 stage 结束才有值，因此**不在**进度事件里；
> 它们出现在终态的 artifact data part 里（`route`/`confidence` 本来就在）。
> 草案 v0.1 的这一列写得比实际乐观，此处按实现修正。
>
> 同理 `normalization` 的 `changed`（问题有没有被改写）也要到阶段结束才知道，
> 因此进度事件里没有；它落在我方日志和指标里，排查时按 trace_id 取。
>
> 展示所需的字段一个不缺：`execution` 的 `database` 在进入时就有，
> "正在查 ERP 订单库"这句照常拼得出来。

#### ⚠️ 缺口（v1.5）：九个 stage 里 `normalization` 没在发

> **✅ 已关闭（v1.10）。** `_STAGE_MAP` 已九条齐全（`8340323`，随 PR
> heyemlee/ai-sql-agent#1 合入 `main`），`text2sql_pipeline.py`
> 已改真 `stage()`。契约侧的关闭证据是**机器可验的**：`schema/stage.schema.json`
> 的 `emitted-2026-09-08` 已是九个，`tools/check_contract.py` 现在断言
> 「当前实发集合 == `contract-declared`」——**改那个集合的提交本身就是证据**。
> 历史组 `emitted-2026-09-04`（八个）保留做对照，仍断言为真子集，**不许被「修」成九个**。
>
> 下面保留原文，是为了让「这里曾经是个缺口」可追溯（本框的留存规则见状态行下方的缺口表）。

**性质：实现待办（v1.5 记，已于 v1.10 关闭）。** §9 #3 的答复是"全发"，实际发八个。

`_STAGE_MAP`（`ai_sql_a2a/server.py:41-50`）只有八条，没有 `normalization`；
`:40` 的注释自承「normalization 今天还不是真 stage（契约 §8.2 待办），所以不会出现」。
根因就是本节末条早就写下的那件事：`ai_sql_pipeline/text2sql_pipeline.py:245-257`
里它仍是一条裸 `log_event("stage.normalization", …)`，而且**只在 `changed` 为真时才记**，
所以事件汇（`trace.stage()`）根本收不到它。

2026-04-28 实测一次查库请求的 stage 序列：

```
routing → retrieval → planning → generation → execution → validation
```

**影响很小但必须写下来**：`normalization` 在 §4.5 就是"建议不展示"的那一档，
调用方少收一条不影响展示。真正的问题是**契约说全发、实际不全发**——
下一个人照着 §4.5 写消费端断言就会挂。

**建议**：要么按 §8.2 把它改成真 `stage()`（那样也顺带修好"只在 changed 时记"
这个观测盲区），要么把它从 §4.5 的表里删掉、§9 #3 的答复改成"发八个"。
**两条都可以，但契约和代码必须选同一条。**

补充说明：

- **`database` 的契约面是「形状 + 一份当前生效清单」，不是一组钉死的枚举值**（v1.6 改）。

  v1.0–v1.5 把四个标识符写成协议常量、声明"不会在非契约变更中改名或新增"。
  这条承诺是假的，而且**钉不住**：标识符集合不是代码里的常量，是部署配置的产物——
  语义层目录（`AI_SQL_SEMANTIC_DIR`）里放几个 yaml 就有几个库。加一个库是运维动作，
  要求它走双边契约变更，成本差着两个数量级，结果必然是绕过流程。**事实上已经绕过了**（见下）。

  **契约面因此拆成两层：**

  | 层 | 内容 | 稳定性 |
  | --- | --- | --- |
  | **形状**（真正的契约面） | 标识符必须匹配 `^[a-z][a-z0-9_]*_db$` | 改动 = 契约变更 |
  | **清单**（可拉取，不是常量） | 当前生效有哪几个，由服务端在 Agent Card / 语义层启动日志中声明 | 见下方变更规则 |

  **当前生效清单（2026-04-28，`AI_SQL_SEMANTIC_DIR=sim_erp/semantic`）：**

  | `database` | 业务含义 |
  | --- | --- |
  | `erp_db` | ERP：工程、明细、变更单、应收发票与客户回款 |
  | `prod_db` | 生产（MES）：生产工单、工序流水与质检 |
  | `svc_db` | 交付与售后：送货记录、售后服务单与补件请求 |
  | `wms_db` | 仓储：供应商、物料主数据、到货与库存快照 |

  **变更规则（不对称，这是本条的核心）：**

  | 动作 | 算不算契约变更 | 为什么 |
  | --- | --- | --- |
  | **新增**一个标识符 | ❌ 不算 | 调用方最坏是少一条中文文案，退化成显示原始标识符——难看，不错 |
  | **删除 / 改名** | ✅ 算，走 §6 | 调用方已有的映射要么指向不存在的库，要么**更糟：把"ERP 订单库"这个标签贴到别的库上**，用户看到的来源是错的 |

  风险不对称，流程就不对称。这样契约管住真正危险的那一半，同时不再挡住每天都要做的那一半。

  **调用方义务：** 中文映射表遇到清单外的标识符**不得崩溃**，退化为显示原始标识符并记一条日志。
  这一条今天就已经成立——`ai-helpdesk/backend/tools/a2a_client.py:88` 的
  `DATABASE_LABEL.get(database, database)` 就是这个兜底。**规则是照着已有行为写的，不是新要求。**

  调用方按清单出文案，**不要去解析我方的默认文案字符串**——那一列是给没做映射的客户端兜底用的，不保证稳定。

  > #### 📝 名字更正（v1.6）：旧表的四个里，两个从来没存在过
  >
  > v1.5 记录了"部署与契约不是同一组"这个事实，但没有追问**为什么不一样**。
  > 追下去发现不是"部署缺两个库"，是**旧表按通用制造业 ERP 的想象写的**
  > （ERP / MES / PM / WMS 是教科书四件套），而实际系统是照橱柜业务长出来的。
  > 逐个对：
  >
  > | 旧表 | 现清单 | 关系 | 证据 |
  > | --- | --- | --- | --- |
  > | `erp_db` | `erp_db` | ✅ 同名同职责 | —— |
  > | `wms_db` | `wms_db` | ✅ 同名同职责 | —— |
  > | `mes_db` | **`prod_db`** | **同一个库，两个名字** | `sim_erp/semantic/prod_db.yaml:4` 的 description 第一句就是「生产系统（**MES**）：生产工单、工序流水与质检」，`aliases` 里就有 `MES`；表是 `work_order` / `stage_log` / `qc_check` |
  > | `pm_db` | —— | **职责本就在 `erp_db` 里** | 项目 = `erp_db.job`，变更单 = `erp_db.change_order`；`_shared.yaml` 的 `metric_disambiguation` 里"工程总额要算 `erp_db.change_order.amount_delta`"即证 |
  > | —— | **`svc_db`** | **契约从未声明的一整块业务** | `sim_erp/semantic/svc_db.yaml`：送货 / 售后服务单 / 补件，回答"什么时候送货""少件怎么补" |
  >
  > 所以 `mes_db` 是一次**改名**、`pm_db` 是一次**删除**、`svc_db` 是一次**新增**——
  > 按上面的不对称规则，前两个算契约变更（本版就是那次变更），第三个不算。
  >
  > **明确不做的事：为 `mes_db` / `pm_db` 造模拟数据把它们变成真库。**
  > 那会造出一个和 `prod_db` 业务完全重复的库、和一个从 `erp_db` 挖出来复制一份的库。
  > 不只是白费工：语义检索靠库描述和别名选库，两个都自称"生产 / MES / 工单"的库会直接打架，
  > 而跨库准确率本来就只有 16%（`ai-sql-agent/benchmark/report_full.json`）。
  > **不能为了让文档成立去伪造现实。**
  >
  > 遗留：`mes_db` / `pm_db` 仍出现在 benchmark 数据集里
  > （`ai-sql-agent/benchmark/runner.py:246`）。那是评测夹具不是运行时，本契约不管，
  > 但 ai-sql-agent 侧应知道它们与语义层对不上。
- **上表的行序不表示事件顺序**（v1.7 补）。表是按契约面的逻辑分组排的；
  真实顺序由 §4.2 保证，且**与实现的调用栈一致**——今天实际是
  `routing → normalization → retrieval → …`，因为路由发生在服务入口
  （要先判断这问题该不该查库），归一化发生在 pipeline 内部（只有查库路径才需要）。
  **不要照着表的行序写「首个 stage 必须是 `normalization`」这类断言**，那会挂。
- **`database` 不匹配上面那个形状时，该 data part 视为不可信**（v1.7 补，
  补的是 `dp-bad-06` 一直缺的正文依据）。形状是契约面，违反它说明发送方
  要么不是本契约约束的服务端，要么序列化坏了——两种情况下都不该把它的
  结果当业务数据。这与"清单外但形状合法"是两回事，后者是常规路径（见上）。
- **`step` 从 1 开始**，跨库查询会有多步。

#### repair 的次数上限（调用方用来判断"推进 vs 原地打转"）

因为调用方的续命规则是"收到任何事件就续命"，而 `repair` 是会重复的 stage，这里把它的上限钉死：

| 事实 | 值 |
| --- | --- |
| 一个 `step` 最多产生几个 `repair` 事件 | **1 个** |
| 一次请求最多产生几个 `repair` 事件 | **等于查询步骤数**（实测上限 2，见下） |
| 内部重试次数 | 默认 **2**（`max_repair_attempts`），**不产生额外事件** |

关键点：**修复的内部重试循环整个跑在同一个 `repair` stage 里**，所以两次重试不会变成两个事件。一个 `step` 的 `repair` 事件发出去之后，下一个事件必然属于**别的 step 或别的 stage**。

因此调用方有一条干净的判据：

> **`repair` 事件的 `step` 必须严格递增。同一个 `step` 收到第二个 `repair` 事件 = 我方有 bug**，可以当异常处理。

步骤数本身由规划阶段决定，配置里**没有硬上限**；2772 条真实审计记录里只出现过 0 / 1 / 2 步，实测上限是 2。所以正常情况下一次请求的 `repair` 事件不会超过 2 个。

> **诚实标注**：`repair` 有硬上限（每 step 一次、内部重试 2 次），**步骤数没有**。理论上规划阶段给出 N 步就会有至多 N 个 repair 事件。真正兜住这个的是调用方的 90s 硬上限，不是我方的配置。要不要给规划步骤数加一个硬上限，是一个独立问题，不在本契约范围内——但如果调用方希望契约层面给出绝对上限，说一声，我方加一个 `max_plan_steps` 配置即可。
- **内部有而契约不暴露的 stage**：`memory_context`（会话记忆读取，纯内部管道，对用户没有展示价值）。刻意不发，避免契约面无谓变大。
- **`normalization` 在我方今天仍不是一个真正的 stage**（`ai_sql_pipeline/text2sql_pipeline.py:245-257` 一条裸 `log_event("stage.normalization", …)`，且只在 `normalization.changed` 为真时才记）。要发这个事件需要先把它变成真 `stage()`，见 §8.2 —— **v1.4 没做，见上面的缺口框。**（行号为 2026-04-28 复核值；v1.0 写的 `:216` 已漂移。）

### 4.6 业务终态语义（v1.5 新增）

§4.4 给的是**传输终态**：载荷有没有完整送达。调用方真正要判断的是**业务终态**：
这份数据能不能拿去回答客户。今天契约层没有后者，于是每个消费端各自用
`queried_database` + `answer` 非空拼一个近似判据出来——而这两个都不承载它被当成的含义：

- `queried_database` 只表示"产生了 pipeline result"。定义就是一行：
  `return self.pipeline_result is not None`（`ai_sql_pipeline/dynamic_service.py:34-36`）。
  **SQL 全挂了它也是 `true`。**
- `has_risk` 只汇总校验发现：`any(s.risk_findings for s in step_logs)`
  （`ai_sql_api/result_summary.py:105`），与 `failed_steps`（`:93`）是两套互不相干的计数。
- `answer` 非空只说明模型说了话，不说明它说的话有数据支撑。

本节把业务终态定义清楚。**这是调用方修那条判据的前提**——不先定清楚，
改出来的也是另一个近似判据。

> **每个字段都标了「今天在不在线上」，请照着读：**
> ✅ = 已在 artifact data part 里，本节只是补定义；
> ➕ = 服务端已经算出来了但没放进 artifact，搬过去代价接近零；
> 🔨 = 服务端今天没有这个概念，**先定义、后实现**，实现前按"未知"处置。

#### 4.6.1 `execution_status`（✅ v1.12 起真的必填）

一个闭集字符串，回答"这次查库到底跑成什么样"：

| 值 | 含义 | 判据（全部可由现有 `steps[]` 算出） |
| --- | --- | --- |
| `not_applicable` | 本轮没查库，走的是对话路径 | `route != "database_query"` |
| `success` | 所有步骤成功，且合计返回了行 | `all(s.success)` 且 `sum(row_count) > 0` |
| `no_data` | 所有步骤都成功执行，但合计零行 | `all(s.success)` 且 `sum(row_count) == 0` |
| `partial_success` | 有成功也有失败 | `any(s.success)` 且 `any(not s.success)` |
| `failed` | 一步都没成 / 查库路径却没有步骤 | `not any(s.success)`，含 `steps == []` |

**`no_data` 与 `failed` 必须分开。** 前者是"查到了，答案是没有"，后者是"没查成"。
对客户是两句完全不同的话，而今天调用方分不出来——它只判 `answer` 空不空
（`ai-helpdesk/backend/api/main.py:1045`），两种情况都落进同一个分支。

**`partial_success` 为什么不能并进 `failed`。** 跨库查询 2 步挂 1 步时，
成功那步的数据是真的、可用的。按 §5.1 判 `failed` 会让调用方整段回落，
**把那半份真数据一起扔掉**——比现状更糟。它需要的是第三个值。

#### 4.6.2 逐步骤成功 / 失败（✅ 已在线上，本节补定义）

`steps[]` 的每一项，字段与取值域：

| 字段 | 类型 / 取值域 | 状态 | 证据 |
| --- | --- | --- | --- |
| `database` | 见 §4.5 的 `database` 契约面 | ✅ | `ai_sql_api/result_summary.py:25` |
| `success` | boolean，**严格布尔**（不接受 `"true"` / `1`） | ✅ | `result_summary.py:28`、`:83` |
| `row_count` | integer ≥ 0 | ✅ | `result_summary.py:29` |
| `truncated` | boolean | ✅ | `result_summary.py:30` |
| `error` | string \| null；`null` ⟺ `success == true` | ✅ | `result_summary.py:32` |
| `final_sql` | string，**实际执行的那条**（修复后的） | ✅ | `result_summary.py:27` |
| `repair_attempts` | array，长度 ≤ `max_repair_attempts`（默认 2，§4.5） | ✅ | `result_summary.py:33` |
| `risk_findings` | array；非空 ⟺ 该步对 `has_risk` 有贡献 | ✅ | `result_summary.py:34`、`:105` |

三个汇总数，**调用方不应自己 reduce**（reduce 的口径会漂）：

| 字段 | 类型 | 状态 | 说明 |
| --- | --- | --- | --- |
| `succeeded_steps` | integer ≥ 0 | ✅ | 已进 artifact（`api/result_summary.py` 的 `data_part()`，`/query` 与 A2A send / stream 共用） |
| `failed_steps` | integer ≥ 0 | ✅ | 同上，已进 artifact |
| `repaired_steps` | integer ≥ 0 | ✅ | 同上，已进 artifact |

> 三个数与 `steps[]` **必须自洽**：`succeeded_steps + failed_steps == len(steps)`。
> 不自洽时调用方以 `steps[]` 为准，并按"数据可疑"处置。

#### 4.6.3 结果截断（✅ 服务端已实现）

链路上有**两道**截断闸，语义不同，排查时必须能分开：

| # | 谁截的 | 字段 / 配置 | 状态 |
| --- | --- | --- | --- |
| 1 | 我方按行截 | `steps[i].truncated`；每条查询最多 **1,000** 行（`PipelineConfig.max_rows`，`src/ai_sql_agent/pipeline/objects.py:60`）。执行器多取一行判断是否截断，并把生效的上限写进 `row_limit`（`execution/postgres_executor.py:142-159`、`execution/sqlite_executor.py:93`） | ✅ |
| 2 | 调用方按字符截 | `SQL_AGENT_MAX_CONTEXT_CHARS` 默认 **2000**（`ai-helpdesk/backend/app/api/main.py:1308`） | 调用方侧，**不是契约面**，写在这里只为提醒排查时有两道闸 |

> **别被 `AI_SQL_MAX_ROWS` 误导：** `core/settings.py:287` 里的 `max_rows`（环境变量
> `AI_SQL_MAX_ROWS`，默认 10000）今天只写进启动日志（`api/app.py` 的 `api.startup`
> 事件），**没有接到执行上**。改它不会改变上限，生效的是上面那个 1,000。
> 本节 v1.5 版写的"默认 10000（`ai_sql_core/settings.py:261`）"指的就是这个没接上的配置，
> 2026-06-05 按代码改正。

两个字段（v1.5 定义，服务端已输出）：

| 字段 | 类型 | 状态 | 说明 |
| --- | --- | --- | --- |
| `result_truncated` | boolean，顶层 = `any(s.truncated)` | ✅ `api/result_summary.py:102`、`:220` | 让调用方一跳拿到，不用遍历 `steps[]` |
| `steps[i].row_limit` | integer \| null | ✅ `execution/objects.py:36`、`api/result_summary.py:58` | 被哪个上限截的。`truncated == true` 时**必须**有值——否则调用方无法判断"1,000 行封顶"还是"被临时调小的上限截了" |

**截断的表述后果**：`truncated == true` 时，任何"共有 N 条 / 全部 / 一共"的说法都不成立。
调用方必须把它当成"至少 N 条"，且**不得**基于截断结果做求和、计数、比例类断言。

#### 4.6.4 数据时点 `as_of`（✅ 服务端已输出）

| 字段 | 类型 / 取值域 | 说明 |
| --- | --- | --- |
| `as_of` | RFC 3339 字符串 \| `null` | 这份数据代表的业务时点 |
| `as_of_source` | `"query_time"` \| `"snapshot"` \| `"unknown"` | 时点是怎么来的 |

- `"query_time"`：读的是主库当前状态，`as_of` = SQL 执行时刻。今天所有查询都属于这一类。
- `"snapshot"`：读的是快照 / 数仓 / 只读副本，`as_of` = 快照时点，**可能显著早于现在**。
- `"unknown"` 或字段缺失：**不可信**。

**现状（2026-06-05 按代码核对）：服务端已经输出这两个字段。**

- 每一步都带 `steps[i].as_of` / `steps[i].as_of_source`，取值在执行器里定
  （`execution/sqlite_executor.py`、`execution/postgres_executor.py`，取值域见
  `execution/data_time.py`）。执行器默认 `as_of_source = "query_time"`，因为今天读的都是主库；
  只有给执行器挂上只读副本或快照库时才改成 `"snapshot"`，目前没有这样配置的库。
- 顶层 `as_of` 取成功步骤里**最早**的那个时点——"这份数据至少有多旧"是唯一安全的读法；
  各步来源不一致（一步主库、一步快照）时，顶层 `as_of_source` 降为 `"unknown"`
  （`api/result_summary.py` 的 `_resolve_as_of`）。失败的步骤不参与。

本节 v1.5 版写的"服务端完全没有这个概念"已经过时，2026-06-05 改正。

**缺失时的处置（仍然生效）：** `as_of` 为 `null` / 缺失时，
调用方**不得**向客户表述任何时点（"截至今天""目前""最新"）。
今天查的都是主库，所以时点就是查询时刻；接了只读副本或数仓之后，
主从延迟会让"最新状态"变成几分钟到几小时前的状态。那时调用方能靠的就是这两个字段：
`as_of_source == "snapshot"` 时按 `as_of` 说"截至某时"，`"unknown"` 或缺失时不说时点。

#### 4.6.5 币种与单位（🔨 服务端今天没有，且已经在被绕过）

| 字段 | 类型 / 取值域 | 说明 |
| --- | --- | --- |
| `currency` | ISO 4217 三字母大写（`"USD"`）\| `null` | 顶层：本轮金额列的币种。多币种时留 `null`，逐列在下面给 |
| `steps[i].column_units` | object：列名 → 单位标识 \| 不出现 | 例：`{"contract_amt": "USD", "qty": "EA", "lead_time_days": "day"}` |

**这一条有现成的证据说明它为什么必须进契约。** 服务端不返回币种
（`grep -rn currency ai_sql_pipeline/ ai_sql_api/ ai_sql_semantic/` 无命中），
于是调用方在**自己那边配了一个全局常量**把它补上：
`SQL_AGENT_CURRENCY` 环境变量（`ai-helpdesk/backend/tools/sql_agent_client.py:151`），
再往 prompt 里塞一句"以上金额的币种是 X"（`backend/api/main.py:1073-1080`）。
那段注释把风险写得很清楚：

> 下游只返回数字不返回币种，不点明的话中文回答会默认写成「元」——
> 接美国厂的库时，报给 builder 的金额就带了错误的币种。

**这是一个跨仓库配置在替契约打补丁。** 币种是**数据的属性**，不是调用方的属性：
一个 ai-sql-agent 接多个库、或一个库里同时有 USD 与 CNY 列时，
那个全局常量**必然**给出错误答案，而且是静默的——报出去的金额看起来完全正常。

- 自然的数据源是语义层 YAML（`ai_sql_semantic`），那里今天也没有 `unit` 键——
  一并算在 🔨 里。

> ##### 缺失处置：**禁止的是"把配置当成事实"，不是"使用配置"**（v1.8 改写）
>
> v1.6/v1.7 这里写的是「**不得**回退到自己的全局常量」。那条禁令**过头了**，
> 而且照它执行会退回它想防的那次事故——调用方实现时发现了这一点，交回了反馈：
>
> > 在 `currency` 还没实现的今天，照这条执行等于把 `SQL_AGENT_CURRENCY` 删掉。
> > 而那个常量当初就是为了堵一次已经发生过的业务事故：下游只返回数字，
> > 中文回答**默认写成「元」**，接美国厂的库时报给 builder 的金额带了错误的币种。
> > **删掉它，"不得擅自补币种"并不会让语言的默认值消失——它只是让那个默认值重新变成隐式的。**
>
> **这个论证成立，契约改。** 关键区分是：危险的不是"用了配置值"，
> 是"**把配置值说成系统核实过的事实**"。所以按来源三分，并**把来源写进给用户的那句话里**
> （与 §4.6.7 的 `status_source` 同构，字段名建议 `currency_source`）：
>
> **`currency_source` 是调用方的字段，服务端不发**（v1.9 澄清）。服务端不知道
> 调用方的部署配了什么，它只保证：**判得出币种就下发 `currency` / `column_units`，
> 判不出就不发**。来源三分是调用方对"这句话该怎么说"的判定，不是载荷字段。
> `status_source` 同理——两个都是**调用方给自己的结论贴的来源标签**，
> 不要求服务端回传。（服务端如果发一个恒为 `contract` 的字段，那个字段只有一个
> 取值，没有信息量。）
>
> | 情况 | 处置 | `currency_source` |
> | --- | --- | --- |
> | 数据自己声明了 `currency` / `column_units` | 按数据说，**可以当事实** | `contract` |
> | 数据没声明，部署配了全局值 | **可以用**，但必须在句子里写明"这是部署配置，不是本次查询返回的字段，不得把币种说成系统核实过的结果" | `deployment_config` |
> | 两者都没有 | **只写数字、不补任何币种符号**，并**主动禁止**语言默认值（中文回答不得默认写「元」） | `unknown` |
>
> **第三档不是沉默。** 沉默正是事故的形态——自然语言有它自己的默认值，
> "不补币种"不会让默认消失，只会让它变回隐式。所以第三档必须是一条**主动的指令**，
> 不是一条禁令。
>
> 这条推论对本节以外同样成立：**凡是"缺了就别说"的规则，都要检查一遍
> "不说"会不会被某个默认值填上。** 会的话，规则要写成"说什么"，不能只写"别说什么"。

#### 4.6.6 来源记录标识（🔨 服务端今天没有）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `steps[i].source_records` | array \| 不出现 | 每项：`{"database": "erp_db", "table": "job", "key_columns": ["job_no"], "keys": ["J25-00003"]}` |

用途：把"客服说的这句话"追回到**哪几条业务记录**。
今天能追到的最细粒度是一次**查询**（`trace_id`），追不到一条**记录**——
出了纠纷要复盘"这个日期是从哪条单子读出来的"，只能拿 `final_sql` 回去重跑，
而重跑得到的是**今天**的数据，不是当时的数据。

**实现之前的处置：** `trace_id` + `steps[i].final_sql` 是唯一可用的溯源手段
（两者今天都已经在 data part 里：`ai_sql_a2a/server.py:209` 的 `trace_id`、`result_summary.py:27` 的 `final_sql`）。
调用方至少要把这两个记进自己的会话日志——`trace_id` 今天已经记了
（`ai-helpdesk/backend/api/main.py:1059`），`final_sql` 没有。

#### 4.6.7 缺失与未知的统一处置：**缺失 = 不可信，不是默认 `false`**

> #### ⚠️ 先读这一段：本表只管「已经在线上、这次没送到」的字段（v1.7 补）
>
> v1.6 的这张表漏了一个区分，**两侧在实现时各自撞上了同一堵墙并各自绕开**
> ——正是契约存在要防的那种分叉：
>
> | 情况 | 按什么处置 |
> | --- | --- |
> | 字段**契约已要求、服务端也在发**，这次没送到 / 类型不对 | 下表（**降级为不可信**） |
> | 字段标着 **🔨 尚无此概念** 或 **➕ 已算出未下发**，服务端还没实现 | **各小节的过渡判据**，不落进下表 |
>
> **为什么必须分开。**（写于 v1.7，当时 `execution_status` 还标着 🔨。）
> 那时**每一份线上载荷都缺它**，照下表逐字执行 → 所有查询一律 `failed` →
> 一条都不能作出处 → **A 组正例（`dp-valid-01` 等）全红**。而 CONSUMER-TESTS 自己写着
> 「A 组与 B 组必须同时绿」。一份自己过不了自家用例的规范，是规范写错了。
> **v1.12 起这个字段已提级、不再属于本表管辖**，但这个区分本身对其余
> 🔨/➕ 字段仍然有效，所以整段保留。
>
> ### ⚠️ v1.12：`execution_status` 这一条过渡判据**已作废**
>
> 它在 v1.12 提级为 `required`（§6 契约变更）。按本节最后一句的规矩，
> **提级即作废**——所以下面那张表是历史，记录的是 v1.7–v1.11 之间的正确做法。
> 今天的规矩只有两行：
>
> | 情况 | 处置 | 来源标记 |
> | --- | --- | --- |
> | 载荷合规（带 `execution_status`） | 直接采信 | `contract` |
> | 载荷不合规（缺这个字段） | **schema 就该拦住它**。调用方仍可从 `steps[]` 推导以免崩，但结论**不得作出处** | `derived`，且一律按不可信处置 |
>
> **推导能力不要删。** 作废的是「推导出来的结论可以照常采信」这一条，**不是推导本身**：
> 服务端出 bug、版本错配、中间件吞字段，都会让不合规载荷真的出现，
> 那时候还得靠 `steps[]` 说清楚「这次查挂了」，而不是直接崩或者静默采信。
> 机器可校验的反例是 `dp-trust-01`——它由 C 组改判到 B 组，**唯一的拒绝理由就是缺这个字段**。
>
> 下面是作废前的原文，保留可追溯：
>
> **`execution_status` 的过渡判据（规范，不是权宜）：**
>
> | 情况 | 处置 | 来源标记 |
> | --- | --- | --- |
> | 服务端下发了 `execution_status` | 直接采信 | `contract` |
> | 没下发，但 `steps[]` 在 | 按 **§4.6.1** 的判据从 `steps[]` 推导 | `derived` |
> | `steps[]` 也拿不到 | `failed` | `missing` |
>
> 这不是发明新规则：§4.6.1 明确写了五个值的**判据全部可由现有 `steps[]` 算出**。
> `result_truncated` 同理——顶层字段没有就看 `steps[i].truncated`（那个今天在线上），
> 两者都没有才按 `true` 处置。**`result_truncated` 的过渡判据仍然有效**，
> v1.12 只提级了 `execution_status` 一个字段。
>
> **调用方必须把来源标出来**（上表第三列，字段名建议 `status_source`）。
> 理由见 §4.6.2 自己的警告：「三个汇总数调用方不应自己 reduce，reduce 的口径会漂」
> ——只要来源还是 `derived`，那句警告就还成立，缺口就没真关。
> 两侧独立实现时都自发做了这件事并都叫了这个名字，v1.7 把它收进契约。
> **v1.12 起合规载荷的来源恒为 `contract`，那句警告对这个字段不再成立**——
> 这正是 §4.4 能写「已关闭」的原因。
>
> 🔨/➕ 字段**提级为必填**时（走 §6），过渡判据随之作废，那时它们才落进下表。
> （`execution_status` 已于 v1.12 走完这一步，其余 🔨/➕ 字段照旧。）

本节所有字段共用一条规则：

| 字段 | 缺失 / 未知 / 类型不对时，按什么处置 |
| --- | --- |
| `execution_status` | **`failed`** |
| `steps[i].success` | **`false`** |
| `steps[i].truncated` / `result_truncated` | **`true`** |
| `steps[i].row_limit`（当 `truncated == true`） | 视为截断上限未知 → 禁止任何总量表述 |
| `has_risk` | **`true`** |
| `degraded` | **`true`** |
| `queried_database` | **`false`** |
| `as_of` / `as_of_source` | 禁止表述时点 |
| `currency` / `column_units` | 见 §4.6.5 的**三档处置**——按来源分，不是一律禁止 |
| `source_records` | 退回 `trace_id` + `final_sql` 溯源 |

> **为什么这条规则要单独写一节。** 上面每一个字段的作用都是**降低**信任。
> 一个"降低信任的信号"缺席时，若按"没有这个信号"处理，等于默认信任——
> 于是**任何序列化 bug、版本错配、字段改名，都会静默地把不可信数据升级成可信数据**。
> 这不是假想：§9.1 的缺口就是这个模式的实例（`bool(data.get("has_risk", False))`
> 把"字段没送到"翻译成了"没有风险"）。
>
> 严格布尔同理：`"false"` 是一个非空字符串，`bool("false")` 为真。
> 三个信任布尔**必须是 JSON 布尔**，类型不对按上表处置，不做宽容转换。
> 机器可校验的版本见 `schema/artifact-data-part.schema.json`。

#### 4.6.8 `degraded` 与 `completed` 的关系：**保留，但 `state` 的含义要写清楚**

**结论：保留。`degraded == true` 仍然是 `state: "completed"`。** 四条理由：

1. **A2A 0.2 的 `TaskState` 里没有"部分成功"这个值。** 把 `partial_success` 或
   `degraded` 硬压成 `failed`，按 §5.1 调用方会整段回落，
   连同那份通常是正确的历史重放结果一起扔掉。
2. **`state` 是协议层字段，通用 A2A 客户端只读它。** §6.1 承诺"通用 A2A 客户端库…
   能正确解析"。把业务语义压进 `state`，等于让一个协议字段的含义随业务规则漂。
3. **两条 transport 必须同构（§4.4）。** 动 `state` 的判定会同时改 `message/send`，
   而 send 是 §6.2 的回滚落点——回滚路径的行为不该被一次业务语义变更牵动。
4. **`degraded` 的信息量在于"为什么可能不准"，不在于"成没成功"。** 历史 SQL 重放
   是一个完整、可用、但可能过期的结果。判 `failed` 会丢掉它，判成无差别的
   `completed` 又会让它被当成新鲜数据。它需要的是第三种表达——那就是 data part 里的
   `degraded` + `degrade_reason`（两者今天都已在线上：`ai_sql_a2a/server.py:206-207`）。

**保留就必须回答：调用方靠什么区分。** 靠下表，四个问题读四组字段：

| 调用方要判断的 | 读什么 | 今天可用？ |
| --- | --- | --- |
| 载荷是否完整送达 | `final:true` 且 `status.state == "completed"`（§5.1） | ✅ |
| 查询是否真的跑成了 | `execution_status` | 🔨（今天只能自己 reduce `steps[].success`） |
| 数据是否新鲜 | `degraded` + `degrade_reason` + `as_of` | 前两个 ✅，`as_of` 🔨 |
| 能不能当出处 | `execution_status ∈ {success, partial_success}` **且** `has_risk == false` **且** `degraded == false` | 见 §9.1 缺口 |

> #### 由此改判 §4.4 缺口的性质
>
> 把两层分清之后，「`failed_steps > 0` 仍发 `completed`」**不是实现 bug，
> 是 §4.4 那张表写错了**——它拿传输终态的字段去描述业务终态。
> 状态行缺口表里这一行的性质，据此由「实现待办」**改判为「契约要改 + 实现待办」**：
>
> - **契约要改（本版已改）**：§4.4 的 `state` 表改写为传输终态，业务终态指向本节；
> - **实现待办（仍然是缺口）**：`execution_status` 与 4.6.2 的三个汇总数要真的发出来。
>   在它们发出来之前，调用方**没有任何字段**能区分"查成了"和"查挂了"——
>   `queried_database` 两种情况下都是 `true`。**这一条的紧迫性没有因为改判而降低。**

---

## 5. 失败与回落（超时预算）

### 5.1 唯一的成功判据

> **收到 `final:true` 且 `state:"completed"` 的终态事件 ⟺ 成功。其余一切情况都回落。**

这条是硬规则，用来兑现事实③里的"不能留下半个结果被当成成功"。

> **v1.5 补一句边界，不改这条规则本身。** 上面这条判的是**传输**：
> "这次委派有没有把一份完整载荷交到你手上"。它**不判数据可不可信**——
> `state == "completed"` 时查询可能全挂、结果可能被截断、数据可能来自历史重放。
> 拿到 `completed` 之后还要过**业务**那道判据，见 §4.6。
>
> 一句话：**`completed` 是"可以开始判断"，不是"判断完了"。**

具体地，以下**都不是**成功，调用方必须回落到静默降级（今天的"请提供订单号"）：

| 情况 | 判据 |
| --- | --- |
| 流正常结束但没有终态事件 | 迭代器耗尽 且 未见 `final:true` |
| 终态事件是 `state:"failed"` | `final:true` + `failed` |
| 终态事件是 `state:"canceled"`（v1.13） | `final:true` + `canceled`。上面那句「其余一切情况都回落」本来就覆盖它；写出来是为了一眼看到，不必自己推 |
| 连接中断 / 协议错 / JSON 解析失败 | httpx 抛异常 |
| 静默超过 20s | 调用方的自适应超时（收到事件会续命）。**仅流式路径**，见 §1 |
| 总时长超过 90s | 调用方的硬上限，与有没有事件无关 |
| 响应 `Content-Type` 是 `application/json` | 鉴权/限流/参数错，见 §3.2 |

**特别地：只收到 `artifact-update`、没收到终态事件，也算失败。** artifact 在正常情况下只比终态事件早几毫秒，按严格规则处理几乎不损失可用结果，但能彻底堵死"半个结果被当成成功"这条路。

> **本节只管 `message/stream`。** `message/send` 没有事件流，也就没有"终态事件"可判——
> 它的成功判据是今天已有的那套（HTTP 200 + Task 的 `status.state`），本契约不改动。
> v1.2 对 send 只改一件事：**预算改成固定 90s、不套静默规则**（§1）。
> 把上表的静默 20s 套到 send 上是本契约明确禁止的读法。

### 5.2 流打开之后才出错怎么办

我方**不会**靠断开连接来表示失败。任何在流中途发生的错误，都会补一个终态事件再关闭：

```json
{"kind":"status-update","final":true,
 "status":{"state":"failed","message":{...部分文本，可能为空...}},
 "metadata":{"trace_id":"…","error":"查询执行失败"}}
```

这样调用方永远走同一条判定路径（§5.1），不需要区分"优雅失败"和"连接断了"。

### 5.3 预算是预算，不是可以商量的下限

我方**不会**为了塞进 90s（或 20s 静默窗）而截断查询、跳过修复或降低质量。流式在这里的作用只有一个：把等待变成可见的、有语义的进度。查询该多久还是多久。

反过来也成立：**我方也不会为了"看起来在动"而发无意义的事件**。每个进度事件都对应一次真实的阶段切换，见 §3.4。

### 5.4 超时之后我方会发生什么

**在新口径下这件事已经很少发生**：实测 max 68.3s < 90s 硬上限，所以只有极端长尾才会命中。以下描述的是那个长尾。

> **v1.13 改写了本节。** v1.12 写的是「我方**不会**中止查询。整条 pipeline 是同步的，没有取消点」。
> 服务端有了取消检查点，这条改为：**调用方断开后，我方在下一个检查点停下。**

**适用范围：三个承载者**——`message/stream`、`message/send`、直连 `/query`。后两者以前断开后同样不中止，
只是契约没写过；调用方的默认 transport 就是 `/query`，所以三条一起写清楚。

调用方断开连接后（静默 20s、总长 90s、网络中断、主动关闭），我方在**下一个检查点**停下，不再发起新的模型调用和 SQL：

| 检查点 | 延迟 |
| --- | --- |
| 阶段边界 | 立即 |
| 两次模型调用之间、重试退避中 | 立即 |
| SQL 执行中（SQLite 进度回调；PostgreSQL 服务端取消） | 立即 |
| 已经发出去的那一次模型调用 | **打断不了**。最坏要等它返回，上限是模型调用超时（今天 60s），返回结果丢弃 |

> **实测（2026-06-09，真实模型）。** 服务端经自己的取消接口取消了 6 次，取消分别落在 routing、检索、规划等阶段：
> 从收到取消到运行结束 **0.07–2.7s**，每次都等于那一次在途模型调用剩下的时间，再加不到 8ms；
> 取消之后没有再发起任何模型调用。断开触发的取消走的是同一套检查点。
> SQL 执行中取消在 PostgreSQL 16 上有测试：服务端 `tests/test_run_cancel.py::PostgresExecutorCancelTest`。

**审计与配额：**

- 照写一条审计，`status_code = 499`，`error` 以 `cancelled:` 开头；
- **每日配额照计一份**，§5.5 不变；
- token 只计断开前已经发出的调用（外加那一次在途调用）。

含义：**超时仍会烧掉一份配额**——配额照计；但不再烧完整的 token，断开之后最多再花掉那一次在途调用。

**服务端开关：** `AI_SQL_CANCEL_ON_DISCONNECT`，三条入口共用，v1.13 起默认开。
关掉就回到 v1.12 的行为（断开后不中止，跑完为止）——那是降级，不需要调用方配合。

**对调用方不可见：** 它断开之后不再读任何东西。下面「三重限制」的论证不受影响。

三重限制让这件事的实际损失接近于零：

1. **极端长尾才会命中**——90s 硬上限之上，实测 max 68.3s 够不着；
2. **调用方没有查询重试**（§9 #1 已查实）——唯一的 backoff 是 Agent Card 发现（30s），
   查询失败一律静默回落。不存在"重试 3 次 = 3 份配额"的放大，一次超时就是一份；

   > **v1.2 的一个例外，以及它的边界。** 调用方补完 `-32004` 降级后，
   > 同一轮里会改发一次 `message/send`——这在形式上就是本节警告的那种"重试"。
   > 它**不构成放大**，因为 `-32004` 在**流打开之前**返回（§3.2），
   > 那一刻我方一个令牌都没扣、一次模型调用都没发。
   >
   > **豁免的边界就是"流打开之前"这五个字。** 任何**流中途**的失败
   > （§5.2 的 `failed` 终态、连接中断、静默/硬上限超时）都**不得**降级重发 `send`：
   > 那时我方的配额已经扣了、模型已经跑了，重发就是实打实的两份。
   > 真要放宽这一条，先回到本节和 §5.5 重新评估，别在调用侧单方面加。
3. 把固定 25s 改成自适应 90s 的**拍板理由就是这一条**：宁可让用户多等，
   也不要把已经花掉的模型调用扔了。

写在契约里是因为**它依赖"调用方不重试"这个前提**——哪天调用方加了重试，
放大就回来了，届时应回到本节重新评估（比如加请求去重）。

### 5.5 限流与配额

**一次 `message/stream` = 一次 `/query`**：一个令牌、一条审计、一份配额。与 `message/send` 完全等价，不打折也不加倍。

限流命中时在流打开之前返回 429（含 `Retry-After` 头），JSON-RPC 错误码 **`-32029`**，走 §3.2 的 JSON 错误路径。
每日配额用尽、流式并发准入满了，也是 429 + `-32029`。（v1.13 写明码值；之前用的 `-32001` 和 A2A 规范的 TaskNotFoundError 撞码，见 §3.2。）

被取消的请求（§5.4）照计一份配额，审计 `status_code = 499`。

---

## 6. 向后兼容

### 6.1 `capabilities.streaming` 翻 true 之后，老客户端怎么办

**`message/send` 永久保留，不弃用、不改形状、不降优先级。** 两条路走的是同一个 handler、同一套鉴权限流审计，只是出口不同。

| 客户端类型 | 行为 |
| --- | --- |
| 不读 Agent Card，硬编码 `message/send` | 完全不受影响 |
| 读 Card 但只实现 send | 完全不受影响（`streaming:true` 只是"另有一条路"，不是"这条路没了"） |
| 读 Card 且实现 stream | 走流式 |
| 通用 A2A 客户端库，见 `streaming:true` 就自动优先 stream | 走流式，且能正确解析——事件形态是标准 A2A |

**不做版本协商**：A2A 没有 per-method 版本位，加一层自定义协商只会让两边都变复杂。Card 的 `version` 从 `1.0.0` 提到 `1.1.0` 作为标记，`protocolVersion` 保持 `0.2`。

### 6.2 回滚

流式能力挂在一个开关上（建议 `AI_SQL_A2A_STREAMING`，默认开）：

- 关掉 → Card 的 `capabilities.streaming` 变回 `false`，`message/stream` 重新返回 `-32004`；
- **不需要重新部署，也不需要调用方配合**——调用方读到 `streaming:false` 或收到 `-32004` 就退回 `message/send`，那是它今天已有的行为。

> ### 回滚成立的两个前提
>
> 回滚不是"关掉开关"这一个动作，它落在 `message/send` 上——所以 send 那条路
> 必须真的能接住。两个前提缺一不可：
>
> | # | 前提 | 状态 |
> | --- | --- | --- |
> | 1 | 调用方读到 `-32004` 能**降级重发** `message/send`，而不是统一错误回落 | ✅ **已实现并实机演练**（v1.2 曾虚报、v1.3 推翻；这次的证据见下） |
> | 2 | `message/send` 的**预算够跑完一次正常查询** | ✅ v1.2 起固定 90s（§1）。若套用静默 20s、或停在固定 25s，回滚就是**静默降级** |
>
> 前提 1 是 v1.0 / v1.1 里的 ⚠️ 阻断项。v1.2 据一份不存在的验证解除过它，
> v1.3 核对后恢复。**v1.4 真正解除**——2026-04-27 做了两条路的实机演练：
>
> | 演练 | 做法 | 结果 |
> | --- | --- | --- |
> | Card 路径 | 服务端开关翻 false，**调用方不重启** | Card 过期后读到 `streaming:false`，直接走 send，答案正确 |
> | `-32004` 路径 | 把调用方 Card 缓存强行写成 `streaming:true`（模拟「开关刚关、手上还是旧 Card」） | 发 `message/stream` 收到 `-32004`，日志「改发 message/send」，答案正确 |
>
> 两条都验过，所以 `AI_SQL_A2A_STREAMING` **默认改为开**。关掉它是降级，不是故障。
>
> 前提 2 是 v1.2 新识别出来的——它一直存在，只是 v1.1 把 send 也算进自适应口径时
> 被掩盖了。**它的生效时机跟着 §1 的上线顺序走**：流式承接流量之前 send 走的是
> 第一/第二档（今天是第二档 45s，v1.6 补），而那个窗口里流式本来就没承接流量，
> 没有可回滚的东西，不构成缺口。切到第三档 90s 时前提 2 才真正吃紧——
> **回滚落点的预算必须与主路的硬上限相等**，否则回滚就是静默降级。
>
> 两个前提都满足之后，`AI_SQL_A2A_STREAMING` 才可以真的关，而不必靠回退部署。

---

## 7. 明确不承诺什么

写清楚比留白好——以下都是**刻意不做**，不是"还没做"：

| 能力 | 做不做 | 原因 |
| --- | --- | --- |
| **断线续传 / `Last-Event-ID`** | ❌ 不做 | 事件不带 `id:` 字段。v1.13 起服务端有了按序号的运行事件日志，技术上能续传；但调用方没有重连逻辑，新增的面（重放边界、repair 的递增判据怎么算）比收益大。**暂不需要，不是做不到。** 断了就重发请求。 |
| **`tasks/resubscribe`** | ❌ 不做 | 继续返回 `-32004`。理由同上：暂不需要。 |
| **`tasks/cancel`** | ❌ 不做 | 继续返回 `-32004`。v1.13 起服务端有取消点了（§5.4），但调用方用不上它：断开本身就会取消，控制台走服务端自己的取消接口。**暂不需要，不是做不到。** |
| **答案 token 级增量（delta）** | ❌ **永久不做**（不是"v1 不做"） | 见下方专条。这是设计边界，不是取舍。 |
| **push notifications** | ❌ 不做 | Card 保持 `pushNotifications:false`。 |
| **进度百分比 / ETA** | ❌ 不做 | 事件数量本身不稳定（§4.2），任何百分比都是编的。 |
| **事件重投递 / at-least-once** | ❌ 不做 | 尽力而为。丢了就是丢了，靠 §5.1 的终态判据兜底。 |
| **`tasks/get`** | ❌ 不做 | **恒返回 `-32004`，与副本数无关。** v1.13 起服务端有了运行记录，但那是服务端自己的接口，不在契约面上；`tasks/get` 只有和请求去重搭配才有意义（断开不取消的请求才需要回来取结果），而 §5.4 之后断开就取消。服务端代码里它仍和 `tasks/cancel`、`tasks/resubscribe` 并列在同一张不支持表里（`src/ai_sql_agent/a2a/server.py` 的 `a2a_endpoint`）。**调用方不要依赖 `tasks/get`**，终态事件里已经有全部结果。 |

> **v1.5 改了 `tasks/get` 这一行的性质，不只是措辞。** v1.0–v1.4 把它写成
> "⚠️ 已知缺陷 / 多副本下会大概率 `-32001`"，暗示单副本下可用、多副本下不可靠。
> 代码里从来不是这样：它和 `tasks/cancel` 在同一张不支持表里，任何副本数下都是 `-32004`。
> 差别对调用方是实质性的——`-32001`（任务不存在）值得重试或降级，
> `-32004`（不支持）**永远**不该重试。写错的那版会诱导调用方写出一条永远走不通的分支。

### 7.1 为什么答案增量（delta）是**永久**不做

不是排期问题，是**会破坏调用方的守卫设计**。写在这里是为了防止以后有人"优化"时把它加回来。

调用方那侧的事实（已查实）：

- 它自己的 `event: delta` 是**它自己 LLM 的输出**，经 `PolicyStreamGuard` 按句放行后才进用户可见流；
- ~~**我方的委派结果在它那边的去向是 `grounding_sources`，不是 prompt 正文**~~
  ⚠️ **这条前提是错的（v1.5 查实）。**

> #### ⚠️ 缺口（v1.5）：上面第二条前提与代码不符
>
> **性质：契约要改。✅ 已关闭（v1.5，本版即改）**——错的是论证前提，不是结论；
> **结论「永久不做 delta」不变**，重新论证见 §7.2。下面保留原文可追溯。
>
> 实际是**两个都进**，不是"进 A 不进 B"：同一段 `order_text`
>
> - 先拼进 prompt 上下文：`ai-helpdesk/backend/api/main.py:676-678`
>   （`if order_text: context_parts.append(order_text)` → `full_context`）；
> - 再按 `order_is_evidence` 开关进 sources：`:698-700`
>   （`grounding_sources=[t for t in (knowledge_text, order_text if order_is_evidence else "") if t]`）。
>
> 也就是说 `grounding_sources` 不是"去向"，是**同一段文本的第二个用途**。
> 那个开关区分的也不是"进不进 prompt"，而是"能不能当出处"——
> 委派失败时的降级提示同样进 prompt 正文，只是不进 sources
> （`main.py:1032` 起的 `[业务系统查询未完成]` 分支返回 `(提示块, False)`；
> 设计说明在 `:987`，开关的理由在 `:695-696`「它是指令不是证据」）。
>
> **本节的结论（永久不做 delta）本身不受影响，但论证站在一条错的前提上，必须重写**——见 §7.2。

所以如果我方发 delta 而它转发：**未经 `PolicyStreamGuard` 的文本会直接落到用户可见流上**，守卫设计当场失效。我方的输出在它的架构里本来就不该走展示通道。

> **以上这段论证已由 §7.2 取代**（它依赖的第二条前提是错的）。**结论不变**，
> 重新论证见下节。这里保留原文是为了让"哪一版靠什么理由得出的结论"可追溯。

推论，一并钉死：**终态事件之前的任何事件都不含答案内容。** 进度事件只有 stage 标签和展示文案，artifact 一次性给全。任何未来想在终态前塞答案片段的改动，都要先回来推翻本节与 §7.2。

### 7.2 delta 结论的重新论证（v1.5）

§7.1 的结论——**永久不做 delta**——**保留**。但它原来的论证站在一条错的前提上
（"我方结果不进 prompt 正文"），所以整段重新写一遍。**先说结论为什么不受影响，
再说前提改了之后论证怎么走。**

#### 为什么"两个都进"不推翻结论

原论证是：*我方结果不进展示通道 → 所以我方也不该往展示通道发东西。*
真实情况是我方结果**同时**进 prompt 正文和 `grounding_sources`。
关键在于：**这两个都是 LLM 的输入侧，没有一个是展示侧。**

调用方的用户可见流只有一个入口——它自己 LLM 的输出，逐句过 `PolicyStreamGuard`
（`ai-helpdesk/backend/core/answer_policy.py:440`），由编排器消费
（`backend/agents/agent_orchestrator.py:344`、`:380`）。我方的文本无论进 prompt
还是进 sources，**都要先穿过一次它自己的生成，才可能被用户看见**。

所以正确的论证是：

> **我方的输出在调用方的架构里只有输入侧的位置，没有输出侧的位置。**
> delta 是一种**为了直通展示而存在**的东西——它唯一的价值就是"边生成边给人看"。
> 一段必须先进 prompt、等对方 LLM 重新组织过才能见人的文本，做成增量没有任何收益：
> 调用方要么攒齐了再用（那 delta 白发），要么真的转发（那就绕过了守卫）。

**前提改了，结论反而更硬。** 原前提（"只进 sources"）暗示我方文本在调用方那边
用途很窄，"不该展示"像是一条局部约定；查实之后知道它**同时是 prompt 正文**——
也就是说它是喂给对方模型的原料。原料被直接播给用户，比原来设想的更离谱。

#### 结论保留，但有一条边界要写清楚

原文"我方的输出在它的架构里本来就不该走展示通道"这句**仍然成立**，
只是理由换了：不是因为它去了 sources，而是因为**调用方的展示通道只接自己 LLM 的输出**。

同时补一条 v1.4 之前没人说过的话：**这个结论依赖调用方的架构，不依赖 A2A 协议。**
哪天调用方改成"把委派结果直接展示给用户"（比如做一个"数据原文"折叠区），
本节的论证就整段失效——那时该重新讨论，而不是发现"反正也没人转发"就默默加上。
**要推翻本节，先推翻的是「调用方的展示通道只接自己 LLM 的输出」这句话，
不是「delta 技术上能不能做」。**

`transport.py` 已有 `chat_stream`，对话路径技术上完全可以流式。**能做但不做**，这一条不变。

---

## 8. 落地要改的清单

对齐通过后按此执行。**本文档不含实现，以下只是范围界定。**

### 8.1 措辞（v1.5 按现状重写）

> **这一节从 v1.0 到 v1.4 一直挂着三条引文，而那三段话从来没进过任何提交。**
> v1.3 已经指出过（"§8.1 引用的原文从未进过任何提交——契约是对着一个远端没有的版本写的"），
> 但只写在变更记录里，条目本身没动，于是 v1.4 又照原样传了一版。
> v1.5 把它按现状改掉。
>
> 复核命令（2026-04-28，均无输出）：
>
> ```bash
> cd ai-sql-agent
> git log --oneline -S"秒级同步"   -- README.zh-CN.md ai_sql_a2a/__init__.py
> git log --oneline -S"seconds-scale"
> ```

**现状（2026-04-28 实测）：**

| 位置 | 现在是什么 | 还要不要改 |
| --- | --- | --- |
| `ai_sql_a2a/__init__.py` | 已无"秒级同步"的说法。现在写的是「本包实现 `message/send` 与 `message/stream`；`tasks/cancel`、`tasks/resubscribe`、push notifications 刻意不做（契约 §7）」 | ✅ 不用改（但引用的契约版本要更新，见下） |
| `README.md` | **完全没有提到 A2A 或流式**——`grep -ci stream README.md` → `0`，`grep -ci a2a README.md` → `0` | ⚠️ 要补 |
| `README.zh-CN.md` | 同上，`grep -ci 流式 README.zh-CN.md` → `0` | ⚠️ 要补 |

**所以剩下的不是"改口径"，是"README 里根本没有这块"。** 建议按 §2 的实测数据补一段
（"原来判断它是秒级同步操作，p90 17.7s 的实测推翻了这个判断"），
并明确保留 `tasks/cancel` / push notifications 不做的理由——那部分理由今天仍然成立。
两个 README 是互译关系，**必须同时补**，否则会出现"英文说支持、中文没提"。

**另外，两侧代码里引用的契约版本都过期了**（附录 B 要求"引用时带上 commit"）：

| 位置 | 现在引的 | 应该引 |
| --- | --- | --- |
| `ai-sql-agent/ai_sql_a2a/__init__.py:4` | 「（v1.3）」 | `v1.5` + tag/commit |
| `ai-sql-agent/tests/test_a2a_server.py:2` | 「（v1.3）」 | 同上 |
| `ai-sql-agent/ai_sql_core/settings.py:189` | 「契约 v1.3」 | 同上 |
| `ai-helpdesk/backend/tools/a2a_client.py:4` | 「（v1.3）」 | 同上 |
| `ai-helpdesk/backend/tools/sql_agent_client.py:9` | commit `9081030`（= **v1.2**） | 同上 |

### 8.2 代码（v1.5 按实际进度重写）

> **这张表原来是一份施工清单，v1.4 之后没有跟着更新，行号已经全部漂了**——
> 比如原文的 `ai_sql_a2a/server.py:296-299`（"把 `message/stream` 移出 `-32004` 列表"）
> 今天指向的是事件汇的 `sink` 函数，那一列已经在 `:359-362`，而且 `message/stream`
> 早就不在里面了。**一张行号全错的施工清单比没有更糟**：照着它去改会改到别的地方。
>
> 改成"做完没有 + 现在在哪"。行号为 2026-04-28 复核值。

| 位置 | 原计划 | 状态 |
| --- | --- | --- |
| `ai_sql_core/trace.py` 的 `stage()` | 加 ContextVar 事件汇，pipeline 侧零改动 | ✅ **已做**。`_stage_sink` 在 `trace.py:27`，`use_stage_sink()` 在 `:33`，`stage()` 在 `:173`；服务端消费点 `ai_sql_a2a/server.py:295-298` |
| `ai_sql_pipeline/text2sql_pipeline.py` | `normalization` 由裸 `log_event` 改成真 `stage()` | ❌ **未做**。仍在 `:245-257`，且只在 `changed` 为真时记。**这就是 §4.5 的缺口**（原文写的 `:209-221` 早已漂到别处） |
| 准入提前到流打开之前 | `require_api_key` + `limiter.check` | ✅ **已做**，但落点不是原文写的 `ai_sql_api/app.py`——在 `ai_sql_a2a/server.py:146-157` 的 `_admit()`，两条 transport 各调一次（`:268`、`:334`，后者注释写着"流打开之前"） |
| Agent Card | `capabilities.streaming` 由开关决定；`version` → `1.1.0` | ✅ **已做**。`ai_sql_a2a/server.py:92-96`、`:28` |
| `message/stream` 移出 `-32004` 列表 | `tasks/cancel`、`tasks/resubscribe` 留在里面 | ✅ **已做**。现在那张表在 `ai_sql_a2a/server.py:359-362`，`tasks/get` 也在里面（§7 已按实际改写） |
| 新增 stream handler | 事件构造 + 顺序保证 + 中途失败补终态 | ✅ **已做**。`_stream_events` 在 `ai_sql_a2a/server.py:282`，失败补终态在 `:322` |
| `AI_SQL_A2A_STREAMING` 开关 | 新增 | ✅ **已做**。`ai_sql_core/settings.py:184`（`a2a_streaming: bool = True`，默认开；`:185-197` 的 docstring 记了 v1.3 阻断解除的两次实机演练） |

> **v1.13 的措辞同步。** 进度事件的来源仍是 `trace.stage()` 的 sink，服务端没有改它。
> 服务端新增的运行事件日志（`ai_sql_runs` / `ai_sql_run_events`，按序号记一次请求的全过程）
> 是另一条旁路，**不在契约面上**：进度事件里不加 `run_id` / `seq`，`progress-event.schema.json` 不变。
> v1.13 在服务端动的三处（断开即取消、`canceled` 终态、`-32029`）见附录 A 的 v1.13 行。

**还没做的两件事**，都在 §4.6 里定义好了形态：

| 要改什么 | 为什么 |
| --- | --- |
| `ai_sql_api/result_summary.py` + `ai_sql_a2a/server.py` 的 `_artifact()` | 把 `execution_status` 与 `succeeded_steps` / `failed_steps` / `repaired_steps` 放进 data part。**后三个 `result_summary.py:93-94` 已经算好了**，只是 `server.py:203-211` 没带上；`execution_status` 按 §4.6.1 那张判据表现算即可，不用改 pipeline |
| `ai_sql_pipeline/text2sql_pipeline.py:245-257` | `normalization` 改成真 `stage()`（顺带修好"只在 changed 时记"这个观测盲区） |

### 8.3 测试（v1.5 按实际进度重写）

> 原表的两行也漂了：`tests/test_a2a_server.py:54-56` 现在是
> `test_card_reflects_the_streaming_flag`——**已经跟着开关走了**，不是原文说的
> "断言 `assertFalse(caps["streaming"])`"。

现状（2026-04-28 实跑：`cd ai-sql-agent && .venv/bin/python -m pytest tests/test_a2a_server.py -q` → **12 passed**）：

| 原计划 | 状态 |
| --- | --- |
| Card 断言跟着开关走 | ✅ `test_card_reflects_the_streaming_flag`（`:54`） |
| `message/stream` 不再恒返回 `-32004` | ✅ `test_stream_returns_32004_when_flag_is_off`（`:63`）改成了"**开关关掉时**才 `-32004`" |
| 事件顺序保证（§4.2） | ✅ `test_stream_event_order_is_guaranteed`（`:121`） |
| 终态唯一性 | ✅ 同上用例内 |
| 中途失败补终态（§5.2） | ✅ `test_mid_stream_failure_ends_with_a_failed_terminal`（`:166`） |
| send / stream 同构（§4.4） | ✅ `test_send_returns_a_completed_task_with_isomorphic_artifact`（`:90`）、`test_stream_and_send_share_one_audit_shape`（`:187`） |
| 准入失败是 JSON 不是 SSE（§3.2） | ✅ `test_stream_admission_failures_are_json_not_sse`（`:158`） |

**还缺的**，全部列在 `CONSUMER-TESTS.md` 里，两侧各自接：

> **⚠️ 下面这份清单写于 v1.5，其中两条已经过时，原文保留可追溯：**
>
> - **C 组那条的 `xfail` 指示是错的**，v1.6 写错、v1.7 改了还是错，v1.9 才改对
>   （理由见 CONSUMER-TESTS 的「`01-failed-step` 这一条被改过两次」）。
>   **v1.12 起 `dp-trust-01` 已改判到 B 组**：`execution_status` 提级为 `required`
>   之后它不再是合法载荷，断言从"业务判定"变成"schema 必须拒它，且理由就是缺这个字段"。
> - **E 组那条的断言集合已经换了**：v1.10 起断 `contract-declared` 而不是
>   `emitted-2026-09-04`，`normalization` 已经在发了（§4.5 缺口框）。
>
> 以 `CONSUMER-TESTS.md` 的当前内容为准，这份清单只反映 v1.5 当时的缺口分布。

- **B 组**：schema 反例必须被拒，且回落方向是"不可信"（§9.1 缺口的回归测试）；
- **C 组**：信任反例。其中 `dp-trust-01`（唯一一步失败但三布尔全"干净"）
  **在 `execution_status` 上线之前应当是红的**，标 `xfail`，不要删也不要放宽断言；
- **E 组**：`stage` 实发集合断言用 `emitted-2026-09-04` 而不是 `contract-declared`，
  差的那一个就是 `normalization`；
- **D 组**：`database` 标识符断言**形状**（`^[a-z][a-z0-9_]*_db$`），再用 `current-*` 断言"实际发什么"、`known` 断言"调用方能认多少"（v1.6：契约面已由枚举降为形状 + 可拉取清单，两者不再是同一件事）。

### 8.4 容量（不改代码，但要知道）

> **v1.5 修正：下面的 2.3 QPS 是推算，不是实测；而且它只管 `/query`，不管 A2A。**
> 原文把它写得像一个测出来的系统上限，两处都不准。

**先说 `/query`（这条路上原来的数字是对的，但要标清楚它是怎么来的）：**

`/query` 是同步 `def`（`ai_sql_api/app.py:461`），FastAPI 会把它丢进 **anyio 的默认线程池**。
那个池子的容量是 **40**，可实测：

```bash
cd ai-sql-agent && .venv/bin/python -c '
import anyio.to_thread, asyncio
async def m(): print(anyio.to_thread.current_default_thread_limiter().total_tokens)
asyncio.run(m())'
# → 40.0
```

一次查库 17.7s 占住一个线程 17.7s，**40 ÷ 17.7 ≈ 2.3**——这是一次**除法**，
不是一次压测。它假设每次查询都恰好 17.7s（实际 p50 6.6s、max 68.3s，§2）、
线程池百分百有效利用、没有别的同步端点抢线程。**真要一个能写进容量规划的数，得压测。**

**再说 A2A（原文完全没提，而这是流式实际走的路）：**

`message/send` 与 `message/stream` 的 handler 都是 `async def`，pipeline 经
`asyncio.to_thread` 下沉（`ai_sql_a2a/server.py:271`、`:303`）。
**`asyncio.to_thread` 走的不是 anyio 那个 40 线程池**，而是事件循环的默认
`ThreadPoolExecutor`，容量是 `min(32, os.cpu_count() + 4)`——**跟机器走**。
在写这段时那台 12 核开发机上是 **16**：

```bash
python3 -c 'import os; print(min(32, (os.cpu_count() or 1) + 4), os.cpu_count())'
# → 16 12
```

而且流式路径**每条在飞的流占两个线程**：一个跑 pipeline，一个阻塞在
`loop.run_in_executor(None, events.get)` 上等事件（`ai_sql_a2a/server.py:312`）。

| 路径 | 线程池 | 该池容量 | 每请求占用 | 同时在飞的上限 |
| --- | --- | --- | --- | --- |
| `/query` | anyio 默认 | 40（实测） | 1 | 40 |
| `message/send` | asyncio 默认 executor | `min(32, cpu+4)`（12 核机上 = 16） | 1 | 16 |
| `message/stream` | 同上 | 同上 | **2** | **8** |

**含义：流式路径的并发上限比 `/query` 低得多，而且随机器核数变。**
按 17.7s 算，12 核机上流式约 **0.45 QPS**，`/query` 约 **2.3 QPS**——差五倍。
压测和容量规划**必须分路径做**，不能拿 `/query` 的数字替 A2A 背书。

当前默认限流是 60 次/分/调用方（`ai_sql_core/settings.py:199`，1 QPS），
**一个调用方跑满就能压垮流式路径**——这一条 v1.4 之前没人算过。

> **2.3 QPS 与 0.45 QPS 都是单副本数字，不是硬上限。**
> 真正的硬约束是同步 pipeline + 模型延迟，不是 40 或 16 这两个数。
>
> **v1.5 更正一处引用错误：** 原文写「共享限流/熔断（`ai_sql_core/shared_state.py`）
> 落地之后多副本才第一次真的能起」。**那个文件在 ai-sql-agent 里不存在**
> （`find ai-sql-agent -name 'shared_state*'` 无命中）。
> 同名文件在**另一个仓库**：`ai-helpdesk/backend/core/shared_state.py:22`，
> 是调用方给自己的限流/熔断做的 Redis 共享状态，**解决不了服务端多副本的问题**。
>
> 服务端要多副本，仍然需要把限流与熔断挪到共享存储上——那件事**还没有代码**，
> 也没有对应文件。**这一条按"待办、无实现"记，不要再引一个不存在的路径。**
> `docker compose up -d --scale app=2` 这类扩容动作在共享限流落地之前会得到
> "每个副本各限各的"——即实际限流阈值被副本数乘大，这是个安全问题不是性能问题。

---

## 9. 已对齐的问题（草案 v0.1 §9 的答复）

调用方已从自己代码里查实并答复，逐条记录在此——这些是契约的**依据**，
以后要改契约先看这里的前提是否还成立。

| # | 问题 | 答复 | 对契约的影响 |
| --- | --- | --- | --- |
| 1 | 超时后我方不中止、照常计费计配额，能接受吗？有重试吗？ | **能接受，且调用方没有查询重试。** 唯一的 backoff 是 Agent Card 发现（30s）；查询失败一律静默回落，不重试。**后续升级**：超时预算由固定 25s 改为自适应（静默 20s / 总长 90s），理由正是"宁可多等也别烧配额"。**v1.2 修正**：自适应仅限流式，`send` 走固定 90s（§1） | §5.4 的浪费降为极端长尾；§1 事实③、§2、§3.4 一并改口径 |
| 2 | 读到 `-32004` 能干净退回 `send` 吗？ | **能。** v1.4 实现并做了实机演练（陈旧 Card → `message/stream` → `-32004` → 自动改发 `send`）。此前 v1.2 虚报、v1.3 推翻 | §6.2 的 ⚠️ 阻断**解除**，开关默认开 |
| 3 | stage 集合够不够？ | **全发。** 展示与否由调用方前端决定——少发它永远拿不到，多发只是几行忽略逻辑 | §4.5 九个 stage 全部发送，`normalization`/`validation` 照发，只在"建议"列标注不展示 |
| 4 | `database` 字段谁做中文映射？ | **调用方自己映射**，它已有自己的文案 | §4.5 把四个库标识符**提升为契约面**，与 stage 值同级 |
| 5 | 要不要转发我方的答案 delta？ | **不要，且永久不要。** 它的 `delta` 是自己 LLM 经 `PolicyStreamGuard` 按句放行的输出；我方结果在它那边进的是 `grounding_sources`，不是展示通道 | §7.1 从"v1 不做"改写为**永久决定**，并写进理由 |

### 9.1 实现时必须守住的两条（调用方补充确认）

1. **artifact 的 data part 三个字段一个都不能少。** 调用方的信任门槛是
   `queried_database == True` **且** `has_risk == False` **且** `degraded == False`
   三者同时满足才把结果放进 `grounding_sources`。流式下这三个字段必须仍在
   artifact 里——这正是 §4.4"与 `message/send` 同构"的实际用途，不是形式要求。
   少一个字段，调用方的判定就会静默变成"不可信"，表现为委派白做了。

   > #### ⚠️ 缺口（v1.5）：这一条有三处不成立，其中一处方向反了
   >
   > **性质：(a)(b) 实现待办；(c) 契约要改。**
   >
   > **✅ 已关闭（v1.10）**：ai-helpdesk 新增 `backend/core/evidence.py`
   > （`40be899`，已随 `main` 推送至 `e594655`）——`strict_bool()` 只认 JSON 布尔（治 b）、
   > `assess()` 按 §4.6.7「缺失即不可信」处置（治 a）、`main.py` 的闸改为
   > 风险/降级/缺字段一律不进 `grounding_sources`（治 c），三个承载者共用同一个 `assess()`。
   > 本机复现：契约 B 组（缺字段 / 字符串布尔）与 C 组（形状合法但不可作出处）
   > **75 passed / 1 xfailed**。下面保留原文可追溯。
   >
   > **(a) 缺字段不是"变成不可信"，两个字段是"变成可信"。**
   > `ai-helpdesk/backend/tools/a2a_client.py:250-266` 的 `_normalize()` 对三个字段
   > 一律 `bool(data.get(k, False))`。对 `queried_database` 这是 fail-safe（缺 → `False` → 不采信），
   > 但对 `has_risk` / `degraded` **方向是反的**：缺 → `False` → 判成"无风险、未降级" → **采信**。
   >
   > **(b) `bool()` 不校验类型。** 任何非空字符串都为真，`"false"` → `True`。
   >
   > 2026-04-28 复现（可直接重跑）：
   >
   > ```bash
   > cd ai-helpdesk && backend/.venv/bin/python -c '
   > import sys; sys.path.insert(0,"backend")
   > from tools.a2a_client import A2AClient as C
   > art=lambda d:{"parts":[{"kind":"data","data":d}]}
   > print("缺 has_risk/degraded ->", {k:C._normalize(art({"queried_database":True}),{})[k]
   >       for k in ("queried_database","has_risk","degraded")})
   > print("字符串 \"false\"    ->", {k:C._normalize(art({"has_risk":"false"}),{})[k]
   >       for k in ("has_risk",)})'
   > ```
   > ```
   > 缺 has_risk/degraded -> {'queried_database': True, 'has_risk': False, 'degraded': False}   ← 门槛通过
   > 字符串 "false"        -> {'has_risk': True}                                                 ← 与字面相反
   > ```
   >
   > **(c) 真正的门槛根本不是三个布尔的与。** 上面这句"三者同时满足"是契约自己写错的：
   > `ai-helpdesk/backend/api/main.py` 里只有 `queried_database` 是闸——
   > `:1041` `if not data.get("queried_database"): return "", False`；
   > 而 `:1062` 的 `has_risk` 和 `:1067` 的 `degraded` **只往上下文里追加一段提示文本，
   > 函数照样 `return …, True`**，结果照样进 `grounding_sources`。
   >
   > 这个设计未必是错的——把带风险的数据连同风险提示一起交给 Agent，可能比整段丢掉更好。
   > 错的是**契约把它描述成了一道三重闸**，于是我方一直以为"少发一个字段最坏是白做工"，
   > 实际最坏是**带风险/降级的数据被当成可信出处**。
   >
   > **建议**：
   > 1. 调用方：`_normalize` 改成显式校验——三个字段必须存在且是 JSON 布尔，
   >    缺失或类型不对按 **`has_risk=True` / `degraded=True` / `queried_database=False`**
   >    处置（**缺失 = 不可信**，不是默认 `False`）。用例见 `CONSUMER-TESTS.md` 的 **B 组**（`fixtures/data-part/schema-invalid/`，六个反例都必须被拒，且回落方向必须是「不可信」）。
   > 2. 双边：重写上面这段"信任门槛"的描述，让它等于代码真正做的事；
   >    要不要把 `has_risk`/`degraded` 升成真闸，是一个独立决定，写清楚再改。
   > 3. 结构性的那一半在 §4.6：光靠三个布尔判不出"部分成功"和"结果被截断"。

2. ~~**§8.4 的 2.3 QPS 是单副本数字**，多副本是缓解手段，不是硬上限。~~
   **v1.5 更正**：这句话本身没错，但它掩盖了两件更要紧的事——2.3 是 `40 ÷ 17.7` 的**推算**
   而不是实测，而且它只管 `/query`；A2A 流式路径走的是另一个线程池，同机上限约 **0.45 QPS**。
   另外原文提到的 `ai_sql_core/shared_state.py` **不存在**。详见改写后的 §8.4。

---

## 10. 版本边界：本契约约束的是 A2A **0.2 子集**

**本契约不宣称与最新版 A2A 兼容。** 它约束的是我方今天实现的那个 0.2 形状的子集，
仅此而已。这一节写下来是为了防两件事：一是有人拿"我们支持 A2A"去对接一个 v1.0 客户端；
二是有人为了"跟上规范"去改版本字符串——**那没有任何意义**，见下。

> **本节的引用约定：** `§X` 一律指**本文**的小节；引用 A2A 官方规范时写成
> 「规范 N 节」「规范附录 A.2.1」，不带 `§`。`tools/check_contract.py` 的链接检查
> 会把每一个 `§X` 当成本文的小节去核对，写混了会误报，也会让读者分不清在引谁。

### 10.1 事实

| 项 | 值 | 证据 |
| --- | --- | --- |
| 我方 Card 声明的 `protocolVersion` | `"0.2"` | `ai_sql_a2a/server.py:27` |
| 我方 Card 的 `version` | `"1.1.0"`（**这是 Agent 自己的版本，不是协议版本**） | `ai_sql_a2a/server.py:28`、§6.1 |
| A2A 官方最新已发布版本 | **1.0.0**（此前 0.3.0 / 0.2.6 / 0.1.0） | <https://a2a-protocol.org/latest/specification/>，2026-04-28 取 |
| 我方用的是官方 SDK 吗 | **不是，手写实现** | `grep -i a2a ai-sql-agent/requirements.txt` 无命中 |

手写实现这一点是好消息也是坏消息：好在没有 SDK 版本被动升级的风险，
坏在**规范演进不会有任何编译期信号**——只能靠这一节人工盯。

### 10.2 为什么改版本字符串没有意义

把 `protocolVersion` 从 `"0.2"` 改成 `"1.0"` 只会让 Card 说谎。
0.2 → 1.0 是**结构性**变更，不是版本号递增：

| 变更 | 对本契约的影响 | 来源 |
| --- | --- | --- |
| **`kind` 判别符被移除**（规范附录 A.2.1「Kind Discriminator Removed」）。v1.0 不再靠 `kind` 区分 Part / Artifact 类型，改为看哪个字段被填充 | **伤筋动骨。** 本契约整套事件模型都建在 `kind` 上：§4.1 的 `result.kind` ∈ {`task`,`status-update`,`artifact-update`}、§4.3 的 `{"kind":"message"}`、§4.4 的 `{"kind":"text"}` / `{"kind":"data"}`。`schema/` 下四个 schema 也全是 `kind` 形状 | 规范附录 A.2.1 |
| **流式响应改为 `StreamResponse` 包一层**，内含 `task` / `message` / `statusUpdate` / `artifactUpdate` 之一，而不是一个带 `kind` 的对象 | §4.1 的信封形状要重写；§4.2 的顺序保证要按新形状重述 | 规范 4.2 节 Streaming Events |
| **方法名换了一套。** v1.0 侧文档用 `SendMessage` / `SendStreamingMessage` / `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask` / `GetExtendedAgentCard` | §3.1 的 `message/stream`、§7 的 `tasks/get` 等 JSON-RPC 名字全部要重新对照。**具体的线上方法名映射本次未取到全文，标为「待核实」** | 规范 9.4 节 Core Methods（**待核实**） |
| **Extended Agent Card 字段位置改了**（附录 A.2.2），改为经 `GetExtendedAgentCard` 单独取 | 我方没用 extended card，**影响为零**——但如果以后要用，别照 0.2 的形状加 | 规范附录 A.2.2 |
| Agent Card 新增 `securitySchemes` / `security`、`AgentInterface` 的 `tenant` | 我方鉴权走 `X-API-Key`（§3.2），没有声明在 Card 里。迁移时这是**要补的**，不是要改的 | 规范 §Agent Card（**部分待核实**） |

> **上表的取用方式：** 带「待核实」的两行是本次抓取没能拿到完整原文的部分，
> **按线索处理，不要当结论**。要动手迁移之前，回到
> <https://a2a-protocol.org/latest/specification/> 的附录 A 与 9.4 节逐条核对，
> 并以 `spec/a2a.proto` 为准——规范自己写明 proto 文件才是结构定义的权威来源。

### 10.3 现在怎么办

**什么都不做，但把边界写明。** 理由：

1. **今天的两个对端都是我们自己的。** 服务端 `ai-sql-agent`、调用方 `ai-helpdesk`，
   没有第三方 A2A 客户端接进来。0.2 形状对双方都能用，迁移收益是零。
2. **迁移的代价不是改版本号，是重做事件模型。** 见上表第一、二行。
3. **但"没有第三方"是一个会变的前提。** §6.1 明确设想过"通用 A2A 客户端库，
   见 `streaming:true` 就自动优先 stream"——那种客户端如果是照 v1.0 写的，
   **接过来会直接解析失败**，而且失败点在事件解析，不在握手，报错会很难懂。

所以：

- **Card 保持 `protocolVersion: "0.2"`。** 它现在是诚实的，改了就不是。
- **对外描述统一为「实现 A2A 0.2 的一个子集」**，不写"支持 A2A"、不写"兼容 A2A"。
- **本节就是迁移矩阵的落点。** 哪天真要迁 v1.0，从 10.2 那张表开始，
  先把「待核实」两行核实掉；§4.1 / §4.2 / §4.4 / §7 与 `schema/` 全部要跟着改，
  那是一次**大版本**契约变更，走 §6，两侧同时改。
- **不要为了"看起来跟上了"而单独改版本字符串。** 那会把一个诚实的
  "我们只做了 0.2 的一小块"变成一个说谎的 Card，而 Card 是对端唯一的自动发现入口。

---

## 附录 A. 变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1.13 | 2026-06-09 | **三项双边变更：断开即取消（§5.4）、`canceled` 终态（§4.2 / §4.4 / §5.1）、429 改用 `-32029`（§3.2 / §5.5）。**<br>**起因：** 服务端加了每次请求的运行记录和取消检查点（阶段边界、模型调用之间、SQL 执行中），从此**能**在调用方断开后停下，而 v1.12 §5.4 写的是"不会停"。这一条不改，服务端只能一直关着这个能力，否则就违反契约。<br>**§5.4：** 三个承载者（`message/stream`、`message/send`、直连 `/query`）都在下一个检查点停下；已经发出去的那一次模型调用打断不了，上限是模型调用超时。实测 6 次取消，停下用时 0.07–2.7s，每次都等于在途调用剩下的时间。审计照写（499），配额照计一份，§5.5 不变。服务端开关 `AI_SQL_CANCEL_ON_DISCONNECT` 默认开，关掉就回到 v1.12 的行为。<br>**§4.4：** `canceled` 只在流打开期间、被本连接以外的途径取消时发（服务端取消接口、运维、关停）；本连接自己断开时发不出去。拼写跟 A2A 0.2 规范走（`canceled`），`metadata.error` 照带，形如 `cancelled: api`；send 返回的 Task 用同一套取值。v1.12 之前同样的情况发 `failed` + `cancelled: …`。<br>**终态第一次有了 schema：** 新增 `schema/terminal-event.schema.json`（状态 ∈ `completed` / `failed` / `canceled`），以及 `fixtures/terminal-event/` 三正两反；F 组从此有 fixture。`tools/check_contract.py` 新增两条断言：终态枚举恰为这三种，F 组至少有一条用例。<br>**§3.2 / §5.5：** 限流、每日配额、流式并发准入满了，统一回 429 + `-32029`（带 `Retry-After`）。原来的 `-32001` 在 A2A 规范里是 TaskNotFoundError，§7 自己也按这个意思用它。<br>**§7：** `tasks/cancel`、`tasks/get`、`tasks/resubscribe`、断线续传仍返回 `-32004`，只把理由从"做不到"改为"暂不需要"。**§8.2：** 补一句措辞同步：运行事件日志是旁路，不在契约面上，进度事件不加 `run_id` / `seq`。<br>**不变的部分：** delta 永久不做；`progress-event.schema.json`、data part、三承载者载荷同构、`protocolVersion` `0.2` 都不变。<br>**两侧同步：** ai-sql-agent 两处 pin、ai-helpdesk 三处 pin 由 `v1.12` 改到 `v1.13`。服务端 `5f1d154` 实现三项；调用方 `5a1aa66` 不改代码，只改 pin，并补终态 schema 与 `canceled` 的消费用例。**判据仍是降级的**：本机按两侧 CI 入口复现全绿（数字见状态行）；本版推送后的 GitHub CI 结果写不进 tag。 |
| v1.12 | 2026-05-08 | **§4.4 的最后一步做掉了，七条缺口现在是真的全关。**<br>**这是一次 §6 契约变更**，不是纯文字版：`schema/artifact-data-part.schema.json` 的 `required` 新增 `execution_status`。v1.9 特意把这条缺口的关闭判据挪到契约仓库这一侧（「提级为 `required` + 现存 fixture 补齐该字段，那一个提交就是证据」），图的是不依赖任何一侧的测试标记；但那一步一直没做，而 v1.10 / v1.11 的状态行都写着「七条**全部**关闭」——2026-05-06 先发了一版只改文字的更正（`444c242`、`d6e6087`），本版把动作补上。<br>**`dp-trust-01` 由 C 组改判到 B 组，这是本版唯一的语义分叉，值得记住。** v1.9 设计 C 组时让它**故意不带** `execution_status`，用来覆盖 §4.6.7 的推导路径（`status_source == "derived"`），并写明「如实断言，这是正确期望，不是缺陷……任何时候都不许放宽」。而同一份 CONSUMER-TESTS 里的关闭判据又要求「现存 fixture 补齐该字段」——**补了它，C 组这条覆盖就没了；不补，提级后它就是非法载荷**。两句话在同一份文档里打架，v1.9 写判据时没注意到会撞掉自己刚设计的分组。本版的取舍：**不补字段，改判到 B 组**，它从「缺字段会被静默推导」的样本变成「缺字段会被挡住」的反例，唯一的拒绝理由就是缺这个字段；C 组 `failed` 这一档由 `dp-trust-07` 接手（它本来就带着）。**文件路径没动**——两侧按字面路径读它，挪文件会红在别人仓库里（附录 B 的硬编码路径清单）。<br>**十五份 fixture 补了字段**，值按 §4.6.1 的判据逐份算（`dp-valid-02` 走对话路径 → `not_applicable`；`dp-trust-02` 一成一败 → `partial_success`；`steps == []` 的几份 → `failed`）。<br>**六个既有反例逐条重验了拒绝理由。** 多一条必填就是给每个反例多一个被拒理由，「这条反例真的在保护它声称保护的东西」会因此悄悄失效——这是 v1.11 那次更正就预判到的坑。六份都补了该字段，实测各自仍只因原来那一个理由被拒；`tools/check_contract.py` 新增一条断言把它守死（除 `dp-trust-01` 外，任何反例的拒绝理由里出现 `execution_status` 即失败），另新增「`execution_status` ∈ `required`」的断言作为本条缺口的机器判据。<br>**§4.6.7 的过渡判据对这个字段作废**（本节最后一句本来就写了「提级即作废」）。作废的是「推导出来的结论可以照常采信」，**不是推导本身**：服务端出 bug、版本错配、中间件吞字段都会让不合规载荷真的出现，那时仍要靠 `steps[]` 说清楚「查挂了」而不是崩掉或静默采信，所以两侧的推导代码都保留，只是结论一律按不可信处置。`result_truncated` 等其余 🔨/➕ 字段的过渡判据不受影响。<br>**两侧同步**：ai-helpdesk 三处 pin、ai-sql-agent 两处 pin 由 `v1.11` 改到 `v1.12`；两侧各有针对 `dp-trust-01` 的断言要跟着改判（详见两仓库本版提交）。**判据仍是降级的**——GitHub CI 至今一次没跑过（账号级 Actions 停用），本版同样只能靠「提交可复核 + 本机按各自 CI 入口复现全绿」。 |
| v1.11 | 2026-05-06 | **补一个自洽的 tag，让下游能真的钉住。**契约面与 v1.10 **逐字节相同**（`schema/` 与全部 fixture 载荷零改动），本版不是 §6 契约变更，两侧无需改消费代码。<br>**起因**：tag `v1.10` 的 `fixtures/index.json` 里 `contract_version` 停在 `v1.9`——三笔「v1.10 补正」（`70fb56d` 查到 Actions 停用的确切原因、`77cadca` 补上版本一致性自检、`75eafe8` 记录 ai-sql-agent 已合入 `main`）都落在打完 tag 之后。下游按 `contract_version` 判断「拉到的是不是钉的那一版」，**所以两侧把 pin 从 v1.8 改到 v1.10 会红一条**，而红的不是契约面，是版本号。<br>**为什么不移动 `v1.10`**：它已经推送到 GitHub（`git ls-remote --tags origin` 可验）。附录 B 的规矩是「钉住的引用必须一直取得到当时那一份」——移动已发布的 tag 会让这条规矩失效，而这个仓库上一版刚刚为了同一个理由用 merge 而不是 squash 合的 PR。**宁可多一个 tag，不动已经发出去的那个。**<br>**为什么不是 `v1.10.1`**：`tools/check_contract.py` 的版本正则是 `v\d+\.\d+`，对 `v1.10.1` 会截出 `v1.10` 再与 `index.json` 比对，自检反而会红——这个仓库的版本约定就是两段式，补丁号没有位置。<br>**两侧同步**：ai-helpdesk 三处（`backend/tests/test_contract_fixtures.py` 的 `PINNED_TAG`、`.github/workflows/ci.yml` 的 `ref`、`scripts/ci-local.sh` 的 `CONTRACT_TAG`）、ai-sql-agent 两处（`tests/test_contract_fixtures.py` 的 `CONTRACT_TAG`、`.github/workflows/ci.yml` 的 `A2A_CONTRACT_TAG`）全部由 `v1.8` 改到 `v1.11`，**跳过 v1.9 / v1.10 两版**：v1.9 新增的 `dp-trust-07` 两侧早已兼容（v1.10 的复核已实测过），所以这是一次纯粹的版本号对齐。<br>**仓库整理（本版一并做掉，不动契约面）**：`git diff v1.10 v1.11 -- schema/ fixtures/` 仍然只有 `index.json` 那一行，以下四项都在契约面之外。<br>**① 版本一致性自检由「一处」扩到「五处」。** v1.10 补的那条只比 `fixtures/index.json`，而真正漂掉的是它没看的地方：`CONSUMER-TESTS.md` 抬头的「配套：…（v1.5）」**活过了 v1.6–v1.10 五个版本**，末尾的 `--branch v1.9` 也停在两版之前。**调用方早在 v1.6 就把前一条写进交回契约的反馈清单，写了也没人改——因为没有任何机器在看那一行。** 现在 `tools/check_contract.py version` 按**形态**收全五处：两张版本表的最新行、`contract_version`、`--branch` 示例、文档抬头的「配套：」。五处都做过反向验证（故意改坏，确认每一处都真的报错）——附录 B 自检表第 5 条，也是 `fixtures/README.md` 那条「反例必须真的被拒」用在自检脚本自己身上。<br>**为免给下一个人挖坑**，该段只看**会被复制粘贴去执行的行**（Markdown 的 ``` 围栏内、YAML 的非注释行）：附录 A 这类叙述历史的正文会原样写出旧版本号，照字面扫全文会把「把 `--branch v1.11` 改成 v1.12」这种正确的变更记录判红。<br>**② CI 里那段 bash 版本检查删掉，判据收归 `tools/check_contract.py` 一处。** 它当时只比「状态行 vs 附录 A」两处，而漂掉的正是它没覆盖的第三、第四处——**判据分散在两个地方，就等于没有一个地方知道判据的全集。**<br>**③ README 新增「目录结构：这几个路径是被下游硬编码的，不要重排」**，列出两侧按死路径读的四类位置（`schema/*.schema.json`、`fixtures/index.json`、同级目录回退 `../a2a-contract`、CI 的 `.contract/fixtures/index.json`）。重排目录是本仓库风险最高的操作——它一次性动掉所有引用，且红在别人的仓库里。真要做，那是一次三仓库同步变更，走 §6。<br>**④ 升版本流程补上「打 tag 必须是这一版的最后一个动作」**，附 v1.10 的原始病历（打完 tag 又补三笔 ⇒ tag 里的版本号永远停在上一版 ⇒ 只能补新 tag）。另新增 `.gitignore`，清掉 `fixtures/` 下两个混在夹具目录里的空 `.claude/.cc-writes/` |
| v1.10 | 2026-05-05 | **七条缺口全部关闭；判据如实降级并写明。**<br>两侧提交已推送：ai-helpdesk `main` = `e594655`（17 笔）、ai-sql-agent 分支 `feat/p0-authorization-and-business-outcome` = `6ab26e6`（11 笔，PR heyemlee/ai-sql-agent#1）。本机按各自 CI 入口复现：helpdesk 后端 675 passed / 2 skipped / 1 xfailed、契约用例 75 passed / 1 xfailed、前端 48 passed、**生产构建成功**；sql-agent **`Ran 550 tests … OK`**（真实 PostgreSQL 16）；契约自检 25 个 fixtures 全过。<br>**§4.5 的 normalization 缺口用机器判据关闭**：`schema/stage.schema.json` 新增 `emitted-2026-09-08`（九个，等于 `contract-declared`），`tools/check_contract.py` 的断言由「实发必须是声明的真子集」改为「当前实发必须等于声明」——**改这一组的这次提交就是关闭证据**。历史组 `emitted-2026-09-04`（八个）保留做对照且仍断言为真子集，不许被「修」成九个：它是那条缺口存在过的证据。<br>**⚠️ 判据是降级的。** 原计划是「两侧 CI 在 GitHub 上绿一次」，**那件事一次都没发生**——三个仓库（含本仓库一个刚建刚推、GitHub 确实读到了的 workflow）的 repo 级 Actions 全是 `enabled: true`、workflow `active`，而 run 数全是 **0**。指向账号级 Actions 策略 / 计费，仓库里改不动。实际判据因此是**「提交可推送复核 + 本机按 CI 入口复现全绿」**：**它证明了检查内容是对的，没证明检查会被强制执行**——这些门禁目前不是 required check，带着红的东西也照样能合。状态行下方加了这条声明，**CI 恢复之前不许删**。<br>**新增「怎么重新验一遍」**：三条可直接跑的命令，外加实测踩到的三个坑——① ai-sql-agent 必须在**没有 `.env`** 的环境里跑（它的配置隔离写在 `tests/conftest.py`，是 **pytest fixture**，而 CI 入口是 `unittest discover`，**unittest 不加载 conftest**；本机有 `.env` 时实测 29 failures + 3 errors，断言变成了「你的 `.env` 配了什么」）；② PostgreSQL 必须映射到宿主机 **5432**（`test_ops_readonly.py` 的只读角色 DSN 端口写死）；③ 契约 tag 要与被测仓库钉的一致——给 ai-sql-agent（钉 v1.8）喂 v1.9 时**只有版本断言一条失败、其余全过**，包括 v1.9 新增的 `dp-trust-07`，说明消费端已兼容，只差改 pin |
| v1.9 | 2026-05-04 | **改掉一个改过两次还是错的地方；澄清两个来源标签的归属；记录两侧都已提交。**<br>**错：`dp-trust-01` 的进度标记挂错了对象。** v1.6 说「这条应当是红的，标 xfail」——错，按 §4.6.7 的过渡判据业务结论今天就能判对，会 XPASS。v1.7 改成「业务结论必过 + 判定来源标 `xfail(strict=True)`」——**还是错，而且更隐蔽**：那条断言的输入是一份**冻结的 fixture**，里面没有 `execution_status`，服务端上线之后真实流量的 `status_source` 变成 `contract`，**而这份 fixture 永远是 `derived`**。于是 xfail 永远不 XPASS，「摘掉它」这个条件永远不成立。两侧实现时都撞上了，服务端写进了反馈。<br>**根因**：fixture 是「契约长什么样」的样本，**不该同时承担「实现做到哪一步」的进度标记**;一份冻结的载荷能证明判据对不对，永远证明不了服务端今天发了什么。两件事绑在同一份文件上就会撞车。<br>**v1.9 按断言对象重分**：fixture 断判据（`dp-trust-01` 如实断 `derived`、新增 `dp-trust-07` 同载荷带字段断 `contract`，**两条今天就该全绿**）；进度只挂在**各侧自己真实产出的载荷**上（只有活载荷会随实现改变）。并把缺口关闭的判据挪成一个**机器可验的契约仓库动作**——`execution_status` 在 schema 里由可选提级为 `required`、现存 fixture 补齐该字段，那一个提交就是证据，`tools/check_contract.py` 能验，不依赖任何一侧的测试标记，也不会因为谁 rebase 而失效。<br>**澄清（§4.6.5）**：`currency_source` 与 `status_source` 都是**调用方给自己结论贴的来源标签**，**服务端不发**——服务端不知道调用方的部署配了什么，它只保证「判得出币种就下发 `currency`/`column_units`，判不出就不发」。服务端回传一个恒为 `contract` 的字段没有信息量。<br>**进度**：七条缺口里五条的实现**两侧均已提交**（ai-helpdesk `main` 17 笔、ai-sql-agent 分支 11 笔），**但都未推送**——本地提交第三方复核不了，所以性质由「待提交」改为「已提交待推送」，仍不写「已关闭」。同理 `emitted-*` 暂不改为九个：那个提交（`8340323`）在一条未推送的分支上，rebase 会换 hash |
| v1.8 | 2026-05-01 | **契约范围修正：把跑生产流量的那条路纳进来。** 调用方交回的两条反馈，核过都成立，都是契约的问题。<br>**一：直连 `POST /query` 从来不在契约里，可它是 `SQL_AGENT_TRANSPORT` 的默认值，也是整条 A2A 关掉时的落点。** 契约叫「A2A 流式契约」，于是把非 A2A 的那条当成了别人的事;而 §6.2 的回滚承诺依赖它。已经造成一个**静默**缺陷：`/query` 把同一个 `StepLogResponse` 序列化成 `step_logs`，A2A 那两条叫 `steps`——调用方按 §4.6.7 处理缺失字段，`steps` 取不到 ⇒ 「缺失即不可信」⇒ **每一次直连查询都不再是出处，没有报错、没有降级事件、没有任何症状**，而 §6.2 的回滚恰好把流量切到这条路上。<br>新增 **§4.4.1**：契约面是 data part 本身（字段名 + 语义 + §4.6 判据），与谁承载无关；三个承载者列表；§4.4/§4.6/§9.1 适用于全部三个，§3/§4.1–4.3/§4.5 只适用于 A2A 那两条。**处置是改名不是加翻译表**——服务端把 `/query` 的 `step_logs` 改名为 `steps`，过渡期两个同时发，调用方两个都认、`steps` 优先，弃用走 §6。承认第三种形状等于把分叉制度化：同一个对象背两个名字，此后每一个新消费方都要重踩一次，而契约永久背着一张翻译表——翻译表是漂移的温床。<br>**二：§4.6.5「不得回退到自己的全局常量」过头了。** 在 `currency` 还没实现的今天，照它执行等于删掉 `SQL_AGENT_CURRENCY`——而那个常量当初是为了堵一次已发生的事故（下游只返回数字，中文回答默认写「元」，接美国厂的库时报给 builder 的金额带错币种）。**删掉它不会让语言的默认值消失，只会让它变回隐式。** 已改为按来源三分并要求把来源写进句子（`currency_source`：`contract` / `deployment_config` / `unknown`，与 §4.6.7 的 `status_source` 同构）：数据声明的可以当事实；部署配的可以用但必须写明「这是部署配置，不是查询返回的字段」；两者都没有时**主动禁止**语言默认值——**第三档是一条指令，不是一条禁令，因为沉默正是事故的形态**。<br>由此得到一条可推广的判据，写进 §4.6.5：**凡是「缺了就别说」的规则，都要检查一遍「不说」会不会被某个默认值填上；会的话规则要写成「说什么」，不能只写「别说什么」。**<br>同步：CONSUMER-TESTS 的硬规则由「两条 transport」改为「三个承载者」并给出 `/query` 的取值位置与过渡期注意事项；README 版本表补全 v1.6–v1.8（此前只有 v1.5）、引用示例钉到 v1.8；fixtures 版本号 → v1.8。<br>本版**只改契约与本仓库资产**，没有动另外两个仓库的任何一行代码 |
| v1.7 | 2026-05-01 | **两侧照 v1.6 各实现一轮，回头暴露出契约自己的三个错；本版先改错，再记进度。**<br>**错一：§4.6.7 混淆「已要求但没送到」与「还没实现」。** `execution_status` 标着 🔨，每一份线上载荷都缺它，照表逐字执行会把所有查询判 `failed`，**A 组正例全红**——而 CONSUMER-TESTS 自己要求「A 组与 B 组必须同时绿」。补了区分与过渡判据（有就用 / 没有则按 §4.6.1 从 `steps[]` 推 / `steps[]` 也没有才 `failed`），并把两侧独立实现时都自发发明、且都叫同一个名字的 `status_source` 收进契约。<br>**错二：`dp-trust-01` 标 `xfail` 的指示写错了。** 那份 fixture 带完整 `steps[]`，业务结论今天就能判对，`xfail` 会 XPASS。改为拆两条：业务结论**必过**，判定来源 `xfail(strict=True)` 断言 `status_source == "contract"`。缺口的实质不是「结论错」，是「结论由谁定义」（§4.6.2 警告过 reduce 的口径会漂）。<br>**错三：§4.5 的 stage 表被当成顺序承诺。** 实际顺序是 `routing → normalization → …`（路由在服务入口，归一化在 pipeline 内部）。已注明行序不表示事件顺序，顺序看 §4.2；顺带补上 `dp-bad-06` 一直缺的正文依据——形状不合法的 `database` 即不可信。<br>**资产**：`dp-trust-05` 补 `row_limit`（正文说「必须有值」而 fixture 没给）；`stage.schema.json` 注记 normalization 的实现状态；fixtures 版本号 → v1.7。<br>**进度记录（不冒认关闭）**：七条缺口里五条的实现已在两侧工作树中就位并经核实，但**两侧都还没提交**，所以性质写作「实现已就位待提交」而不是「已关闭」——附录 B 自检表第 2 条要求证据可复核，未提交的工作树不满足。提交后按 commit 复核再改。<br>三条错全部由两侧实现时撞上并各自绕开而暴露。**两侧绕法一致纯属运气好**，这正是契约要防的分叉——所以本版把它们收成规范，而不是记成「已知差异」 |
| v1.6 | 2026-04-28 | **把 v1.5 挂起来的两个双边决定做掉，其余不动。** 不是第四次收口——状态行的口径与 v1.5 一致，只是两条缺口的「性质」由「要改」变成「已改」。<br>**§1③ 补第三档**：上线顺序表由两档（未实现 25s / 已承接流量 90s）改为三档，中间补上**「已实现、尚未承接流量 = 45s」**——即把代码现值扶正为契约值，而不是把代码改回 25s。理由：45s 是按 p90 17.7s 放宽出来的（25s 对 p90 只有 1.4× 余量，实测 max 68.3s 远在其外）；这一档状态真实存在且流式必经；而直接跳 90s 会违反本节自己「先能看到进度，再放宽超时」的硬要求（今天 `SQL_AGENT_TRANSPORT` 默认仍是 `query`）。**代码侧因此无需改动。**<br>**§1③ 新增外层超时联动**：调用方在 A2A 客户端之外还有一层工具超时（`ai-helpdesk/backend/api/main.py:334`，今天 47s）。外层 ≤ send 时外层先杀，**改 send 的值不会有任何效果**；切到第三档必须两个数一起动。今天 47 > 45，顺序是对的。<br>**§4.5 `database` 契约面降级**：由「钉死四个枚举值、不会改名或新增」改为**形状（`^[a-z][a-z0-9_]*_db$`）+ 一份可拉取的生效清单**，并配一条**不对称变更规则**——新增不算契约变更（调用方最坏退化成显示原始标识符），删除/改名算（会让映射指向不存在的库，或更糟：把标签贴到别的库上）。<br>**§4.5 名字更正**：追问「为什么部署与契约不是同一组」后发现，旧表是按通用制造业四件套（ERP/MES/PM/WMS）想象写的，而系统是照橱柜业务长出来的——`mes_db` 是 `prod_db` 的旧名（`prod_db.yaml` 的 description 自称「生产系统（MES）」）、`pm_db` 的职责本就在 `erp_db`（项目=`job`，变更单=`change_order`），**两个都从未在任何部署里存在过**；而真实存在的 `svc_db`（送货/售后/补件）契约里一直没有。据此：`mes_db`→`prod_db` 改名、`pm_db` 删除、`svc_db` 补入。调用方私自加的 `prod_db`/`svc_db` 两个标签**追认为合规**。<br>**明确拒绝了一个方向**：为 `mes_db`/`pm_db` 造模拟数据把它们变成真库——那会造出与 `prod_db` 重复的库和从 `erp_db` 挖出来复制的库，语义检索选库会直接打架（跨库准确率本就只有 16%）。**不能为了让文档成立去伪造现实。**<br>schema / checker / CONSUMER-TESTS D 组同步：`$defs` 由 `contract-declared`+`deployed-*` 改为 `current-*`+`retired-v1.6`，断言改为「退役集与生效集不得相交」。本版仍**只改文档与契约仓库自己的资产**，没有动另外两个仓库的任何一行代码 |
| v1.5 | 2026-04-28 | **状态行第三次收口：由「两侧均已实现并端到端验证」改为「链路已通；下列条款未兑现」。** 这次的病因与前两次都不同——v1.2 是无证据，v1.3 是核对后推翻，**v1.5 是有证据、但证据证的不是契约条款**：v1.4 那张表证明的是端到端链路能跑通，不是条款逐条兑现。<br>七条就地标注 ⚠️ 缺口，每条注明性质（实现待办 / 契约要改）并附文件:行号或可重跑命令：§1③（send 预算跑的是契约没有的第三档 45s）、§4.4（`failed` 只在 pipeline 抛异常时发，步骤失败照发 `completed`）、§4.5（`normalization` 未发送；四个 `database` 标识符钉不死，当前部署里 `mes_db`/`pm_db` 根本不存在）、§7（`tasks/get` 恒 `-32004`，不是「多副本下 `-32001`」，性质由「已知缺陷」改为「刻意不做」）、§7.1（「结果不进 prompt 正文」这条前提是错的，两个都进）、§9.1（三布尔缺失即可信、`bool()` 不校验类型、且真正的门槛只有一道闸不是三道）。<br>**§4.6 新增业务终态语义**：`execution_status` / 逐步骤成败 / 部分成功 / 无数据 / 截断 / as-of / 币种单位 / 来源记录标识，每个字段写明取值域与**缺失即不可信**的处置；并重新定义 `degraded` 与 `completed` 的关系。<br>**新增机器可校验资产**：`schema/`、`fixtures/`（正例 + 七类反例）、`CONSUMER-TESTS.md`、`.github/workflows/contract.yml`。<br>**措辞纠偏**：§8.1 三段引文从未进过任何提交（v1.3 已指出、v1.4 未处理）；§8.4 的 `ai_sql_core/shared_state.py` 不存在（真身在 ai-helpdesk），2.3 QPS 标注为推算并补上「A2A 路径走的是另一个线程池」；§7.2 新增 delta 结论的重新论证。<br>**§10 新增版本边界**：本契约约束 A2A **0.2 子集**，不宣称与已发布的 A2A v1.0 兼容，附迁移矩阵。<br>本版**只改文档，没有为了让契约成立去动另外两个仓库的任何一行代码** |
| v0.1 | 2026-04-21 | 初稿，待对齐 |
| v1.0 | 2026-04-21 | 对齐通过。§9 记录五问答复；§7.1 delta 由"v1 不做"改为**永久不做**并写入 `PolicyStreamGuard` 理由；§6.2 加回滚路径失效前提；§4.5 库标识符升为契约面、附带字段按实现修正 |
| v1.1 | 2026-04-21 | 调用方超时由固定 25s 改为**自适应**（收到事件续命 / 静默 20s 判死 / 总长 90s 硬上限）。§1③、§2、§3.4、§5 全节改口径；§3.4 的"不发心跳"理由重写为"保活帧会削弱故障检测"；§4.5 新增 **repair 次数上限**一节 |
| v1.4 | 2026-04-27 | **两侧实现完成并端到端验证。** 服务端 `ai_sql_a2a/`（事件来源是 §8.2 挑的 `trace.stage()` 事件汇，pipeline 侧零改动）；调用方 `a2a_client.py` + `/chat/stream` 的 `progress` 事件 + 前端步骤列表。<br>**§6.2 的 ⚠️ 阻断解除**——两条回滚路径都做了实机演练（Card 路径 / `-32004` 路径），`AI_SQL_A2A_STREAMING` 默认改为开。§1 三条前提这次是**真的**成立了，且是照着本契约补出来的。<br>状态行改回「已实现」时附上了可复核的证据表——v1.2 就是只写结论不写证据，才让虚报活了一版。<br>附录 B 更新：`/query` 直连仍在，作为 `SQL_AGENT_TRANSPORT=query` 的默认路径 |
| v1.3 | 2026-04-23 | **状态行改口：由"服务端已实现"改为"两侧均未实现"。** 逐条核对 GitHub 后确认 v1.2 的两条实现声明都不成立，且 §8.1 引用的原文从未进过任何提交——契约是对着一个远端没有的版本写的。§1 的三条"调用方既成事实"同步标注为**未成立**（无心跳、无 progress 通道）。正文设计结论不变，只改关于"做没做"的陈述。<br>**§6.2 的 ⚠️ 阻断恢复**——v1.2 据"调用方降级分支已补完并验证"解除了它，那份验证没有对应代码；回滚路径不存在，**打开流式开关之前必须先补上调用方的 `-32004` 降级分支**，否则要关的时候是全线故障而不是降级。§9 #2 同步改回缺口。<br>§1 的时序图标注为设计稿而非实测。附录 B 补上今天的实际连接方式：ai-helpdesk 直连 `POST /query`（`backend/tools/sql_agent_client.py`），是 A2A 之外的捷径，不是本契约的实现 |
| v1.2 | 2026-04-21 | **超时口径按路径分档**：v1.1 的自适应只适用于 `message/stream`；`message/send` 全程零事件是设计（§6.1），套用静默 20s 等于给它 20s 硬预算——比原先的固定 25s 更严，且会让 §6.2 的回滚变成静默降级。send 改为**固定 90s、无静默规则**。§1③ 拆成 A/B 两表并新增**上线顺序**硬要求（先能看到进度，再放宽超时）；§2 口径限定为流式；§5.1 判据表标注仅管流式并补 send 说明。<br>**§6.2 的 ⚠️ 阻断框删除**——调用方 `-32004` 降级分支已补完并验证，回滚路径首次真正存在；改写为「回滚成立的两个前提」（降级分支 ✅ / send 预算 ✅）。§9 #1 #2 两行前提同步更新；§5.4 补 `-32004` 降级的配额放大豁免与边界（豁免仅限流打开之前，流中途失败不得重发 send）。<br>**服务端 §8 已全部实现**，状态行由"正在实现"改为"已实现" |

## 附录 B. 本文的位置

唯一权威副本在独立版本库 `a2a-contract` 里，两个实现仓库都不各存一份。

- 服务端实现：`ai-sql-agent` 的 `ai_sql_a2a/`（`server.py` + Agent Card）
- 调用端实现：`ai-helpdesk` 的 `backend/tools/a2a_client.py`

**直连 `/query` 那条路仍然在**，作为 `SQL_AGENT_TRANSPORT` 的默认值（`query`）。
它是同步无事件的，A2A 是 `SQL_AGENT_TRANSPORT=a2a`。两条路都通、都有测试；
默认留在 query 是因为它跑得久、验得多，切 A2A 是一个环境变量的事。

两边在代码里引用本文时，请写"见 a2a-contract 仓库 `a2a-streaming-contract.md`"并带上 commit，不要复制正文。

### 怎么确认手里这份是不是权威副本

副本漂移已经真实发生过一次（有一侧照着 v0.1 草案实现，并把草案当成了新版），
所以留三个一眼可辨的标记。**读到任何一条 v0.1 特征，就是拿错了，去仓库重新取。**

| 特征 | v0.1 草案（已废弃） | v1.1 起（权威） |
| --- | --- | --- |
| §4.5 `repair` 行的附带字段 | 有 `attempt` | **没有** `attempt` |
| §4.5 `validation` 行的附带字段 | `step`, `findings` | `step`, `database` |
| §4.5「repair 的次数上限」一节 | **没有** | **有**（含 step 严格递增判据） |

v1.3 起再加一条，防的是反方向的漂移——**文档说已经做完了，其实一行都没写**：

| 特征 | v1.0–v1.2（已废弃） | v1.3 | v1.4 起（权威） |
| --- | --- | --- | --- |
| 状态行对实现的陈述 | "已全部实现"，**无证据** | "两侧均未实现" + 核对表 | "两侧均已实现" + **可复核的证据表** |
| §1 三条前提 | 写作"已经成立的事实" | 标注 ①② 未成立 | ✅ 三条都成立 |
| §6.2 回滚阻断 | v1.2 无据解除 | 恢复阻断 | **据实机演练解除**，开关默认开 |

判别 v1.2 与 v1.4 的关键不是"说没说已实现"，而是**有没有附证据**：
v1.2 只写结论，v1.4 附了文件位置、测试文件名和端到端时序。
**读到"已实现"却找不到证据表的，就是拿错了版本。**

v1.5 起再加一条，防的是**第三种**漂移——**有证据表，但证据表证的不是契约条款**：

| 特征 | v1.4（已废弃） | v1.5 起（权威） |
| --- | --- | --- |
| 证据表的每一行对应什么 | 一个**组件 / 一条链路**（"服务端 handler"、"前端进度展示"、"端到端时序"） | 一个**契约小节编号**（§1③ / §4.4 / §4.5 / §7 / §9.1 …） |
| 状态行是否声称"全部兑现" | 是（"两侧均已实现"） | 否——"链路已通"与"条款兑现"分开陈述，未兑现的逐条列出 |
| 正文里有没有 ⚠️ 缺口框 | 没有 | 有，且**每个缺口框注明性质**（实现待办 / 契约要改） |

v1.6 起再加一条，防的是**第四种**漂移——**契约写死了一个它管不住的东西**：

| 特征 | v1.0–v1.5（已废弃） | v1.6 起（权威） |
| --- | --- | --- |
| §4.5 `database` 的契约面 | 一张钉死四个值的表（`erp_db`/`mes_db`/`pm_db`/`wms_db`），声称"不会改名或新增" | **形状 + 可拉取清单**，配不对称变更规则（新增不算 / 删改算） |
| 清单里有没有 `prod_db`、`svc_db` | 没有（它们真实存在却不在契约里） | **有** |
| 清单里有没有 `mes_db`、`pm_db` | 有（它们从未在任何部署里存在过） | **没有**（移到 schema 的 `retired-v1.6`） |
| §1③ 上线顺序表的档数 | 2 档（25s / 90s），而代码跑第三档 45s | **3 档**，中间档 45s，且带外层工具超时联动 |

v1.8 起再加一条，防的是**第五种**漂移——**契约漏掉了跑生产流量的那条路**：

| 特征 | v1.0–v1.7（已废弃） | v1.8 起（权威） |
| --- | --- | --- |
| 契约覆盖几个承载者 | 两个（`message/send` / `message/stream`） | **三个**，多出直连 `POST /query`——它是 `SQL_AGENT_TRANSPORT` 的默认值 |
| 有没有 §4.4.1 | 没有 | **有**（载荷与承载者分开，并划出各节的适用范围） |
| §4.6.5 币种缺失时 | "**不得**回退到自己的全局常量"（一律禁止） | **按来源三分**，`currency_source` 三值；第三档是主动指令不是禁令 |

**读到"契约只管 send 和 stream 两条"的，就是 v1.7 或更早。** 那个范围漏掉的
恰好是今天跑生产流量的那一条。

**读到"当前钉死为这四个标识符"这句话的，就是 v1.5 或更早。** 那句话描述的东西
从来没有成立过——枚举钉死的前提是两边同时发版，这在两个独立仓库之间不成立。

### 自检表（改状态行之前逐条过一遍）

前两次翻车各留下一条规则，v1.4 留下第三条。**改状态行的人必须自己走完这五步，
走不完就不许把状态行往"已实现"的方向改。**

| # | 规则 | 从哪一版学到的 | 怎么算过 |
| --- | --- | --- | --- |
| 1 | **结论必须附证据**：文件路径:行号 / 测试文件名 / 可重跑的命令 | v1.2（只写结论，虚报活了一版） | 每条结论旁边都有一个别人能自己跑一遍的东西 |
| 2 | **证据必须指向真实提交**，不是本地未提交的改动、也不是"打算这么写" | v1.3（§8.1 引用的原文从未进过任何提交） | 引用的每处代码都能在远端 `git log -S` 里查到 |
| 3 | **两侧都要核**，一侧的实现不能推定另一侧 | v1.3（服务端与调用方分别虚报过） | 服务端与调用方各自的证据分开列 |
| 4 | **⚠️ 证据表必须逐条对应契约条款编号，不是对应"端到端跑通了"。** 一行证据必须能回答"§X 那一条，代码里在哪、值是多少"；只能回答"整条链路能跑"的，不算条款证据 | **v1.4（这一条是新的）** | 证据表的第一列是**小节编号**；把表里每一行的小节号抄下来，与正文里带**数值、字段名、状态映射**的条款对照，**没有被任何一行覆盖的条款，一律按"未核实"处理，不许按"应该没问题"处理** |
| 5 | **机器能校验的部分交给机器**：形状类断言写进 `schema/` + `fixtures/`，不靠人读文档 | v1.5（前四条全是人肉规则，人肉规则会累） | `CONSUMER-TESTS.md` 里该条款有对应用例，且两侧 CI 都在跑 |

> **第 4 条为什么值得单列。** v1.4 的证据表**不是假的**——12 个契约测试真的绿，
> 端到端时序真的跑出来了。它的问题是**覆盖面**：链路跑通只要求事件流的形状对，
> 于是所有"形状对但数值错"的条款（send 预算 45 而不是 90、`failed_steps>0` 却发
> `completed`、九个 stage 只发八个、`database` 枚举与部署不符）全都从证据表的缝里漏了过去。
>
> **判别方法：拿证据表的行数去比契约里"带数值或字段名的条款"的条数。**
> 前者远小于后者，就说明这张表证的是链路不是条款——无论它看起来多详实。

判断顺序永远是：**先看状态行的版本号**，再对照本表。不要靠"我印象里新版加了什么"。
