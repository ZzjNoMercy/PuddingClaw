# Request Templates

当用户想创建新 skill，但不想自己单独维护模板文件时，直接复用这里的对话模板。

## 通用创建模板
```md
帮我创建一个新的 skill，并在完成后评估它是否真的有效。

请按这条链执行：
1. 如有必要，先用 `skill-patrol` 搜索/学习同类开源 skill
2. 用 `skill-creator-pro` 设计并创建这个 skill
3. 创建完成后，用 `skill-benchmark` 做效果评估
4. 如果需要，再用 `skill-governance` 做归档和治理收尾

我的需求如下：
- Skill 想解决的问题：
  [这里写重复任务]
- 典型触发场景：
  [至少 3 条]
- 明确不该触发的场景：
  [至少 2 条]
- 期望输出：
  [结果形式]
- 是否需要脚本支持：
  [是 / 否 / 由你判断]
- 是否需要 references：
  [是 / 否 / 由你判断]
- 是否要参考外部开源 skill：
  [是 / 否]
- benchmark 要求：
  - 先做 `quick-check`
  - 再做 `benchmark-run`
  - 做 `baseline-vs-with-skill` 对比
  - 最后给我 verdict：
    `effective / partially effective / not proven / ineffective`
- 额外约束：
  [例如：主 `SKILL.md` 保持短；必须能 resume；不能依赖某个外部 API]
```

## 极简模板
```md
用 `skill-creator-pro` 帮我创建一个新 skill，解决这个问题：
[问题]

触发场景：
[场景]

不该触发：
[场景]

输出形式：
[输出]

做完后用 `skill-benchmark` 跑 `baseline-vs-with-skill`，告诉我它是否真的有效。
```

## 产品型 skill 模板
```md
帮我创建一个偏产品/业务工作流的 skill。

它要解决的问题：
[业务问题]

典型触发：
[例如：用户要写 PRD、做市场调研、做内容策划]

不该触发：
[例如：纯技术实现、代码调试]

输出要求：
[PRD / Markdown / 方案文档 / 汇总报告]

完成后请用 `skill-benchmark` 评估它在典型产品场景里是否有效。
```

## 开发型 skill 模板
```md
帮我创建一个偏开发/工程执行的 skill。

它要解决的问题：
[工程重复任务]

典型触发：
[例如：重构、调试、代码生成、脚本化操作]

不该触发：
[例如：纯 brainstorming、产品讨论]

输出要求：
[代码修改 / 脚本 / 配置 / 检查报告]

如果合适，可以先参考开源 skill，再创建本地 skill，最后用 `skill-benchmark` 做 baseline-vs-with-skill 对比。
```

## 内容工作流 skill 模板
```md
帮我创建一个偏内容生产/内容运营的 skill。

它要解决的问题：
[内容类重复任务]

典型触发：
[例如：写直播宣传语、发公众号、生成课程说明、做内容规划]

不该触发：
[例如：纯代码问题、通用闲聊]

输出要求：
[固定格式文案 / Markdown / 发布稿]

做完后请用 `skill-benchmark` 验证它是否在内容场景里稳定有效。
```

## 信息不全时的处理
如果用户只说“帮我创建一个新 skill”而没有给足信息，优先让用户按上面的模板补齐输入，而不是直接猜测边界。
