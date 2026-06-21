# 点击"新对话"不立即创建 Session 的实现方案

> 目标：用户点击侧边栏的「新对话」时，**不要**立即向后端创建 Session，而是在用户真正发送第一条消息时**按需懒创建**。这样可以避免产生大量空 Session，也符合主流 Chat 类产品的交互习惯。

---

## 1. 核心思路

| 步骤 | 行为 |
|------|------|
| 点击「新对话」 | 前端只做 UI 状态切换，不归档 Session |
| 用户输入并发送第一条消息 | 前端先判断当前是否是"占位" Session，如果是则先调后端创建真实 Session，再用真实 Session ID 发送消息 |
| 后续消息 | 已经落在真实 Session 上，正常追加即可 |

关键点：

1. 用一个**占位 ID**（如 `"default"`）表示"尚未创建真实 Session"的状态。
2. 发送消息前调用 `ensureSession()` / 等价逻辑，把占位 ID 换成真实 ID。
3. 创建 Session 的时机要**在写入本地消息缓存之后、调用 SSE 之前**，保证后续 SSE 使用的 `session_id` 是真实 ID。

---

## 2. 前端状态设计

### 2.1 Session 相关状态

```ts
// 当前选中的 session id
const [sessionId, setSessionId] = useState("default");

// 已存在的会话列表（从后端拉取）
const [sessions, setSessions] = useState<SessionMeta[]>([]);

// 每个 session 的消息缓存（避免切换时重复拉取）
const messagesMapRef = useRef<Record<string, ChatMessage[]>>({});
```

### 2.2 规则

- `"default"` 不是真实 Session，只是占位符。
- 当 `sessionId === "default"` 时，消息区显示空对话。
- 当用户发送第一条消息时，如果 `sessionId === "default"`，先创建真实 Session。

---

## 3. 点击「新对话」时做什么

**不创建 Session**，只做三件事：

```tsx
<button
  onClick={() => {
    // 1. 如果在别的页面，先回到首页 / 聊天页
    if (pathname !== "/") {
      router.push("/");
    }

    // 2. 切换到占位 session，表示"这是一个全新的空对话"
    setSessionId("default");
  }}
>
  新对话
</button>
```

注意：

- 不要调用 `apiCreateSession()`。
- 不要往 `sessions` 列表里追加任何东西。
- 清空输入框、清空当前消息展示即可。

---

## 4. 发送消息时懒创建 Session

在 `sendMessage` 的最前面加入这段逻辑：

```ts
const sendMessage = useCallback(async (text: string) => {
  if (!text.trim() || isStreaming) return;

  // ━━ 懒创建 Session ━━
  // 只有在占位 session（或当前 session 已不存在）时才创建
  if (sessionIdRef.current === "default") {
    await createSession();
  }

  // 此时 sessionIdRef.current 已经是真实 ID
  const sendSessionId = sessionIdRef.current;

  // 1. 先把用户消息写入本地缓存
  const userMsg: ChatMessage = {
    id: `user-${Date.now()}`,
    role: "user",
    content: text,
    timestamp: Date.now(),
  };

  const assistantMsg: ChatMessage = {
    id: `assistant-${Date.now()}`,
    role: "assistant",
    content: "",
    timestamp: Date.now(),
  };

  updateSessionMessages(sendSessionId, (prev) => [...prev, userMsg, assistantMsg]);

  // 2. 用真实 sendSessionId 调 SSE
  for await (const event of streamChat(text, sendSessionId, signal)) {
    // ... 处理 token / tool / title 等事件
  }
}, [isStreaming, createSession]);
```

### 4.1 `createSession` 实现

```ts
const createSession = useCallback(async () => {
  const meta = await apiCreateSession(); // POST /api/sessions

  // 加入会话列表
  setSessions((prev) => [
    { id: meta.id, title: meta.title, updated_at: Date.now() / 1000 },
    ...prev,
  ]);

  // 预置空缓存，避免 setSessionId 时把本地消息覆盖掉
  messagesMapRef.current[meta.id] = [];

  // 切换到新 session
  setSessionId(meta.id);
}, [setSessionId]);
```

---

## 5. 后端接口要求

只需要一个标准的创建 Session 接口：

```
POST /api/sessions
Response: { id: string, title: string }
```

后端行为：

- 创建一条新的 Session 记录（数据库 / 内存 / 文件等）。
- 返回新生成的 `id` 和默认 `title`。
- 可选：在用户发送第一条消息后，后端自动生成标题并返回 `title` 事件，前端更新会话列表。

---

## 6. 边界情况处理

### 6.1 页面刷新后保留状态

如果用户刷新页面时正处在「新对话」状态（`sessionId === "default"`），应该保持空对话，不要自动切到最近一个真实 Session。

```ts
useEffect(() => {
  // 从 sessionStorage 恢复上次选中的 session
  const saved = sessionStorage.getItem("chat_session_id");
  if (saved && (saved === "default" || sessions.some((s) => s.id === saved))) {
    setSessionId(saved);
    return;
  }

  // 如果当前是 default，保持 default，不要跳走
  if (sessionIdRef.current === "default") return;

  // 否则兜底：切到最近活跃的 session
  const latest = [...sessions].sort((a, b) => b.updated_at - a.updated_at)[0];
  if (latest) setSessionId(latest.id);
}, [sessions]);
```

### 6.2 从其他页面触发新对话

例如从「技能」页面点击某个技能后想在新对话中打开：

```ts
const triggerSkillCreator = useCallback(() => {
  setPendingInput("/skill-creator 帮我创建一个新的 Skill");
  setSessionId("default"); // 切到占位 session，等用户发送时才创建
}, [setSessionId]);
```

### 6.3 删除当前 Session 后

删除当前 Session 后，切回 `"default"` 占位状态：

```ts
await apiDeleteSession(id);
setSessions((prev) => prev.filter((s) => s.id !== id));
if (sessionIdRef.current === id) {
  setSessionId("default");
}
```

---

## 7. 完整流程图

```
用户点击「新对话」
    │
    ▼
router.push("/")          // 回到首页
setSessionId("default")   // 切换到占位 session
    │
    ▼
用户看到空对话
    │
    ▼
用户输入并发送第一条消息
    │
    ▼
sessionId === "default" ?
    │
    ├── 是 ──▶ createSession() ──▶ 后端创建真实 Session
    │            setSessionId(realId)
    │
    └── 否 ──▶ 使用当前真实 sessionId
                  │
                  ▼
        写入本地消息缓存
                  │
                  ▼
        streamChat(text, realSessionId)
                  │
                  ▼
        后端返回 title 事件，前端更新会话标题
```

---

## 8. 迁移到其它项目的要点

1. **定义占位 ID**：选一个不会与真实 ID 冲突的值，如 `"default"` 或 `"new"`。
2. **分离「切换视图」和「创建 Session」**：点击新对话只做 UI 状态切换。
3. **在发送消息入口处统一懒创建**：所有发送路径（用户输入、技能触发等）都走同一套 `ensureSession` 逻辑。
4. **创建 Session 后立即切换到新 ID**：避免后续 SSE 仍然使用占位 ID。
5. **预置空消息缓存**：防止切到新 Session 时把已经追加到本地的用户消息覆盖掉。
6. **后端接口**：至少提供 `POST /sessions` 创建接口；可选 `GET /sessions` 列表、`GET /sessions/:id/history` 历史。

---

## 9. 参考代码位置

本项目实现可参见：

- `frontend/src/components/layout/Sidebar.tsx` ——「新对话」按钮点击逻辑
- `frontend/src/lib/store.tsx` —— `setSessionId("default")`、`createSession`、`ensureSession`、`sendMessage` 中的懒创建逻辑
- `frontend/src/lib/api.ts` —— `createSession` API 封装
