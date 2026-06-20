# PuddingClaw Frontend Dockerfile
# 多阶段构建：先 build 出静态产物，再用轻量镜像运行 next start

# ==================== 构建阶段 ====================
FROM node:20-alpine AS builder

WORKDIR /app

# 复制依赖文件并安装
COPY package*.json ./
RUN npm ci

# API 代理地址只影响 Next 构建，不应使依赖安装层失效。
ARG BACKEND_INTERNAL_URL=http://backend:9100
ENV BACKEND_INTERNAL_URL=$BACKEND_INTERNAL_URL

# 复制源码并构建
COPY . .
RUN npm run build

# ==================== 运行阶段 ====================
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
ENV BACKEND_INTERNAL_URL=http://backend:9100

# 仅复制运行所需文件
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
