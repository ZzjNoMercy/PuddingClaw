# 当前全部精选同步

只在用户明确要求拿到当前全部精选，或维护持久化精选镜像时读取本文件。普通资讯问答不要使用 snapshot。

## 首次建立镜像

1. 选择字段模式：
   - 只维护 id、标题、站内链接和分类：`fields=minimal`。
   - 需要摘要或第三方原文链接：`fields=default`。
2. 请求一次 `/api/v1/selected/snapshot?fields=<模式>`。
3. 从同一个成功响应取得完整 `items` 与不透明 `cursor`。
4. 把 items 集合和 cursor 作为一个原子状态写入；不能只保存其中一半。

如果用户只想在对话里查看当前全部精选，可以取一次 snapshot，但先报告总数，再按用户指定数量分批展示。未指定数量时仍只先展示最重要的 3—8 条。

## 持续接收变化

1. 请求 `/api/v1/selected/changes?cursor=<原样回传>&limit=100`。
2. 完整应用本页每条 change：
   - `op=upsert`：按 id 新增或替换条目。
   - `op=remove`：按 id 删除条目。
3. 整页全部应用成功后，再原子保存响应中的新 cursor。
4. `hasMore=true` 时立即用新 cursor 继续排空积压；排空后再恢复正常轮询。
5. 健康轮询期间只调用 changes，不请求 snapshot、items 或 fingerprint。

## 恢复

changes 返回 `400` 或 `409` 且 Problem `code=snapshot_required` 时：

1. 停止重试旧 cursor。
2. 用原来的字段模式重新请求一次 snapshot。
3. 原子替换本地完整集合与 cursor。
4. 后续恢复 changes 轮询。

一次恢复仍失败时停止并报告；不要循环下载 snapshot。

## 不变量

- cursor 不透明且绑定字段模式、同步端点和服务端水位。
- 不解析、不递增、不修改、不跨端点复用 cursor。
- `publishedAt` 和 `discoveredAt` 都不能表示编辑与撤选，不能充当完整同步水位。
- 不使用重叠时间窗替代 changes；时间窗无法可靠表达 remove。
- 不把 `/api/v1/items?mode=all` 当成“当前全部精选”。它是最近公开池，语义不同。
- 持久任务保存完整 URL 的 ETag，并在下次发送 `If-None-Match`；`304` 时保持本地状态和 cursor。
- 正常轮询至少间隔 60 秒；`hasMore=true` 的积压分页除外。
