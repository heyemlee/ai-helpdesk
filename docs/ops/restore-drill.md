# 恢复演练记录

第一次真的跑。**演练之前这条路径两个存储都是坏的，而且都不报错。**

## 结果

| 项 | 值 |
| --- | --- |
| **RTO**（从执行恢复到服务全部恢复运行） | **17 秒** |
| 备份耗时 | 1 秒（Postgres 168KB dump + Redis 9.3MB） |
| **RPO（技术下限）** | **约 1 秒**——Redis AOF 默认 `appendfsync everysec`，拷贝时最多丢 1 秒写入；Postgres 的 `pg_dump` 是取快照那一刻的一致视图 |
| **RPO（实际）** | **无界**。备份是手工触发的，没有定时任务。实际 RPO = 距上次有人手动跑 `docker-deploy.sh backup` 的时间 |

**RPO 那两行必须一起读。** 技术下限 1 秒是"备份跑起来之后"的性质；
今天没有任何东西保证备份会被跑。**要把实际 RPO 变成一个数，先得有定时备份。**

## 演练方法（可重复）

1. 写入可检测的标记：Postgres 插一行 `conv_id='DRILL-FINAL'`，Redis 写一个 key；
2. 备份；
3. **删掉标记**（模拟数据丢失）；
4. 恢复，计时；
5. 验证标记**回来了**。

第 5 步是关键。只验"恢复没报错"证明不了任何事——这次三个缺陷里有两个都是
"命令成功返回，数据没动"。**必须验数据本身。**

## 演练查出来的三个缺陷（都已修）

### 1. Redis 备份和恢复是彻底的空操作

Redis 跑 `--appendonly yes`，权威数据在 AOF 里：

```
/data/appendonlydir/appendonly.aof.1.incr.aof   9.7 MB   ← 真实数据
/data/dump.rdb                                    89 B   ← 备份脚本拷的这个（空 RDB）
```

备份拷走 89 字节的空文件；恢复把它拷回去，而 AOF 模式下 Redis **根本不读它**。
`docker cp` 两次都成功，脚本打印"备份完成"。**没有任何症状。**

修复：备份/恢复整个 `/data`。清空必须用挂同一个卷的**临时容器**，
不能 `docker exec` 到 redis 上——那时它已经 stop 了。
（第一版修复就是这么写错的，和它要修的原始缺陷是同一类错误。）

### 2. Postgres 恢复只在空库上成立

裸 `pg_dump` 产出的 SQL 只有 CREATE 没有 DROP，往非空库灌会在第一张表撞上
`relation "conversation_log" already exists`。而**真实恢复场景恰恰是库非空**。

修复：`pg_dump --clean --if-exists`。

### 3. 恢复失败后应用被留在停止状态，脚本不说

实测：恢复因为缺陷 2 中止，`ai-helpdesk-app` / `nginx` / `frontend` 全部
`exited` 并保持，而屏幕上只有一句"请检查 postgres.sql"。

真实事故里这是最坏的形态：**你以为你在恢复，实际上你刚把服务停了，
而且没人告诉你。** 修复：所有失败路径都把应用拉回来，并明说当前状态。

### 附带：备份目录同秒撞车会静默嵌套

目录名只到秒。同一秒两次备份不是"覆盖"——`docker cp src dest` 的语义
**取决于 dest 存不存在**，第二次会把内容**嵌套**进第一份里，
恢复时按老路径读到的是陈旧数据。修复：撞名就换名；`docker cp` 一律用
`src/.` + 先 `mkdir`，两种情况结果一致。

## 锁定

`backend/tests/test_restore_ordering.py` 的 `TestRedisIsBackedUpAsAWholeDirectory`
五条，加上原有的顺序断言，共 11 条。用假 docker 跑命令序列，不碰真实数据。

## 还没做

- **定时备份**。没有它，上面那个"实际 RPO 无界"不会变。
- **异地/离机保存**。备份现在就落在同一台机器的 `backups/` 下，
  机器没了备份也没了。
- 更大数据量下的 RTO。这次是 202 行对话日志 + 9.3MB Redis，
  17 秒里绝大部分是容器起停，不是数据量。
