# AI SQL Agent 常用入口。每个目标都是一条能直接抄到终端的命令，不藏逻辑。
.DEFAULT_GOAL := help
PY ?= python
COMPOSE_PROD := docker compose --project-directory . -f deploy/docker-compose.prod.yml
COMPOSE_VLLM := docker compose --project-directory . -f deploy/docker-compose.vllm.yml
COMPOSE_PGV  := docker compose --project-directory . -f deploy/docker-compose.pgvector.yml

.PHONY: help install test test-ci run demo demo-memory check-env semantic-check \
        benchmark-smoke gate gate-smoke docker-build up down logs vllm-up vllm-down \
        pgvector-up pgvector-down clean

help: ## 列出所有目标
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 以可编辑模式安装应用包与测试依赖
	pip install -e ".[dev]"

test: ## 全量离线测试（pytest；conftest 会屏蔽本机 .env 与模型凭证）
	$(PY) -m pytest tests -q

test-ci: ## CI 同款入口（unittest；不读 .env 的环境里用）
	AI_SQL_LOG_LEVEL=ERROR $(PY) -m unittest discover -s tests -v

run: ## 本地起 API 与内置网页（默认 http://localhost:8000，端口见 .env 的 AI_SQL_PORT）
	scripts/run_api.sh

demo: ## 端到端 Demo：单库 + 跨库（离线 Mock 模型）
	$(PY) examples/end_to_end.py

demo-memory: ## 长短期记忆 + 路由联合 Demo
	$(PY) examples/memory_end_to_end.py --user-id user-001 --session-id session-001 --memory-db runtime_data/my_memory.db

check-env: ## 环境自检：Python 版本、依赖、SQLite、核心组件、pgvector 连通性
	$(PY) scripts/check_env.py

semantic-check: ## 语义元数据自洽：导出模板 → 校验 → 显示生效的一份
	$(PY) -m ai_sql_agent.semantic export /tmp/ai_sql_semantic
	$(PY) -m ai_sql_agent.semantic validate /tmp/ai_sql_semantic
	$(PY) -m ai_sql_agent.semantic show

benchmark-smoke: ## 评测冒烟子集（离线）
	$(PY) -m benchmark.run_benchmark --smoke

gate: ## 发布门禁：完整 150 条 + 留出集，需要模型凭证
	$(PY) -m benchmark.gate --repeat 3

gate-smoke: ## 发布门禁冒烟档（离线，CI 用）
	$(PY) -m benchmark.gate --repeat 3 --smoke

docker-build: ## 构建生产镜像
	docker build -f deploy/Dockerfile -t ai-sql-agent:local .

up: ## 生产编排：应用 + PostgreSQL(pgvector)
	$(COMPOSE_PROD) up -d --build

down: ## 停掉生产编排
	$(COMPOSE_PROD) down

logs: ## 跟踪应用日志
	$(COMPOSE_PROD) logs -f app

vllm-up: ## 内网私有化编排：应用 + PostgreSQL + vLLM
	$(COMPOSE_VLLM) up -d --build

vllm-down: ## 停掉 vLLM 编排
	$(COMPOSE_VLLM) down

pgvector-up: ## 只起本地 pgvector（开发用）
	$(COMPOSE_PGV) up -d

pgvector-down: ## 停掉本地 pgvector
	$(COMPOSE_PGV) down

clean: ## 清理缓存与打包产物
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
	rm -rf .pytest_cache build dist src/*.egg-info
