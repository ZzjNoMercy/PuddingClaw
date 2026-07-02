# Higress 多模态 Embedding 路由说明

`qwen2.5-vl-embedding` / `qwen3-vl-embedding` 使用 DashScope 原生多模态 Embedding API，不是 OpenAI-compatible `/v1/embeddings`。

因此不要把它加到现有 `ai-route-text-embedding` 里。现有文本 embedding 路由适合：

```text
/v1/embeddings + text-embedding-v4
```

多模态索引需要单独路由：

```text
/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding
```

后端对应配置：

```bash
PUDDINGCLAW_MULTIMODAL_EMBED_BASE_URL=http://localhost:8080
PUDDINGCLAW_MULTIMODAL_EMBED_ROUTE_PATH=/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding
PUDDINGCLAW_MULTIMODAL_EMBED_MODEL=qwen2.5-vl-embedding
PUDDINGCLAW_MULTIMODAL_EMBED_DIM=1024
```

如果不配置 `PUDDINGCLAW_MULTIMODAL_EMBED_BASE_URL`，后端会走 DashScope SDK direct，需要：

```bash
DASHSCOPE_API_KEY=...
```

如果本地 Higress AI Proxy 已经给 qwen / multi-model provider 配了同一个 DashScope token，后端会在
`config.json.multimodal_embedding.api_key`、`DASHSCOPE_API_KEY`、`EMBEDDING_API_KEY` 都为空时自动复用 Higress 里的 token。

## 重要限制

- 多模态 embedding 的图片输入不能把本地 `file://...` 直接交给远端 DashScope 读取。
- 后端经 HTTP / Higress 调用时会把图片转成 base64 data URL。
- 如果走 DashScope SDK direct，则 SDK 可处理 notebook 中的 `file://...` 形式。
- Higress 的现有 `ai-proxy` OpenAI-compatible route 不保证支持 DashScope 原生多模态 embedding；若使用 Higress，需要配置 native passthrough 或等价能力。

## 路由模板（示意）

以下模板只作为 Higress Console / YAML 配置参考。不要直接覆盖现有 `data/higress` live 配置。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ai-route-qwen-vl-embedding-native.internal
  namespace: higress-system
  labels:
    higress.io/domain_higress-default-domain: "true"
    higress.io/internal: "true"
    higress.io/resource-definer: higress
  annotations:
    higress.io/comment: "Native DashScope multimodal embedding route for qwen2.5-vl-embedding."
    higress.io/destination: llm-multi-model.internal.dns:443
    higress.io/ignore-path-case: "true"
spec:
  ingressClassName: higress
  rules:
    - http:
        paths:
          - path: /api/v1/services/embeddings/multimodal-embedding/multimodal-embedding
            pathType: Prefix
            backend:
              resource:
                apiGroup: networking.higress.io
                kind: McpBridge
                name: default
```

如果你的 Higress route 不负责注入 DashScope API Key，后端仍需要 `DASHSCOPE_API_KEY`，并会把它作为 `Authorization: Bearer ...` 发送到该 route。
