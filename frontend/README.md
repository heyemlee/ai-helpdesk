# ai-helpdesk Console

ai-helpdesk 的员工工作台（React 18 + TypeScript + Vite）。主导航只有三项——**对话、知识库、
工单**——是客服员工每天做的三件事；监控、评测、编排检查器、模型信息、SOP 和连接配置都收在
「管理与诊断」里。

这是给员工用的界面，不是客户入口：知识文档、SOP、工单接口都要员工钥匙（`X-API-Key`，
后端配了 `AI_HELPDESK_API_KEY` 时生效），谁能看什么由后端判，界面上没有"切换成客户视角"
这种开关。

技术栈：React 18 · TypeScript 5 · Vite 6 · TanStack Query 5 · Tailwind v4 · Vitest 3

## 功能

- **对话**：消息流、新建对话、刷新后恢复历史。每条回复下面列出本轮用到的知识片段
  （能点回原文）、订单数据是不是实时查到的；需要人工时显示工单号、状态和通知是否送达
- **知识库**：打开就列出库里的文档（标题、摘要、来源、导入时间，文档数与片段数分开报），
  点开读正文；按标题和正文筛选；「按问题检索」走和对话相同的检索链路；导入后直接能打开
- **工单**：编号、问题、状态、创建时间；按状态筛选；详情里看转人工原因和关联对话，
  状态流转走后端的状态机，失败时保留原状态并提示
- **学习模式**（默认关）：逐步讲这一轮实际发生的事——意图识别、知识检索、专家选择、
  SQL 委派、回答检查、转人工——每一句都只转述后端在响应里报告的内容；
  刷新之后按 turn_id 从事件日志取回那一轮再讲
- **管理与诊断**：服务与模型信息、连接配置、模型连接（对话模型和向量模型，测试通过才保存，Key 只进不出）、SOP 只读、Monitor、端到端评测、编排检查器
- 按句流式接收 —— 后端的答案策略守卫必须看到完整句子才能判断，所以流式粒度是句不是 token
- 中英文切换，默认英文；键盘可达，窄屏用底部导航

## 源码结构

```text
src/
├── App.tsx                    # 外壳：三项主导航 + 管理与诊断
├── components/
│   ├── Sidebar.tsx            # 导航、待处理工单数、服务状态
│   ├── ChatPanel.tsx          # 对话主面板、「可以查询什么」
│   ├── MessageBubble.tsx      # 回复 + 知识依据 + 订单数据提示 + 工单卡
│   ├── TurnExplanation.tsx    # 学习模式的逐步讲解
│   ├── KnowledgePanel.tsx     # 文档列表 / 筛选 / 按问题检索 / 导入
│   ├── KnowledgeDocument.tsx  # 单篇文档正文与检索片段
│   ├── TicketsPanel.tsx       # 工单列表与详情
│   ├── AdminPanel.tsx         # 管理与诊断：概览、连接、SOP，内嵌下面四个
│   ├── ModelSettings.tsx      # 模型连接：对话模型 / 向量模型的测试、保存、恢复和重建进度
│   ├── MonitorPanel.tsx       # Monitor 摘要
│   ├── EvalPanel.tsx          # 端到端评测
│   ├── RoutingInspector.tsx   # 编排检查器
│   └── ui.tsx                 # 基础组件（含加载 / 空 / 出错的状态块）
├── api/                       # client / hooks（TanStack Query）/ types
├── hooks/useChatStream.ts     # 按句流式接收
├── hooks/useRestoredTurn.ts   # 刷新前的那一轮：按 turn_id 从事件日志取回
├── i18n/                      # 文案目录（英文为基准）
└── lib/                       # route（hash 路由）/ format / highlight / errors
```

## API 地址

前端统一请求 `/api/*`，由不同环境的代理转发到 FastAPI：

| 运行方式 | `/api/*` 转发到 | 配置位置 |
|----------|-----------------|----------|
| `npm run dev` | `http://localhost:8080` | `vite.config.ts`（可用 `VITE_API_URL` 覆盖） |
| 前端容器单独跑 | `host.docker.internal:8080` | `API_UPSTREAM` 环境变量 |
| 全栈 compose | `ai-helpdesk:8000`（Docker 内网里的员工实例） | 仓库根的 `docker-compose.yml` |

默认配置打开就能用，不需要先填用户 ID、会话 ID 或 API 地址。要把工作台指向另一套后端时，
到「管理与诊断 → 概览 → 连接」里改：API 地址和用户 ID 存在 localStorage；员工钥匙只存在
sessionStorage，关掉标签页即失效。

页面位置在 URL 的 hash 里（`#/knowledge/doc/<id>`、`#/tickets/<id>`、`#/admin/sops`），
刷新和浏览器后退都能回到原处，不需要给代理或后端加路由回落。

## 本地运行

```bash
npm install
npm run dev
```

访问 http://localhost:5173 。后端需要另外起在 `:8080`（8000 归同机的 ai-sql-agent），
或者直接用仓库根目录的 `scripts/dev.sh` 一条命令同时起前后端。

## 测试与类型检查

```bash
npm test           # vitest run
npm run typecheck  # tsc -b --noEmit
```

测试覆盖 API client、对话气泡（知识依据 / 工单卡 / 学习模式）、知识库与工单各页的
加载 / 空 / 无匹配 / 出错状态、刷新后取回的那一轮，以及用真后端录制的事件流回放，
不需要后端在跑。

## Docker

前端镜像是多阶段构建，`npm run build` 在镜像内完成，**不需要**先在本机构建：

```bash
docker compose up -d --build
```

访问 http://localhost:5174 。

想连同 Redis / Postgres / 观测栈 / 后端一起起，用仓库根的 `docker-compose.yml`
里已经并入的 `frontend` 服务，详见后端 README 的「前端和后端一起启动」一节。

## 界面样式

`src/console.css` 保留为原型同步的基础组件层；产品界面的配色、布局、响应式样式
统一放在 `src/workspace.css`，由 `index.css` 在基础层之后引入。修改应用设计时请编辑
`workspace.css`，避免被 `predev` / `prebuild` 的原型同步覆盖。

桌面使用侧栏导航，手机使用底部导航；连接配置在「管理与诊断」里保存后生效。编排检查器
只在学习模式里从某一轮打开，窄屏时使用原生模态对话框，支持焦点约束与 Esc 关闭。刷新之后的历史消息
按 turn_id 从事件日志取回那一轮，工单详情里也能打开转人工的那一轮。对话示例
先填入草稿，确认发送后才发起请求；切换页面保留草稿，新建对话清除草稿。
