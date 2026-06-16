# PuddingClaw Backend Dockerfile
# 生产/开发环境均可使用，默认以非 reload 模式启动

FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（transformers/tokenizers 编译时需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码
COPY . .

# 创建默认日志配置（若项目根目录的 logging.yaml 未通过 volume 挂载则使用此配置）
RUN cat > /app/logging.yaml <<'EOF'
version: 1
disable_existing_loggers: false
formatters:
  default:
    format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: default
    stream: ext://sys.stdout
root:
  level: INFO
  handlers: [console]
EOF

# 持久化目录：sessions、workspace、memory、logs 等
# 运行时可通过 docker volume 挂载到宿主机
RUN mkdir -p /app/sessions /app/workspace /app/memory /app/logs

# 默认环境变量
ENV PYTHONUNBUFFERED=1
ENV BACKEND_HOST=0.0.0.0
ENV BACKEND_PORT=9100

EXPOSE 9100

# 启动后端服务
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "info", "--log-config", "/app/logging.yaml"]
