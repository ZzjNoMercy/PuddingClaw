# UI：Segment 渲染顺序约定

## 背景

Agent 模式下，一次 assistant turn 可能包含多次模型调用（model invocation）。每次调用产生的 reasoning、content 和 tool calls 被组织成一个 **segment**。

前端会把多 segment 的消息渲染成多个块，每个块包含：
- 该 segment 的文本内容 (`segment.content`)
- 该 segment 的思考链 (`segment.timeline` / `segment.reasoning`)

## 约定

**文本内容必须显示在工具链之前。**

即每个 segment 的渲染顺序为：

1. `segment.content`（模型对外说的话，意图或总结）
2. `segment.timeline` / `segment.reasoning`（背后的思考与工具执行）

## 实现位置

`frontend/src/components/chat/ChatMessage.tsx` 中的 `SegmentBlock` 组件：

```tsx
return (
  <div className="space-y-2">
    {/* 1. 先渲染文本 */}
    {segment.content ? (
      <div className="px-1 py-1 text-[15px] leading-relaxed">
        <div className="markdown-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={citationComponents}>
            {rendered}
          </ReactMarkdown>
        </div>
      </div>
    ) : null}

    {/* 2. 再渲染工具链 / 思考 */}
    {segment.timeline && segment.timeline.length > 0 ? (
      <ThoughtChain timeline={segment.timeline} isStreaming={isStreaming && isLast} />
    ) : segment.reasoning ? (
      <ReasoningBlock
        content={segment.reasoning}
        defaultOpen={isStreaming && !segment.content}
        isStreaming={isStreaming && !segment.content}
      />
    ) : null}
  </div>
);
```

## 为什么

如果先显示工具链再显示文本，用户会先看到「使用了 8 个工具」，然后才看到模型说「之前有 4 个 tracker 文件写在了错误位置」。

这会让工具链和文本的因果关系倒置。先展示模型说的话，再展示为支撑这句话而执行的工具，阅读体验更自然。

## 迁移到其他项目

在其他项目中找到类似的 segment / assistant-message 渲染组件，把 `content` 的渲染块从 `timeline` / `tool chain` 之后移到之前即可。
