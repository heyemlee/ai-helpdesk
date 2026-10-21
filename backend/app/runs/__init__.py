"""
可恢复的运行（durable agent runs）：一轮对话不再绑在 HTTP 连接上。

**原来的样子。** /chat/stream 的整轮流程——读记忆、意图、检索、委派查库（17～40 秒）、
专家作答、守卫、落工单——都跑在 SSE 响应的生成器里。客户刷新页面，这一轮就中止；
发版或容器重启，正在跑的轮次全部丢掉；长任务和连接绑在一起，扩容只能按连接分。

**现在（AI_HELPDESK_RUN_MODE=queued）。** 一轮对话是一个 run：

    API  ── 写 agent_runs(queued) ──▶ Postgres（run 的状态只有这一份）
     │  └─ XADD ──▶ Redis Stream  ai-helpdesk:runs:{instance}  （派发；消费组 workers）
     │                                  │ XREADGROUP
     │                                  ▼
     │                               worker ── 条件 UPDATE 抢租约 ── 跑现有的整轮流程
     │                                  │ 每一帧 XADD
     ▼                                  ▼
    客户端 ◀── GET /runs/{id}/events（Last-Event-ID 续流）── ai-helpdesk:run:{id}:frames

- **状态只在 Postgres。** Redis 里的两条流都是可以重建的：派发消息丢了，清扫任务按库里
  `queued` 太久的 run 重新派发；帧丢了，续流时从库里的结果补出最后一帧。所以 Redis
  不持久化也不丢 run。代价是派发有一次"先写库、再发消息"的双写，靠清扫补齐（库就是
  outbox），不靠两边同时成功。
- **至少执行一次，副作用只算一次。** worker 被杀掉，它没确认的消息过了闲置时间会被
  别的 worker 用 XAUTOCLAIM 接手，租约过期的 run 可以被重新领走（attempt + 1）。所以一轮
  可能跑不止一遍，每一个对外的副作用都按 run 去重：工单 id 由 run 推出来（同一个 run
  只会有一张），审计和工作记忆按 run 写一次，工具结果按 run 记下来（memo），重跑时
  40 秒的委派不再做第二遍。
- **提交点。** 回答一定下来就写进库（checkpoint），然后才落副作用。在那之后失联，接手的
  那一遍不再重新作答，照着提交点补完副作用、发出同一个回答：客户看到的、审计和记忆里的、
  工单判没判，永远是同一个回答。
- **attempt 是栅栏。** 续租、写结果都带 owner 和 attempt：被判失联、run 已经被别人
  领走的那个 worker，写结果时一行都改不到，它那一遍作废；它发出去的帧带着旧的 attempt，
  续流时被丢掉。
- **这一轮自己出错不重试。** 重试只针对 worker 失联（进程被杀、机器没了、租约丢了）。
  编排器抛异常、模型说不可用，这些是这一轮的结局，和原来一样报给客户，重跑一遍只会
  再错一遍。反复失联超过上限的 run 进死信，转人工。

模块：

- `records`：run 的形状和状态
- `store`：Postgres 和进程内两种存储（按实例隔离）
- `broker`：派发队列（Redis Streams 和进程内实现）
- `channel`：一个 run 的帧流（续流靠它）
- `memo`：这一轮的工具结果记录，以及当前绑定的 run
- `degrade`：按积压降级
- `worker`：领 run、续租、执行、写结果、接手失联的 run、清扫
- `service`：API 这一侧：提交、查询、续流、等结果
"""
