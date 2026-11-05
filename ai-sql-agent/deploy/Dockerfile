# 从仓库根构建：docker build -f deploy/Dockerfile -t ai-sql-agent:local .
# 换 Python 版本：--build-arg PYTHON_VERSION=3.12（默认 3.11，与 CI 一致）
ARG PYTHON_VERSION=3.11
#
# ---- 构建阶段：先装依赖再装应用包，源码改动不打掉依赖层缓存 ----
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 依赖清单只有一份（pyproject.toml）；从里面抽出来单独装一层
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install --prefix=/install -r /tmp/requirements.txt

# 应用包本体（含 api/static 与 resources/demo_sql 两组包内资源）
COPY README.md ./
COPY src ./src
RUN pip install --prefix=/install --no-deps .


# ---- 运行阶段 ----
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AI_SQL_LOG_FORMAT=json \
    AI_SQL_AUTH_MODE=api_key

# 非 root 运行
RUN useradd --create-home --uid 10001 ai_sql

COPY --from=builder /install /usr/local

# 工作目录只放运行期产物（记忆库、审计库、Demo 数据）；代码已装进 site-packages
WORKDIR /app
COPY .env.example /app/.env.example
RUN mkdir -p /app/runtime_data && chown -R ai_sql:ai_sql /app

USER ai_sql

EXPOSE 8000

# 存活探针交给编排系统调 /health，这里给一个基础的容器级健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3)"

# 单进程多线程：Pipeline 与索引在进程内共享，多 worker 会各建一份索引。
# 需要更高并发时优先加副本数，而不是加 worker。
CMD ["uvicorn", "ai_sql_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
