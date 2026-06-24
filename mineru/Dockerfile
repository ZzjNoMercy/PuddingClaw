# MinerU 服务镜像
#
# 说明：
# - 本镜像基于 MinerU 官方 Python 包构建，使用 MinerU 自带的 API 服务（mineru-api）。
# - 模型文件不打包在镜像内，启动时通过 volume 挂载 ~/.mineru/models。
# - CPU 模式（默认）：使用 pipeline 后端，适合无 GPU 环境。
# - GPU 模式：构建时传入 BASE_IMAGE=nvidia/cuda:12.1.0-devel-ubuntu22.04 并配合 --gpus all 运行。
#
# 用法：
#   CPU:  docker build -t puddingclaw/mineru:cpu .
#   GPU:  docker build --build-arg BASE_IMAGE=nvidia/cuda:12.1.0-devel-ubuntu22.04 -t puddingclaw/mineru:gpu .
#
# 参考：
# - https://github.com/opendatalab/MinerU
# - https://opendatalab.github.io/MinerU/zh/quick_start/docker_deployment/

ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 MinerU（包含所有核心功能和 API 服务）。
# Docker 容器与 backend 依赖隔离，因此可使用 mineru[all] 以启用 GPU 后端。
# 使用国内镜像源加速，必要时可通过 build-arg 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --upgrade pip -i ${PIP_INDEX_URL} && \
    pip install --no-cache-dir \
        -i ${PIP_INDEX_URL} \
        --extra-index-url ${EXTRA_INDEX_URL} \
        "mineru[all]"

# 确保配置和模型目录存在（挂载点）
RUN mkdir -p /root/.mineru/models /root/.mineru/output

# 默认环境变量
ENV MINERU_MODEL_SOURCE=modelscope
ENV MINERU_DEVICE=cpu
ENV MINERU_API_HOST=0.0.0.0
ENV MINERU_API_PORT=8002

EXPOSE 8002

# 使用 MinerU 自带 API 服务
# 启动命令可通过 docker run 覆盖，例如指定 GPU 后端
CMD ["sh", "-c", "mineru-api --host ${MINERU_API_HOST} --port ${MINERU_API_PORT}"]
