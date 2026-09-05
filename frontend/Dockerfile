# ai-helpdesk 前端 — 多阶段构建
# 阶段 1 在镜像内完成 npm build，因此不需要先在本机跑 npm run build。
# Node 20 已于 2026-04 EOL（不再有安全更新），钉在当时的 Active LTS 上。
FROM node:24-alpine AS build

WORKDIR /app

# 先只拷依赖清单，命中 Docker 层缓存，改业务代码时不用重装依赖。
# **不要退回 `npm install`。** 原先是 `npm ci || npm install`：lock 与
# package.json 对不上时它会静默改用 install，装出一套 lock 里没有的依赖树，
# 镜像因此不可复现——同一个 commit 今天和下周构建出来的东西可以不一样。
# 宁可让构建在这里失败：那说明 lock 该更新了，是一次要进提交的改动。
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── 阶段 2：用 nginx 提供静态站点并反代后端 API ────────────────────────────────
FROM nginx:1.27-alpine AS production

# 用 template 而不是固定 conf：后端地址由 API_UPSTREAM 环境变量注入，
# 单独跑前端时指向 host.docker.internal，整栈 compose 里指向 ai-helpdesk 服务名。
COPY docker/templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

# NGINX_ENTRYPOINT_LOCAL_RESOLVERS: 官方 entrypoint 里的 15-local-resolvers.envsh
# 默认直接 return，不设这个开关就不会导出 NGINX_LOCAL_RESOLVERS，
# 模板里的 resolver 指令会拿到未替换的字面量导致 nginx 启动失败。
ENV API_UPSTREAM=host.docker.internal:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER='^(API_|NGINX_LOCAL_RESOLVERS)'

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
