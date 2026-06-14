# EchoMind Skills 文档

EchoMind 启动时会从 `ECHOMIND_SKILLS_DIR` 读取 Skills，并在匹配用户请求时注入到对应 Agent 的 system prompt。Skills 适合维护业务处理规范、客服话术、技术排障 SOP、账单审核边界、升级规则和禁止事项。

当前内置四类 Skills，与三个专家 Agent + 一个兜底 Agent 一一对应：

```text
skills/technical_support/SKILL.md         # 专家：故障排查、接口错误、部署配置和安全边界
skills/billing_support/SKILL.md           # 专家：扣款、退款、发票、订阅和财务审核
skills/order_logistics/SKILL.md           # 专家：订单状态、发货进度、物流轨迹和签收异常
skills/general_customer_service/SKILL.md  # 兜底：接待、澄清、分流、投诉和转人工
```

`agents:` 字段决定 Skill 注入给哪个 Agent。新增 Agent 类型时必须同步新增对应
Skill，否则那个 Agent 会在没有任何 SOP 约束的情况下回答用户。

## Skill 文件格式

推荐每个 Skill 使用独立目录，并将主文件命名为 `SKILL.md`：

```text
skills/<skill_name>/SKILL.md
```

文件顶部使用简单 front matter：

```markdown
---
name: 技术支持处理规范
description: 适用于 TechnicalAgent 的故障排查和升级处理规范
keywords: 报错,错误,接口,API,部署,超时,500,401,日志
agents: technical
enabled: true
---
```

字段说明：

- `name`：Skill 展示名称，会出现在注入给模型的 prompt 中。
- `description`：简短说明，方便 `/skills` 接口排查。
- `keywords`：触发关键词，用户消息命中后才注入；多个关键词用英文逗号或中文逗号分隔均可。
- `agents`：适用 Agent，可填 `technical`、`billing`、`order`、`general`，多个值用逗号分隔。
- `enabled`：是否启用，支持 `true/false`。

## 编写要求

- 重要规则放在文档前半部分，因为过长内容会按 prompt 预算截断。
- 一类 Skill 只描述一类职责，不要把技术、账单、通用客服规则混在一个文件里。
- 必须包含“角色定位”“处理流程”“升级条件”“禁止事项”等稳定章节。
- 对用户隐私、支付、密码、验证码、API Key、Token 等敏感信息必须写明禁止收集或禁止公开。
- 对无法保证的事项使用保守措辞，例如“通常”“预计”“需要核验后确认”。
- 对需要人工、财务、二线技术处理的场景要明确写出升级条件。

## Skill 与出站策略的关系

Skill 是注入 prompt 的**软约束**，模型可能违反。真正的硬约束在
`core/answer_policy.py`：Agent 回复生成后会逐句扫描，把没有出处的价格承诺和
时效承诺替换成保守表述，记录结构化违规项，并拉低本轮置信度触发转人工。

"有出处"的判定依据是本轮的 RAG 检索结果和注入的 Skill 正文——所以
把明确的价格和时效条款写进 Skill 或知识库，Agent 才被允许引用它们。

## 热加载

修改 Skill 文件后，不需要重启服务，调用：

```bash
curl -X POST http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```
