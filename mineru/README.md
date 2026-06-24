# MinerU 部署目录

本目录只保留一个可选的本地构建 Dockerfile，用于在官方 Docker 镜像不可用或需要自定义时手动构建 MinerU 服务镜像。

## 推荐部署方式

**默认请使用项目提供的自动部署脚本：**

```bash
python scripts/setup-mineru.py
# 开发调试时前台运行，实时查看日志：
# python scripts/setup-mineru.py --foreground
```

该脚本会：

1. 检测操作系统、GPU、Docker 环境
2. 推荐并执行合适的部署方式
3. 使用 uv 安装 MinerU base 包（作为 backend 的 optional dependency，不含 vllm/lmdeploy GPU 后端，避免与 backend 核心依赖冲突）
4. 自动预下载 pipeline 模型到 `~/.mineru/models`（约 10GB+；可用 `--skip-model-download` 跳过）
5. 或拉取 MinerU 官方 Docker 镜像（容器内独立安装 `mineru[all]`，可使用 GPU 后端）
5. 启动 `mineru-api` 服务
6. 将 `MINERU_URL` 自动写回 `backend/.env`

## 什么时候需要本 Dockerfile

- 官方镜像 `opendatalab/mineru` 无法拉取
- 需要自定义基础镜像或 CUDA 版本
- 需要预装特定版本的 MinerU 依赖

## 手动构建

```bash
docker build -t puddingclaw/mineru:local .
```

GPU 版本：

```bash
docker build \
  --build-arg BASE_IMAGE=nvidia/cuda:12.1.0-devel-ubuntu22.04 \
  -t puddingclaw/mineru:gpu .
```

## 参考

- [MinerU 官方文档](https://github.com/opendatalab/MinerU)
- [MinerU Docker 部署文档](https://opendatalab.github.io/MinerU/zh/quick_start/docker_deployment/)
- [项目架构文档](../docs/ARCHITECTURE.md)
