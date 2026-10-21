# 事件日志：上线、观察和日常运维

一轮 `/chat` 或 `/chat/stream` 从开始到结束发生的事，按顺序记成事件，追加进 Postgres 的
`turn_events` 表（`backend/app/events/`）。它是工程数据：排障、刷新后恢复检查器、工单里看
"转人工那一轮的经过"、拿真实流量重放。合规留档仍然是 `conversation_log`，两者的保留期和用途都不同。

## 开关

开关 `AI_HELPDESK_EVENT_LOG` 和工具循环一样写在 `backend/.env` 里（`full` / `meta` / `off`，代码默认 `off`），
两个实例共用这份文件；客户实例要单独关，在 `backend/.env.customer` 里写 `AI_HELPDESK_EVENT_LOG=off`。
`docker-compose.yml` 只写死各实例必须是那样的几项：

| | 员工实例 | 客户实例 |
|---|---|---|
| `AI_HELPDESK_INSTANCE` | `staff` | `customer` |
| 原文保留 | 30 天（代码默认） | 14 天 |
| 整行保留 | 180 天 | 180 天 |
| 读接口 | 开 | 关 |

`AI_HELPDESK_INSTANCE` 没设或写错时日志不开，`/health` 的 `event_log.disabled_reason` 写明原因：
标错实例的后果是员工那边看得到客户的轮次，宁可不记。改开关要重启对应的实例。

**两个实例不必一起升级。** 事件日志新建的都是新表（`turn_events`，以及记"工单 / 审计记录是哪一轮"的
`ticket_turns`、`conversation_log_turns`），没有给 `tickets`、`conversation_log` 加列：旧镜像按 `SELECT *`
读这两张表，多一列它就报错。所以员工实例先升级、客户实例还跑旧镜像，或者员工实例回滚到旧镜像，都照常读写，
回滚不需要删表删列。

## 上线后看什么

先只开员工实例，观察两到三天：

1. `/health` 的 `event_log`：`dropped` 为 0，`queue_depth` 接近 0，`last_error` 为空；
2. Prometheus：`turn_events_dropped_total` 为 0，`turn_projection_mismatch_total` 为 0（影子校验：
   每轮结束时拿内存里的事件投影一次，和发出去的响应比），`/chat`、`/chat/stream` 的 p50 / p95 和打开前持平；
3. 每天跑一次检查，没有终结事件、seq 有缺口、有没打用途的模型调用的轮次都应该是 0：

   ```bash
   docker exec ai-helpdesk-app python scripts/turn_events.py check --instance staff --hours 24
   ```

   排队模式下还会有一列 `abandoned`，不算问题：跑那一轮的 worker 被杀了，接手的那一遍（或者把 run
   送进死信的那一次）替它补了一条 `turn_abandoned`。这些轮次缺的那一截是真丢了，但去向是清楚的；
   数量不会超过接手的次数（`agent_run_attempts_total{kind="reclaimed"}`）加上进死信的次数
   （`agent_runs_finished_total{status="dead"}`）：停机交还、撞故障交还重试的那几遍自己收了场，不会出现在这里。

4. 表的增长：每轮十几到二十几条事件，结构每轮几 KB，原文每轮一二十 KB。

`turns_concurrent_total` 是同一会话两轮重叠的次数。后端没有串行化同一会话，这个数两周后再看：
一直是 0 就不做；不是 0 再加会话锁。

客户实例升级、打开日志之后，先确认外网读不到：`http://<主机>/turns/x` 应该是 404（对外的 nginx
只放行 `/chat`、`/chat/stream`、`/health`，客户实例的读接口本身也关着）。

## 保留期

服务自己每天清一次，只清本实例的行：原文到期清空成 `NULL`，结构留着；整行到期删除。启动后约一分钟
跑第一次。清了多少、上次什么时候跑的，看日志里的"事件日志保留期"那一行，或 `/health` 的
`event_log.retention`。天数配成 `0` 表示不清。

## 查一轮

- 员工实例：工作台刷新后点"这一轮的经过"或打开编排检查器；接口是 `GET /turns/{turn_id}`（要员工钥匙）。
- 客户实例（读接口关着）：在本机跑
  `docker exec ai-helpdesk-customer python scripts/turn_events.py show --instance customer --turn <turn_id>`，
  加 `--raw` 看原始事件。
- 客户拿着响应头里的 `X-Request-ID` 来报障时：先在审计日志或工单里按 request_id 找到那一轮的 `turn_id`。

## 删除请求

```bash
# 先看要删什么
docker exec ai-helpdesk-app python scripts/turn_events.py forget --instance staff --user <user_id>
# 确认之后真的删
docker exec ai-helpdesk-app python scripts/turn_events.py forget --instance staff --user <user_id> --yes
```

按用户找会话有两处：`turn_started` 的原文，和审计日志（`conversation_log`）里记在这个人名下的会话——本实例写的，
加上分不出实例的老记录。原文 30 天就清空，更早的轮次只能靠审计找到；它们剩下的结构行仍然能关联回这个人。
输出里的 `found_by` 说每个会话是从哪儿找到的（`given` / `turn_started` / `conversation_log`）。
也可以直接给 `--conv <conv_id>`（可以给多个）。只删事件日志；审计日志在这里只读不删，删不删是另一个决定。

## 备份和恢复

`scripts/docker-deploy.sh backup` 的 `pg_dump` 带 `--exclude-table-data=turn_events`：只备表结构、不备数据。
保留任务清得到库里的原文，清不到备份里的。恢复之后日志是空的，丢的是工程数据。

## 重放

```bash
docker exec ai-helpdesk-app python scripts/replay_turns.py --instance staff --mode route --hours 24
docker exec ai-helpdesk-app python scripts/replay_turns.py --instance staff --mode guard --outcome handoff
docker exec ai-helpdesk-app python scripts/replay_turns.py --instance staff --mode export --turn <turn_id> --out /tmp/cases.json
```

- `route`：拿当前代码重算两道门控和路由，报出会变的轮次。改路由锚点、门控规则之前先跑一遍；
- `guard`：把每一步的原始输出喂给当前的答案守卫，报出替换结果变了的轮次；
- `export`：导出成 `data/eval/trajectory_cases.json` 的用例骨架，`needs` 和期望要人来填；
- `answer`：用当前代码和模型重新作答，**要调模型**：不带 `--yes` 只打印计划和调用量，跑之前要安排。

重建出来的请求和当时的对不上（脚手架、SOP 改过）时，报告里标成"用当前代码重放旧输入"，不当成复现。

## 走 Kafka（AI_HELPDESK_EVENT_SINK=kafka）

事件不直接写 `turn_events`，而是发进 Kafka（每个实例一个 topic，`ai-helpdesk.turn-events.<实例>`），由投影消费者
写库；采样消费者从同一个 topic 挑评测候选。读接口、控制台、保留任务、`turn_events.py` 都照旧对着 Postgres。
设计和取舍见 [durable-agent-runs.md](../design/durable-agent-runs.md) 的"The event log on Kafka"。

```bash
docker compose -f docker-compose.yml -f docker-compose.kafka.yml up -d --build
# 排队模式再叠 Kafka：多给一个文件（worker 的那两项变量），见 docker-compose.queued-kafka.yml 开头
```

- topic：`python -m app.events topics` 建（6 个分区、留 7 天，死信 topic 加 `.dlq`），已有的只改保留期。保留期最多
  13 天：要短于最短的原文保留期（客户实例 14 天），不然回放会把库里已经清空的原文写回去。分区数只在新建时定。
- 看积压：Prometheus 的 `turn_events_consumer_lag`（按消费者、实例、分区），Grafana"排队模式"面板最下面一行；告警
  `TurnEventsConsumerLagging`。投影连不上库时一直重试、不跳过，积压会一直涨——topic 只留 7 天，超过的会被 Kafka 删掉。
- 死信：解不开的消息、实例不对的、库因为数据拒收的那一行，原样进 `ai-helpdesk.turn-events.<实例>.dlq`，消息头里有
  原因（`error`）和来处（`source`：topic:分区:offset）。消费者已经跳过、分区照常往前走；告警 `TurnEventsDeadLettered`。
  ```bash
  docker exec ai-helpdesk-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 \
    --topic ai-helpdesk.turn-events.staff.dlq --from-beginning --property print.headers=true --timeout-ms 5000
  ```
- 重建投影（库里的 `turn_events` 丢了或者要搬库）：停掉投影消费者，把它的消费组 offset 拨回最早，再起来。主键去重，
  已有的行不会多出来；删除请求删过的会话（`forgotten_conversations`）不会回来。只能补回 topic 还留着的 7 天。
  ```bash
  docker stop ai-helpdesk-projector-staff
  docker exec ai-helpdesk-kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
    --group ai-helpdesk.turn-events.projector.staff --topic ai-helpdesk.turn-events.staff --reset-offsets --to-earliest --execute
  docker start ai-helpdesk-projector-staff
  ```
- 删除请求：`turn_events.py forget` 先把会话记进 `forgotten_conversations`，再删库里的事件和评测候选。Kafka 里的副本删不掉，
  保留期（7 天）到了才没；在那之前任何回放都会按这张表跳过它。
- 评测候选：`eval_candidates` 只有结构（为什么挑中、这一轮的摘要），按 `rules`（规则版本）分开。导成用例骨架时拿 turn_id
  走原来的导出（原文按保留期还在的才导得出来）：
  ```bash
  docker exec ai-helpdesk-postgres psql -U aihelpdesk -d aihelpdesk -Atc \
    "SELECT turn_id FROM eval_candidates WHERE instance='staff' AND rules='v1' ORDER BY sampled_at DESC LIMIT 20"
  docker exec ai-helpdesk-app python scripts/replay_turns.py --instance staff --mode export --turn <turn_id> … --out /tmp/cases.json
  ```
- 改采样规则（`AI_HELPDESK_SAMPLER_RULES` / `_RANDOM_RATE` / `_MIN_CONFIDENCE`）：规则的标签跟着变，采样消费者换一个消费组，
  从 topic 留着的最早那条读起，新的一份候选和旧的并排放着，旧的不动。
- 写入那一侧：服务和 worker 的 `/health` 里 `event_log.sink` 是 `kafka`，`event_log.kafka.connected` 说连没连上（第一次发
  的时候才连）。连不上时和库挂了一样：退避重试，最后计数丢掉（`kafka_error`），回答不受影响。worker 把一个 run 记成跑完
  之前最多等 1 秒，让这一轮的事件先送到。

## 已知限制

- 写入口径是"最多一次"：进程崩溃会丢最后约 250 毫秒，库挂了会丢重试之后的批次。看得出来：seq 有缺口，或者没有终结事件。
- `turn_abandoned` 不是跑这一轮的进程发的，所以不接着编号，固定写在保留号 1,000,000 上（`RESERVED_SEQ`）：
  接手的一方不知道原来那一轮记到了第几条，补两次也只留一条。查缺口只看保留号以下的那一段；"完整"仍然只认
  进程自己发的三种终结事件，补过的轮次在 `GET /turns/{turn_id}` 里是 `complete: false`，`abandoned` 说明后来
  怎么样了（`superseded` 加接手的是第几次执行，或 `dead_letter`）。
- 同一会话在途的检测在进程内做，只在单进程时准；以后开多 worker 要换成 Redis 计数。
- 流式中途断开、出异常的轮次，事件日志会记（`turn_aborted`、`turn_failed`），审计和记忆仍然不写——要不要补是另一个决定。
- 客户端送来的 `conv_id`、`user_id` 含 NUL、控制字符、孤立代理字符或超过 200 字的，`/chat` 和 `/chat/stream`
  在入口就回 422，这一轮不开始。`message` 里的 NUL 仍会让客户原话那条审计写不进去，这次没动。
- system 全文每轮每个专家存一份，是原文里最大的一块；量大了再按哈希去重（哈希已经记着）。
- SDK 自己的重试在 `model_call` 里只算一次；远端 Embedding 和 Rerank 不是对话模型，不在日志里，看 OTel。
