# opendatalab/MinerU 更新追踪

> 拉取时间: 2026-06-28 UTC | 数据来源: Tavily Search + GitHub 公开页面 (API 限速回退)

---

## 仓库概览

- ⭐ Stars: **68,500** [^src_c7f11b895a072537]
- 🍴 Forks: **5,800** [^src_c7f11b895a072537]
- 👥 Contributors: 74 | 📦 Releases: 175 [^src_c7f11b895a072537]
- 💻 主要语言: Python 99.3%
- 📝 描述: 高保真文档解析引擎 — PDF/Word/PPT/图片 → Markdown/JSON

---

## 最新 Release

### v3.4 — Pipeline OCR 能力升级 [^src_2fe8c1529cdbf60b]
- 🆕 Hybrid 后端新增 `effort` 解析强度 (`medium` / `high`)
- 🧠 VLM 模型升级至 **MinerU2.5-Pro-2605-1.2B**
- 📈 Pipeline 后端 OCR 精度和效率提升
- 🔄 模型下载/缓存复用/配置写入流程优化
- 🐳 适配 vLLM 0.21.0

### v3.3 — Hybrid 后端效率提升
- 多平台 Hybrid 效率提升，默认 `medium` 适合日常，`high` 面向高精度

### v3.2.0 — UI & 稳定性
- 界面改进、依赖优化、VLM 模型升级、稳定性修复

### v3.1.0 — 开放 & 生产级
- 新 License，PPTX/XLSX 原生支持，`MinerU2.5-Pro-2604-1.2B`

---

## 生态系统

### MinerU-Ecosystem [^src_f3160355e68ee184]
- 🔌 **MCP Server** — Claude Desktop / Cursor / Windsurf 原生工具
- 🔗 **LangChain / RAGFlow / Dify / FastGPT** 原生集成
- 🌍 109 语言，VLM+OCR 双引擎

### MinerU-Popo [^src_1716480f2b96a5cc]
- OCR 后处理模型，精度 **90.6**，推理 **0.37s**
- 桥接页面 OCR → 文档语义结构

### OmniDocBench (CVPR 2025) [^src_1dc6ef283e6184e5]
- v1.7 (2026-04-30): 千帆 OCR 排行榜，技能维度评测
- 覆盖 Gemini 3、GPT5.2、Kimi 2.5、DeepSeek-OCR-2 等
