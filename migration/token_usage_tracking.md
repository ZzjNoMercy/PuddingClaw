# Token 用量追踪功能迁移指南

> **目标**：为 Agent 后端增加每轮 LLM 调用的 Token 用量记录，支持本地 jsonl + MySQL 双写，用于后续 TPM 估算和监控大盘。  
> **影响范围**：`magic-mirror-backend`（Django ORM + Migration）、`agent-backend`（核心逻辑 + API）

---

## 一、交付文件清单

### 1.1 magic-mirror-backend（Django 后端）

| # | 修改文件 | 说明 |
|---|---------|------|
| 1 | `apps/agent/models.py` | 新增 `TokenUsage` Django Model |
| 2 | `apps/agent/migrations/0002_add_token_usage.py` | 自动生成的 migration（已随代码提供） |

### 1.2 agent-backend（FastAPI 后端）

| # | 修改文件 | 说明 |
|---|---------|------|
| 1 | `config.py` | `create_chat_llm()` 增加 `stream_usage=True`，启用 LangChain usage 返回 |
| 2 | `graph/agent.py` | `_run_agent_stream()` 捕获 `usage_metadata`，每轮 model 调用后记录 token |
| 3 | `token_usage_db.py` | **新增**：pymysql 直写 MySQL，复用 `user_resolver.py` 的数据库配置 |
| 4 | `stats_manager.py` | `record_token_usage()` 增加 `round_num` / `start_time` 字段 |
| 5 | `api/stats_api.py` | 新增 `/stats/tokens/*` 三个查询接口 |

---

## 二、数据库 Migration（必做）

### 2.1 Development（本地）

```bash
cd magic-mirror-backend
python manage.py migrate agent
```

验证表是否创建：
```sql
SHOW TABLES LIKE 'agent_token_usage';
DESCRIBE agent_token_usage;
```

### 2.2 SIT / PROD（K8s 部署）

**方式一：Pod 自动执行（推荐）**

Web 容器启动时会自动执行 `python manage.py migrate`（通过 `entrypoint.sh`）。

只需重启 Pod：
```bash
# SIT
kubectl rollout restart deployment magic-mirror-web -n magic-mirror-sit

# PROD
kubectl rollout restart deployment magic-mirror-web -n magic-mirror-prod
```

**方式二：手动执行 Job**

```bash
# 修改 namespace 为对应环境后执行
kubectl apply -f k8s/db-migrate-job.yaml
kubectl logs job/magic-mirror-db-migrate -n <namespace>
kubectl delete job magic-mirror-db-migrate -n <namespace>
```

> ⚠️ 如果 K8s 多 Pod 无 sticky session，jsonl 数据会分散在各 Pod 本地，但 MySQL 数据是集中存储的，不受影响。

---

## 三、核心改动详解

### 3.1 启用 LLM usage 返回（`config.py`）

```python
return ChatDeepSeek(
    ...
    stream_usage=True,   # ← 新增
    **client_kwargs,
)

return ChatOpenAI(
    ...
    stream_usage=True,   # ← 新增
    **client_kwargs,
)
```

作用：让 LangChain 在 stream 的最后一个 chunk 中返回 `usage_metadata`，包含真实的 `input_tokens` / `output_tokens`。

### 3.2 Agent 核心捕获逻辑（`graph/agent.py`）

#### 触发时机

在 `_run_agent_stream()` 的 `updates` 模式、`node_name == "model"` 处：

```
用户提问 → LLM Stream → usage_metadata（最后一个 chunk）
         → node_name="model" 更新
         → 读取 agent_msg.usage_metadata
         → _record_token_usage() 双写 jsonl + MySQL
         → （如果有 tool_calls）→ 工具执行 → 下一轮 model
```

#### 每轮记录的数据结构

```python
{
    "user_id": "0549252",
    "session_id": "session-xxx",
    "agent_type": "aisight",
    "round": 1,                    # 第几轮 LLM 调用
    "input_tokens": 8000,          # 该轮 prompt tokens（LLM 返回的实际值）
    "output_tokens": 1200,         # 该轮 completion tokens
    "total_tokens": 9200,
    "start_time": 1780993283.12,   # 该轮调用开始时间戳
    "timestamp": 1780993285.45     # 记录写入时间
}
```

#### Fallback 机制

如果 LLM 接口不支持 `stream_options`（某些私有化部署），`usage_metadata` 为空，自动 fallback 到 tiktoken 估算 `output_tokens`，`input_tokens` 记为 0。

### 3.3 MySQL 写入模块（`token_usage_db.py`，新增）

复用 `user_resolver.py` 的数据库配置（`DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`），保持与 Django 后端环境一致。

写入逻辑：
- 成功：静默写入
- 失败：静默跳过，不阻塞主流程（jsonl 作为 fallback）

### 3.4 Stats 查询 API（`api/stats_api.py`）

新增 3 个接口：

| 接口 | 说明 |
|------|------|
| `GET /api/stats/tokens/ranking?limit=10` | 用户 Token 消耗排行 |
| `GET /api/stats/tokens/total` | 全局累计 Token 统计 |
| `GET /api/stats/tokens/daily?days=7` | 最近 N 天每日 Token 消耗趋势 |

---

## 四、部署步骤（agent-backend）

### 4.1 文件同步

将以下修改后的文件同步到目标环境：

```
agent-backend/
├── config.py                    # stream_usage=True
├── graph/agent.py               # 捕获 usage_metadata + 每轮记录
├── token_usage_db.py            # 新增：MySQL 写入
├── stats_manager.py             # round_num / start_time 支持
└── api/stats_api.py             # Token 查询接口
```

### 4.2 重启服务

```bash
# 停止旧进程
ps aux | grep "agent-backend\|uvicorn" | grep -v grep | awk '{print $2}' | xargs kill -9

# 启动
python app.py
# 或
uvicorn app:app --reload
```

### 4.3 验证

问一个简单问题（如"你好"），然后检查：

```bash
# 1. 本地 jsonl
cat workspace/0549252/aisight/stats/tokens/2026-06-09.jsonl

# 2. MySQL
SELECT * FROM agent_token_usage ORDER BY id DESC LIMIT 5;
```

正常应看到：
- jsonl 中有新记录，`round` 从 1 开始
- MySQL 中有对应记录，`created_at` 自动填充

---

## 五、监控查询示例

### 5.1 今日各用户总用量排行

```sql
SELECT user_id,
       SUM(input_tokens) AS in_tok,
       SUM(output_tokens) AS out_tok,
       SUM(total_tokens) AS total_tok
FROM agent_token_usage
WHERE DATE(created_at) = CURDATE()
GROUP BY user_id
ORDER BY total_tok DESC;
```

### 5.2 峰值 TPM（按分钟聚合）

```sql
SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H:%i') AS minute,
       SUM(total_tokens) AS tpm
FROM agent_token_usage
WHERE created_at >= NOW() - INTERVAL 1 HOUR
GROUP BY minute
ORDER BY tpm DESC
LIMIT 10;
```

### 5.3 单次对话各轮明细

```sql
SELECT round_num,
       input_tokens,
       output_tokens,
       total_tokens,
       start_time
FROM agent_token_usage
WHERE session_id = 'session-xxx'
ORDER BY round_num;
```

### 5.4 平均单次对话消耗

```sql
SELECT AVG(total_tokens) AS avg_per_session,
       AVG(input_tokens) AS avg_input,
       AVG(output_tokens) AS avg_output
FROM agent_token_usage;
```

---

## 六、常见问题

### Q1: MySQL 有数据但 jsonl 没有？

正常。jsonl 是本地 fallback，如果 `_record_token_usage` 中 jsonl 写入抛异常（如目录权限问题），MySQL 仍可能成功。以 MySQL 为准。

### Q2: MySQL 里没有数据？

排查步骤：
1. 检查 `agent-backend` 日志是否有 `UnboundLocalError` / `TypeError` 等异常
2. 手动测试 `token_usage_db.insert_token_usage(...)` 看是否成功
3. 检查数据库连接配置（`user_resolver.py` 中的 `DB_HOST` 等）
4. 确认表已创建：`SHOW TABLES LIKE 'agent_token_usage'`

### Q3: `input_tokens` 为什么这么大？

Agent 多轮调用时，每轮的 `input_tokens` 包含**累积的历史上下文**（system prompt + 所有 messages）。长任务累加后总量会很大，这是**每次 API 调用的实际消耗**，从成本角度是正确的。

### Q4: 只想要"新增"token，不要累积值？

TPM 估算需要累积值（反映实际 API 成本）。如需"新增"token，可用初始 `context_usage` 与每轮差值计算，或只看 `output_tokens`。

### Q5: SIT/PROD 的 migration 执行后表不存在？

确认执行了 `python manage.py migrate agent`，不是 `makemigrations`。如果多数据库环境，确认 `DATABASES['default']` 指向正确的 RDS。

---

## 七、前端扩展（可选）

后端 API 已就绪，前端如需 Token 监控页面：

1. `magic-mirror-frontend/apps/web-antd/src/api/core/stats.ts` 增加 token 接口调用
2. 新建 `/views/agent/token-monitor.vue` 页面
3. `router/routes/modules/agent.ts` 增加路由（建议加 `permission: ['admin']`）

组件建议：
- 顶部卡片：总 Input / 总 Output / 活跃用户
- 表格：用户 Token 排行
- 表格/折线图：最近 7 天每日趋势

---

*文档结束。如有问题，贴 `agent-backend` 日志和 MySQL 查询结果即可。*
