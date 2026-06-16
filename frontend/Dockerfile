# PuddingClaw Frontend Dockerfile
# 多阶段构建：先 build 出静态产物，再用轻量镜像运行 next start

# ==================== 构建阶段 ====================
FROM node:20-alpine AS builder

WORKDIR /app

# 复制依赖文件并安装
COPY package*.json ./
RUN npm ci

# 复制源码并构建
COPY . .
RUN npm run build

# ==================== 运行阶段 ====================
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV FRONTEND_PORT=3000

# 仅复制运行所需文件
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
