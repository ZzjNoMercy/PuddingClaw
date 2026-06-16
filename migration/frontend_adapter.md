# V5 前端最小适配说明

> 前提：前端保持 V5 React 版本不变，只做最小改动以兼容后端升级后新增的 SSE 事件。

---

## 后端新增了哪些 SSE 事件？

| 事件名 | 触发时机 | 前端是否必须处理 |
|--------|---------|----------------|
| `retrieval` | RAG/mem0 检索到记忆时 | 否（已有） |
| `token` | LLM 输出 token | 否（已有） |
| `tool_start` | 开始调用工具 | 否（已有） |
| `tool_end` | 工具调用结束 | 否（已有） |
| `done` | 对话完成 | 否（已有） |
| `title` | 首条消息完成后生成标题 | 否（已有） |
| **`context_usage`** | **每次 tool 执行后 + 对话结束时** | **可选** |
| **`new_response`** | **工具执行后 LLM 重新开始生成时** | **可选** |
| **`error`** | **发生 API 错误时** | **建议处理** |

---

## 改动点 1：error 事件（建议接入）

**作用**：后端现在会把 429/401/503/Timeout 转成中文友好提示，前端应展示给用户。

**文件**：`src/lib/api.ts` 或你封装 SSE 的地方

```typescript
// 在 event.data 解析处增加：
if (event.event === 'error') {
  const data = JSON.parse(event.data);
  // data.error 是用户友好的中文错误消息
  appendMessage({
    role: 'assistant',
    content: `⚠️ ${data.error}`,
    isError: true,
  });
}
```

---

## 改动点 2：context_usage 事件（可选，1 分钟搞定）

**作用**：实时显示上下文窗口使用率，长对话时给用户心理准备。

**文件**：`src/components/chat/ChatPanel.tsx`（或你的聊天主组件）

```typescript
// 在组件 state 中增加：
const [contextUsage, setContextUsage] = useState<{ used: number; total: number; pct: number } | null>(null);

// 在 SSE 事件处理中：
if (event.event === 'context_usage') {
  const data = JSON.parse(event.data);
  setContextUsage({
    used: data.used_tokens,
    total: data.total_tokens,
    pct: data.percentage,
  });
}

// 在 JSX 中展示（放在输入框上方或侧边栏）：
{contextUsage && (
  <div className="text-xs text-gray-500 px-2 py-1">
    上下文: {contextUsage.used.toLocaleString()} / {contextUsage.total.toLocaleString()} tokens
    ({contextUsage.pct}%)
    {contextUsage.pct > 85 && <span className="text-red-500 ml-1">⚠️ 即将截断</span>}
  </div>
)}
```

效果预览：
```
上下文: 81,700 / 262,144 tokens (31.2%)
```

---

## 改动点 3：new_response 事件（可选，可忽略）

**作用**：当 Agent 执行完工具（如搜索、读文件）后重新开始生成回复时触发。

**使用场景**：如果你希望工具执行前后的内容分成两个气泡展示，可以用这个事件做分割。

**如果不改**：所有内容会合并在一个气泡里，和 V5 行为一致，完全没问题。

---

## 一句话总结

> **只改 `error` 事件处理 = 最优先（用户体验提升明显）**
> **加 `context_usage` 显示 = 锦上添花（1 分钟搞定）**
> **`new_response` = 可完全忽略**

不改任何前端代码也能跑，只是看不到用量和错误提示会变原生英文。
