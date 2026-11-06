# 员工部署、备份和恢复操作说明

本目录提供部署准备；没有安装定时任务、发布镜像或操作现有生产栈。实际验证结果见 [第五阶段报告](../../docs/architecture/upgrade-stage-5.zh-CN.md)。首次上线仍须补齐外部账号、域名、密钥、数据迁移与业务验收，并取得生产操作授权。

## 部署边界

使用 [release/compose.yml](../release/compose.yml) 的独立 Compose 项目。PostgreSQL、Redis 不发布主机端口；API、Worker、SQL Agent、前端只在容器网络内通信。SQL Agent 单实例、单 Uvicorn Worker。`ingress` 仅绑定选定的 `127.0.0.1` 端口，外层现有 TLS 入口代理到该端口。`PUBLIC_ORIGIN` 必须是员工入口的精确 HTTPS origin，`PUBLIC_HOST` 是同一主机名，不含端口或协议。正式上线还需从真实 HTTPS 域名检验 Cookie、Origin、Host 和 TLS 链路。

运维工具只使用显式配置的项目、Compose 文件和环境文件，拒绝固定 `container_name`。当前旧生产栈不符合这份模板，不能把它的项目名直接填进配置后运行。首次切换需要另行安排维护窗口：停止共享数据库的全部写入者，导出 PostgreSQL、SQL Agent 状态和必要配置、迁移私有对象，在新项目恢复并复核。当前演练没有读取生产数据，也没有执行这次迁移。

员工账号采用本地 PostgreSQL 身份目录，没有默认账号或密码。只通过已验证的员工会话产生 actor；角色或停用变更会撤销已有会话和旧授权版本的排队工作。业务查询是单公司共享只读范围，SQL 服务身份为固定的 `ai-helpdesk`，禁止 impersonation；本实现不提供业务行级隔离。

| 角色 | 允许的主要动作 |
| --- | --- |
| 员工 | FAQ、自己会话与查询历史；允许受众内的知识、Case、材料与私有附件；提交补料 |
| 运营 | 员工能力；运营受众、历史工单、人工接管、状态变更与草稿审批 |
| 管理员 | 员工与运营能力；员工管理、历史归属登记、知识发布、模型与数据源管理 |

管理员也不能直接认领其他员工已归属的原始会话/SQL 查询历史。历史会话的首次归属必须显式登记原因。Case 业务数据在允许受众内共享，上传者也不能绕过关联 Case 的限制。

## 准备配置

将 `release.env.example`、`app.env.example`、`sql.env.example` 和 `config.example.json` 复制到部署机上的专用配置目录后填写。模板的必填值为空，因此不能直接启动。配置不应放进 Git；环境文件和完整运维报告保持 `0600`，包含密钥的目录保持 `0700`。

| 文件/目录 | 内容和权限要求 |
| --- | --- |
| `/etc/helpdesk/release.env` | 三份已验证的不可变镜像 ID 或 registry digest、显式数据目录、入口域名、PostgreSQL 密码 |
| `/etc/helpdesk/app.env` | 员工 preflight 参数、固定只读 SQL 服务凭据、模型和私有对象存储配置 |
| `/etc/helpdesk/sql.env` | 单公司只读范围、已批准库表、语义目录和模型配置；沿用 `deploy/sql-agent/` 的配置机制 |
| `STATE_ROOT/config` | 模型设置、`sql_keys.json`、`sql_admin_key`、`metrics_token`；SQL 管理 key 仅后端使用 |
| `STATE_ROOT/sql` | SQL Agent 的 memory、audit、runs SQLite 状态 |
| `STATE_ROOT/objects` | 仅本地私有对象适配使用；正式离机对象使用独立私有桶 |
| `backup_dir` | 加密快照、完整清单、维护标记、发布记录；API 不挂载此目录 |
| `report_root` | 只存备份/恢复验证成功时间戳，目录 `0755`、文件 `0644`，API 只读挂载 |
| `key_file` | 独立生成的 32 字节随机 AES 密钥，base64 格式、`0600`；必须在数据目录和备份目录之外 |
| `gate_root` | 维护标记目录；由入口只读挂载 |

密钥需要独立托管和灾难恢复副本。对象存储凭据可用主机 AWS profile 或 `credentials_file`；后者是只在运行期读取的 SDK 凭据 JSON，不能提交。应用容器使用自身最小权限运行期身份。私有附件桶与备份桶分开；禁用公开访问。云 IAM、跨故障域、加密密钥托管及桶策略需要真实账号验收，SeaweedFS 演练不证明这些事项已完成。

Linux 上运行维护工具的账户需能读取私有文件并恢复原 UID/GID。后端镜像 UID 为 1000，SQL Agent 为 10001；恢复按快照记录的权限和属主重建文件，不靠放宽数据文件权限启动。SQL/配置目录的初始权限须允许这两个服务各自写入对应文件。配置可以拆分并设置适当组权限，不要照搬演练目录的宽松初始化权限。

首次管理员由受控终端运行镜像内的 `scripts/staff_accounts.py` 创建；密码交互输入，不放在命令行。数据库 schema 必须已由同版应用初始化。没有公开注册入口。生产环境仍保留现有 API key、AUTH_SECRET、PROTECT_CHAT、token issuer CIDR 和 deployment scope preflight；员工 `/auth/token` 始终拒绝共享密钥签发身份。

## 定时备份和验证

依赖使用独立 Python 3.12 环境安装 `scripts/operations/requirements.txt`，不复用生产应用的 `.env`。Linux 可使用本目录 `helpdesk-backup.*` 和 `helpdesk-restore-verification.*`；macOS 提供两份 `com.ai-helpdesk.*.plist`，部署前调整绝对路径和 Docker 连接。二者选一种安装，不要同时启用。当前均未安装或加载。

备份计划为每小时一次；Linux 最多增加 30 秒随机延迟。恢复验证为每日 03:05，Linux 最多延迟 5 分钟。它们共用文件锁：一次操作正在执行时，另一操作失败退出，避免清理正在验证的快照。主机休眠、Docker 停止、磁盘或网络不可用会影响计划；单机不是高可用，必须关注成功时间而非定时器是否存在。

单次备份在维护标记生效后停止该项目全部非存储服务，等待写入者退出；再执行 PostgreSQL custom-format dump，复制 SQL SQLite 状态、必要部署配置及私有对象。备份覆盖整库；清单另外记录业务表计数/摘要、每个文件 SHA-256，以及所有 ready 附件的原图和预览 SHA-256。SQLite 执行完整性检查。配置原件在 `config/deployment/` 中进入加密包，密钥本身不进入包。

包使用 AES-256-GCM 加密后写入本地，上传离机桶后重新读取远端密文校验大小和 SHA-256。配置 `require_offsite=true` 时，远端未验证就不记录备份成功。备份结束只恢复此前已运行的服务；备份失败不改变业务数据。小时快照会产生短暂维护窗口，不宣称在线无停机备份或连续 WAL 恢复。

每日验证把登记的快照复制到新的临时目录，先核对实际密文 SHA-256，再在独立临时 PostgreSQL 中恢复；解包的 `snapshot_at` 必须与登记值相同。验证检查业务摘要、私有对象、SQLite，并确认账号关闭、会话清空。应用/Worker/通知均不启动。只有全部成功才刷新 `last-verification.json` 和可读时间戳；报告不能仅复制旧登记值冒充验证结果。

## 保留策略

默认本地策略是 30 天并至少保留最新 48 份。最新成功备份和最近一次成功恢复验证对应的快照无论多旧都受保护。没有成功验证、保护对象缺失、清单摘要变化时不清理；无清单文件和符号链接不属于自动清理范围。

`prune` 默认为预览，`--apply` 才删除列出的本地密文及其清单。示例配置启用 `apply_after_backup`，只在新备份成功后执行同一受控算法。清理与备份/验证共用锁。会保留未知、失败或手工维护的文件，容量管理仍需要查看报告。

工具不删除远端备份。真实云端生命周期/Object Lock 策略尚未配置或验证；不要设置会无条件删除“最后一个已验证可恢复快照”的规则。拟定云端策略时，应先保留最新/最近验证对象，并验证权限、版本保留和恢复，再启用到期清理。已链接的业务材料、Case 消息/事实/动作/回执、政策和审批证据没有自动 TTL；维护任务仅清理失效会话、旧登录限流桶和超过一天未关联的上传。不会批量清除未知远端对象。

## 恢复及重新开放

以下命令是操作接口示例，不代表本次已经授权或执行生产恢复。所有路径必须指向经过核对的独立部署配置：

```sh
python scripts/operations/recovery.py --config /etc/helpdesk/operations.json --allow-production backup
python scripts/operations/recovery.py --config /etc/helpdesk/operations.json --allow-production restore /secure/snapshot.aihdbak
python scripts/operations/recovery.py --config /etc/helpdesk/operations.json --allow-production review-access /secure/current-access-review.json
python scripts/operations/release.py --config /etc/helpdesk/operations.json --allow-production --manifest /secure/release.json --probe /secure/business-probe.json
```

恢复前关闭入口及所有写入者；先验证 GCM、清单和文件，再改数据库/SQL 状态/对象。对象恢复后重新读取**实际目标**计算哈希；仅检查解包文件不算成功。任何一步失败都留下 `restore_failed_writers_closed` 和维护标记，服务不自动开放。重新执行完整恢复可以覆盖失败中间态；不能手工删除维护标记来绕过检查。

恢复后的所有员工账号先停用、授权版本递增、会话清空。`review-access` 只接受当前人工核对的 actor ID、employee/operations/admin 角色及 active 布尔值，至少有一名活动管理员；未列出的账号继续关闭。文件示例结构为 `[{"actor_id":"emp_...","role":"employee","active":true}]`。应核对人员当前有效性、密码重置需要和旧密钥是否已撤销，不能直接把备份里的角色/密码/云凭据当作最新状态。该命令只完成访问复核，不启动应用。

必要环境文件被恢复在 `STATE_ROOT/config/deployment/`，不会自动覆盖 `/etc/helpdesk/` 的现行外部配置。操作员需要先核对域名、当前密钥和账户范围，再决定哪些值恢复到外部文件；不要自动恢复已撤销的凭据。

重新开放必须使用同一不可变发布清单和私有业务探针文件。探针包含一个已复核普通员工的凭据、已恢复 Case ID、预期事实和附件预览哈希，只能放在 `0600` 文件中。检查不是空用例：必须读取实际 Case 消息、事实、非空附件原图和预览。它还验证未登录拒绝、伪造 key 拒绝、普通员工不能调用管理员接口、未知历史不可读。通过前只启动 API/前端；SQL 启动后先使用这份真实员工会话经 `/sql-api/whoami` 验证整条网关，再核对下游固定只读服务身份，通过后才启动 Worker、开放入口。会话凭据短暂保存在 API 容器内的 `0600` 文件，SQL 检查后删除，不进入输出报告。若备份后的 SQL key 轮换与恢复的 `sql_keys.json` 不一致，这一步必须失败关闭；不能仅依据 `/readyz` 或 Case/附件能读就开放。

快照覆盖之前的数据恢复不代表之后的写入不会丢失。演练明确注入并观察到快照后的一笔写入丢失。按小时计划只能提出“约一小时加调度/快照完成时间”的设计预算，不能把小样本实测秒数作为生产 RPO 承诺。生产 RTO 还包含下载、人员复核、密钥重置、域名/TLS、数据规模和外部服务恢复。

## 不可变发布和回退

CI 的 backend/frontend/浏览器/contract/SQL 门禁通过后，Docker job 从已提交 Git archive 构建三份镜像。两套临时 Compose 演练分别验证本地和 S3 对象、真实本机入口、维护关闭、权限、私有附件、恢复和故障候选回滚。镜像清单、两份演练报告都记录 backend/frontend/sql 完整镜像 ID；发布脚本逐项对照，缺任何服务或同提交不同 ID 都拒绝。

只有仓库显式开启 `AI_HELPDESK_PUBLISH_IMAGES=true` 后，main 上成功的该 job 才能将**已经演练过的镜像**推送到 GHCR，并生成 registry digest 清单。当前没有执行 registry 推送或生产发布。若部署机没有镜像，先按清单拉取对应 digest；部署工具使用 `--pull never`，不替换为可变标签。

发布把维护入口关上，验证候选后原子更新实际 `release.env`，成功后才开放。候选失败只尝试 `active-release.json` 中已通过员工边界验证的版本；再次验证后恢复运行。回退失败则保持关闭。回退不降级数据库，后续迁移必须继续满足该安全版本的兼容性。备份/恢复与代码回退是不同动作，不自动把数据库回到更旧时间点。

当前真实旧生产镜像因旧 `kb_chunks` DDL 在升级 schema 上启动失败，而且它本来没有员工会话边界，旧前端还持有 SQL 管理 key。因此旧版不能作为开放服务的回退目标。第一份最低安全版本是本阶段新构建并通过演练的版本，并非“此前已线上验证的版本”。第一次切换若这份最低版本也无法通过，只能保持维护并修复/按已审查计划恢复，不能重新开放旧前端。

## 可观测性和证据

`prometheus.yml` 是新项目的抓取配置，目标为 API、Worker、SQL Agent。采集器需接入对应私有网络；员工指标使用独立 Bearer token。加载现有 `deploy/prometheus/alerts/ai-helpdesk.yml`：保留 Run/模型/工具/事件指标，增加备份超时、恢复验证超时、Case 结果拒绝和登录失败告警。已用 Promtool 校验 18 条规则的语法；真实告警投递渠道尚未连接。

API 只挂载独立的时间戳目录，不能借监控读取备份、候选环境或私有账号文件。发布成功不会刷新“恢复验证成功”的指标。日志在后端和 SQL Agent 输出端统一过滤已登记及环境中的密钥、凭据、Cookie、Authorization 和图片 data URL；不承诺把所有自由文本中的个人信息自动识别为秘密。

CI 的 Linux 演练为恢复私有文件属主使用 sudo；结束后只把 `/tmp/recovery-local.json`、`/tmp/recovery-s3.json` 两个脱敏报告交还 runner，仍保持 `0600`，不开放任何临时凭据、密钥或快照。Linux 容器权限场景已验证这一交接。
