# ai-helpdesk Console

ai-helpdesk 的调试控制台（React 18 + TypeScript + Vite）。除了普通对话，它把后端每一轮的
**路由决策过程**摊开显示：走的是规则快通道还是三路融合、主 Agent 和辅助 Agent 分别是谁、
综合置信度多少、有没有触发答案策略拦截或转人工。

技术栈：React 18 · TypeScript 5 · Vite 6 · TanStack Query 5 · Tailwind v4 · Vitest 3

## 功能

- 对话调试，每条回复带路由元信息徽章（意图通道 / 主辅 Agent / 置信度 / 策略拦截 / 转人工）
- 按句流式接收 —— 后端的答案策略守卫必须看到完整句子才能判断，所以流式粒度是句不是 token
- 健康检查与 Monitor 摘要
- 知识库检索、文档导入、文件上传
- 一键跑端到端评测

## 源码结构

```text
src/
├── App.tsx
├── components/
│   ├── ChatPanel.tsx          # 对话主面板
│   ├── RoutingInspector.tsx   # 把路由决策过程摊开显示
│   ├── MessageBubble.tsx
│   ├── MonitorPanel.tsx       # Monitor 摘要
│   ├── KnowledgePanel.tsx     # 知识库检索 / 导入 / 上传
│   ├── EvalPanel.tsx          # 端到端评测
│   ├── Sidebar.tsx            # 健康状态 + API 地址设置
│   └── ui.tsx                 # 基础组件
├── api/                       # client / hooks（TanStack Query）/ types
├── hooks/useChatStream.ts     # 按句流式接收
└── lib/format.ts
```

## API 地址

前端统一请求 `/api/*`，由不同环境的代理转发到 FastAPI：

| 运行方式 | `/api/*` 转发到 | 配置位置 |
|----------|-----------------|----------|
| `npm run dev` | `http://localhost:8000` | `vite.config.ts`（可用 `VITE_API_URL` 覆盖） |
| 前端容器单独跑 | `host.docker.internal:8000` | `API_UPSTREAM` 环境变量 |
| 全栈 compose | `ai-helpdesk:8000` | 仓库根的 `docker-compose.yml` |

页面侧边栏的「API 地址」输入框可以临时改成任意地址（例如直接填 `http://localhost:8000`
绕过代理），改动存在 localStorage 里。

## 本地运行

```bash
npm install
npm run dev
```

访问 http://localhost:5173 。后端需要另外起在 `:8000`，
或者直接用仓库根目录的 `scripts/dev.sh` 一条命令同时起前后端。

## 测试与类型检查

```bash
npm test           # vitest run
npm run typecheck  # tsc -b --noEmit
```

测试覆盖 API client 的地址解析、MessageBubble 渲染和 RoutingInspector 的路由元信息展示，
不需要后端在跑。

## Docker

前端镜像是多阶段构建，`npm run build` 在镜像内完成，**不需要**先在本机构建：

```bash
docker compose up -d --build
```

访问 http://localhost:5174 。

想连同 Redis / Postgres / 观测栈 / 后端一起起，用仓库根的 `docker-compose.yml`
里已经并入的 `frontend` 服务，详见后端 README 的「前端和后端一起启动」一节。
