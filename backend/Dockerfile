# PuddingClaw Backend Dockerfile
# 生产/开发环境均可使用，默认以非 reload 模式启动

FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（transformers/tokenizers 编译时需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# SkillHub CLI 是后端运行依赖：用于在容器内安装/管理 SkillHub 技能。
# 只安装 CLI，不安装商店运行时；技能目录由 docker-compose volume 挂载到 /app/skills。
RUN curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh \
    | bash -s -- --cli-only \
    && /root/.local/bin/skillhub --version
ENV PATH="/root/.local/bin:${PATH}"

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# 预下载 tokenizer 缓存到镜像，避免运行时访问 HuggingFace / tiktoken 下载超时。
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache
ENV HF_HOME=/app/.hf_cache

RUN mkdir -p /app/.tiktoken_cache /app/.hf_cache && python - <<'PY'
import tiktoken
# 触发下载并缓存 cl100k_base
tiktoken.get_encoding("cl100k_base")
print("tiktoken cl100k_base cached")
PY

# DeepSeek 官方 tokenizer 作为可选缓存（失败不中断构建）。
# 若本机挂了梯子，可传入代理构建，直接走 huggingface.co：
#   docker build --build-arg HTTP_PROXY=http://host.docker.internal:15732 \
#                --build-arg HTTPS_PROXY=http://host.docker.internal:15732 \
#                --build-arg NO_PROXY=localhost,127.0.0.1 -t puddingclaw-backend .
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY:-} HTTPS_PROXY=${HTTPS_PROXY:-} NO_PROXY=${NO_PROXY:-}
RUN python - <<'PY' || echo "DeepSeek tokenizer pre-cache failed, will fall back at runtime"
import os
# 默认直接访问 huggingface.co；大陆环境可设置 HF_ENDPOINT=https://hf-mirror.com
# os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from transformers import AutoTokenizer
AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V3", trust_remote_code=True)
print("DeepSeek tokenizer cached")
PY

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
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

EXPOSE 9100

# 启动后端服务（-u 保证容器内日志实时输出）
CMD ["python", "-u", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "info"]
