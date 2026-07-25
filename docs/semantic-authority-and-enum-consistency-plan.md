# 语义权威链与枚举一致性检查落地方案

> 状态：待审核
> 范围：语义资产机器可读化、SQL 枚举一致性检查、Generator 规则回声、分析模型验收绑定
> 依据：session-23b096f95245（柴油枚举事故）、session-7b019aad45e1、docs/reports 验收报告（Codex 生成）全链路 trace
> 回答的问题：guardrail 规则能不能不用人工写？——能，前提是规则的知识已在语义资产中声明；人工只写资产没覆盖的部分。

## 1. 事故链（本方案要解决什么）

`dimension:energy_type` 权威声明：传统能源 = 5 个汽油系取值（柴油"默认不归入"）。实际发生的：

1. **Generator 自由发挥**：`sql-gen-82fe3ee2afe7` 的 question 无任何枚举，generator 自行决定口径，产出偏大旧值；两次 validator 均"语义校验 passed"；
2. **Agent 显式覆盖**：`sql-gen-544acd209833` 的 question 自写 7 值枚举（汽油系 + 柴油系），命中维度明文禁止的"不得根据通用行业认知自行扩展"；generate 输出的提示语"外层 Agent 不得凭字段名或常识直接覆盖"零强制力；
3. **Codex 验收报告口径**：`ELSE → 传统能源`（柴油、天然气、氢燃料全落入），同样偏离资产——证明**口径知识散落在散文、历史 SQL、第三方报告里，各自演化**;
4. validator 标称"已重放当前语义资产与 Guardrail"，但只查安全/表范围/guardrail 正则，**从不校验字面量与资产映射的一致性**。

## 2. 第一性原理

1. **单一权威**：业务口径的唯一权威是语义资产，不是 prompt 提示、question 文本、历史 SQL 或外部报告。冲突时资产胜出，例外必须显式登记（用户确认的口径 override);
2. **规则是数据，引擎通用**：检查引擎不含业务知识；业务知识以机器可读形式声明在资产里。新增维度/映射只写资产，引擎零改动——这是"规则不用人工写"的成立条件；
3. **fail-closed 三件套**：任何新硬门必须同时具备可闭合路径、可执行反馈、有效熔断，不制造新的不可闭合缺口（code_validation 死路的教训）;
4. **LLM 只做判断边界**：集合成员判定走确定性引擎；LLM 只用于"这可能是业务口径变更"的确认。

## 3. 现状调研（源码级）

### 3.1 语义资产声明与消费

- 声明：`backend/semantic-assets/<type>/<name>/*.md`,YAML frontmatter + markdown 正文。`classifications`/`enum_universe`/`forbidden_patterns` **目前只存在于正文表格**（如 `dimensions/energy_type/dimension.md` 的"传统能源与新能源分类"和"禁止规则"两节），机器不读；
- 生成器消费：`service.py:53` `_resolve_request_semantic_assets` → `resolver.py:192` 读正文全文 → `resolver.py:415` `format_semantic_assets_for_prompt` 注入 generator 自己的 prompt。**资产到 generator 的链路完整**;
- Agent 消费：`semantic_assets.py` middleware 注入索引（id/name/desc/frontmatter JSON)——**已确认生成器不依赖此系统 prompt**;
- frontmatter 已有结构化先例：`aliases`、`tags`、`resolution`（含 bindings、列绑定 `vehicle_model_base.energy_type` / EAV `type_name`)。

### 3.2 SQL validator 链

`tools/database/sql_validate_tool.py:34-140`:

```
validate_readonly_sql(sql, allowed_tables)     # 只读 + 表范围(:60)
→ detect_guardrail_conflicts(...)              # 语义 guardrail(:72-78)
→ 冲突 action ∈ {rewrite, block} → 拒发 Receipt,返回结构化修复协议(:83-96)
   （协议已含：parent_generation_id 重新生成、Agent 不得改 SQL、
     同一语义缺口最多重生两次、仍败返回 semantic_profile_required)
→ 通过 → register_validation_receipt(semantic_guardrail_ids + evidence)(:121)
```

**repair 协议已存在且设计良好**——新检查接入后自动继承。

### 3.3 Guardrail 引擎

`analytics/nl2sql/guardrails.py`:

- 规则 = `sql-guardrails/rules/<id>/guardrail.md`(YAML frontmatter + 正文）;
- `DETECTORS`(:661-667）注册 5 种 detector，全部**正则/子串**:`forbid_sql_pattern`、`require_sql_contains`、`require_table_when_available`、`require_group_by`、`forbid_exists_distinct_pattern`;
- `scope_matches`(:529-554)：按 `semantic_assets`(issubset 匹配已解析资产）、表范围、intent 关键词过滤；
- **全仓无 sqlglot/sqlparse/pglast**（已 grep 确认）——SQL"理解"能力 = 正则，撑不起字面量提取（NL2SQL 产物全是多层 CTE);
- 现有规则实例（`voltage_platform_400v_physical_value/guardrail.md`)：人工把"物理枚举是 `400V平台`"写成 `forbid_sql_pattern` + 正则——**知识本应在语义资产里，规则文件只是搬运工**。

### 3.4 验收合同编译

`harness/rubric_compiler.py:80-150`:contract 由 task_type + packs 组装（core/web_research/analytics/artifact/code),analytics pack 只有通用的 metric_consistency + traceability——领域错误（ELSE 口径、外推矩阵、须线规则）通用验收不可见。

## 4. 方案

### P0：语义资产机器可读化（前提）

把规范性声明从正文提升到 frontmatter，**frontmatter 为唯一权威，正文映射表由其生成或与其一致性校验**（禁止双写漂移——Codex 报告口径就是这么长出来的）:

```yaml
# semantic-assets/dimensions/energy_type/dimension.md frontmatter 增加
classifications:
  传统能源: [汽油, 汽油+48V轻混系统, 油电混合, 汽油电驱, 汽油+24V轻混系统]
  新能源: [纯电, 插电混合, 增程式纯电动]
enum_universe: [纯电, 插电混合, 增程式纯电动, 油电混合, 汽油, 汽油电驱,
                汽油+48V轻混系统, 汽油+24V轻混系统, 汽油+天然气,
                柴油, 柴油+48V轻混系统, 氢燃料, 天然气]
forbidden_patterns:
  - id: no_like_energy_fuzzy
    pattern: "LIKE '%纯电%'"
    message: "模糊 LIKE 会误匹配增程式纯电动"
  - id: no_eav_fallback_when_base_available
    type: structural   # 有 vehicle_model_base 时禁止回退 vehicle_params 自关联
```

- 划分标准：**机器要拿它判定的进 frontmatter**（映射、合法取值、禁止模式、列绑定）;**解释性的留正文**（业务含义、示例、为什么排除柴油）;
- `resolution.bindings` 已有列绑定，检查器据此定位"受治理列"，无需硬编码。

### P1：`semantic_enum_consistency` detector（核心）

在 guardrails 引擎注册第 6 种 detector，**规则数据来自语义资产而非规则文件**:

```
输入:clean_sql + generation.result.semantic_assets(已解析资产,validate 链现成)
1. 收集本次解析到的资产中声明了 classifications/enum_universe/forbidden_patterns 的
2. 由资产 bindings 定位受治理列(如 vehicle_model_base.energy_type、EAV type_name='能源类型')
3. sqlglot 解析 SQL(新增依赖,仅用于字面量提取,不重写现有 5 个正则 detector):
   - 提取作用于受治理列的 IN/=/LIKE 字面量集合
4. 判定:
   - 字面量 ⊄ enum_universe → conflict(技术错误)
   - 出现 classifications 的业务大类且枚举 ≠ 声明映射 → conflict(口径覆盖)
   - 命中 forbidden_patterns → conflict(禁止模式)
5. 出路(复用 validate 现有 repair 协议):
   - 精确违规 → technical_reject,附结构化 diff(多了哪些值、少了哪些、命中哪条)
   - 超集/子集且疑似业务口径变更 → business HITL,用户确认后记为 run 级口径 override
   - 一致 → passed,guardrail_id 记入 Receipt
6. 逃逸:资产未覆盖的列不检查;资产未声明 classifications 只查 forbidden/universe
```

规则文件写法（无需人工写正则，一行声明）:

```yaml
# sql-guardrails/rules/semantic_enum_consistency/guardrail.md
type: semantic_enum_consistency
scope: {}          # 默认对所有含语义资产的生成生效
params: {}
action: { type: rewrite }
```

**"规则不用人工写"的准确边界**：知识已在资产中声明的（枚举、映射、禁止模式），检查自动成立，事故后**不再人工补正则**；资产未声明的全新操作知识（如某 EAV 字段的真实物理值，在它被写进资产之前）仍走人工 guardrail——但沉淀路径反转：**事故 → 把知识写进语义资产 → 检查自动生效**，而不是"事故 → 手写一条正则规则"。

### P2：question 渠道三层防线（堵 question 层覆盖）

柴油事故的另一半：agent 在 question 文本里自写枚举。信任模型的根本问题：**权威按域划分——物理实现归资产/数据库证据，业务口径归 question，而 question 是 agent 写的，generator 看不到用户原文**。资产"只有用户另行明确分类口径时才可纳入"的口子，让 agent 编造的枚举伪装成"用户明确口径"。因此 P2 不是单一校验，而是三层：

- **P2a 入口校验（question vs 用户原文）**：复用 `_trusted_user_scope_text` 基础设施（`_agent_added_physical_guidance` 已用它比对物理标识，但中文枚举字面量不在其模式内——柴油事故正是从这个盲区进来的）。扩展：question 中出现所选资产 `enum_universe`/`classifications` 声明的枚举字面量、而用户原文没有 → 视为 agent 私加口径，拦截。用户原文确实写明的 → 合法 override，放行（与资产"用户明确口径可纳入"一致)。
- **P2b Generator 规则回声（applied_rules，防 generator 漂移）**：针对 question 无枚举、generator 自由发挥（ELSE 归入、自扩映射）的场景。**第一性原理：不让 LLM 自报依据（不可信），由 detector 确定性派生**——detector 检查时本来就收集了"哪些资产的哪些声明被检查、字面量集是什么"，把这个清单作为 `applied_rules` 记入 generation trace 与 Receipt，供验收与审计对账。
- **P2c 分类下移检测（防规避校验）**：事故的第三轮形态——agent 被拦后改要原始枚举 breakdown,SQL 零字面量、零分类结构（guardrail 无从检查），分类挪到 agent 推理层（无校验区）完成。检测信号：question 点名 classifications 标签，但 SQL 既无该标签的 CASE 映射臂、治理列上也无该映射的过滤字面量 → 分类结构缺席 → conflict，要求把分类以 CASE 物化回 SQL 层（物化后 P1 自动生效）。原则：**分类映射必须发生在机器可校验的位置**。

注意：P2a/P2b/P2c 均不直接"校验自然语言的正误"，而是校验**渠道一致性**（question↔用户原文）与**结构存在性**（分类是否物化），判定仍是确定性的。

### P3：分析模型验收绑定（产物层）

`analytics-models/<model>/model.md` frontmatter 增加 `acceptance:` 块（详见此前讨论）：不变量类型（grain_independence/no_extrapolation/boxplot_whisker_rule 等）由通用引擎执行，模型只写 type + target 参数（每模型 3-8 条，事故驱动积累）;`RunRubricCompiler` 编译时并入基础 packs。与 P1/P2 互补：P1/P2 管"查询口径",P3 管"产物口径"——agent 脑内/JS 层的分类汇总错误（柴油事故第三轮的最终形态）只有这一层能拦。

## 5. 代码改动清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `semantic-assets/dimensions/energy_type/dimension.md` | frontmatter 增加 classifications/enum_universe/forbidden_patterns（试点维度）；正文映射表标记为"由 frontmatter 生成" |
| 2 | `analytics/semantic_assets/registry.py`(`_parse_frontmatter`,:440) | 新字段解析与校验（unknown classification 报错） |
| 3 | `analytics/nl2sql/guardrails.py` | `DETECTORS` 注册 `semantic_enum_consistency`;新 detector 实现（资产加载 → 列定位 → 字面量判定 → conflict + diff 消息） |
| 4 | `analytics/nl2sql/sql_enum_extract.py`（新） | sqlglot 字面量提取器（受治理列 → IN/=/LIKE 字面量集合）；唯一新增依赖 `sqlglot` |
| 5 | `analytics/nl2sql/service.py`(:868-887) | generate 输出增加 `applied_rules` 字段；resolver 透出资产 classifications |
| 6 | `tools/database/sql_validate_tool.py`(:72-96) | 无改动（新 detector 自动进入 repair 协议）；只需确认 conflict 消息格式兼容 |
| 7 | `sql-guardrails/rules/semantic_enum_consistency/guardrail.md`（新） | 一条声明式规则（P1 所示） |
| 8 | `tools/database/sql_generate_tool.py` | P2a:`_agent_added_enum_caliber`——question 中出现所选资产 enum_universe/classifications 字面量但用户原文没有 → 拦截（与物理标识检查合并报错） |
| 9 | `analytics/nl2sql/guardrails.py` | P2c:detector 内"分类下移检测"——question 点名 classifications 但 SQL 无 CASE 映射臂且无映射值过滤 → conflict;P2b:`collect_applied_semantic_rules` 确定性派生写入 `generation_trace["applied_rules"]` |
| 10 | `harness/analytics_invariants.py`（新） | P3:acceptance.invariants 引擎（`INVARIANT_TYPES` 注册表 + `evaluate_model_invariants`)；首个不变量 `classification_mapping_declaration`（最终答复把枚举值归入未声明分类 → 违规，否定表述豁免） |
| 11 | `harness/rubric_compiler.py` + `harness/deterministic_checks.py` | P3 接入：模型声明 invariants 时编译期追加 criterion `analytics_model_invariants`(deterministic);执行器注册分发，缺 model_id fail-closed |
| 12 | `analytics-models/产品配置分析/model.md` | P3:frontmatter 增加 acceptance.invariants(classification_mapping_declaration → dimension:energy_type) |

不变更：现有 5 个正则 detector、validate 主流程、resolver 的 generator 注入链。

## 6. 测试计划

- 柴油用例（真实事故回归）:question 枚举含柴油 + 资产映射 5 值 → conflict,diff 消息列出 `柴油, 柴油+48V轻混系统` 为多余值；
- generator 自由发挥用例：question 无枚举 + SQL 出现 ELSE 归入 → 按 classifications 判定；
- 合法超集 HITL 用例：枚举 = 映射 + `汽油+天然气` → business HITL 而非 technical_reject;
- CTE 嵌套用例：3 层 WITH 内嵌 IN 列表，sqlglot 提取完整（正则反例，证明引入 parser 的必要）;
- 逃逸用例：资产未声明 classifications 的维度 → 不拦截；
- 性能：sqlglot parse 单次 < 50ms,validate 总耗时不退化；
- 回归：`backend/tests` 全量 + guardrail 现有规则（400V 等）行为不变。

## 7. 决策点

1. **frontmatter 与正文的关系**：推荐 frontmatter 为唯一权威 + 正文由脚本生成/校验；还是暂时双写 + CI 一致性检查过渡？
2. **sqlglot 引入**：只用于字面量提取（本方案），还是借机把 5 个正则 detector 逐步迁移（不推荐本轮做）?
3. **业务 override 的持久度**：用户确认"含柴油"后，效力范围 = 本 run / 本 goal / 写回资产修订？本方案建议 run 级，落 goal 需另审；
4. ~~P3 批次~~（已落地，见下）。

## 7.1 落地状态（2026-07-25)

- P0 / P1:✅ 已落地（含 CASE 派生标签别名误报修复，见 8.5);
- P2a / P2b / P2c:✅ 已落地（改动清单 #8-#9);
- P3:✅ 最小脊柱已落地（改动清单 #10-#12)——首个不变量 `classification_mapping_declaration` 只覆盖"最终答复文本的归类声明";**数值层对账（用 generation 原始结果行回放分类聚合、与产物数值比对）是下一迭代**,当前产物数值错误仍只能靠 P1/P2 在 SQL 层拦截。

## 8. 附录：`semantic_enum_consistency` 规则文件的运行机制（落地实录）

为什么 `sql-guardrails/rules/semantic_enum_consistency/guardrail.md` 只有 34 行、一条正则都没有，却全局生效——它不承担知识，只做三件事：

### 8.1 加载链上它实际消费的字段

`detect_guardrail_conflicts`(`backend/analytics/nl2sql/guardrails.py:865-896`）对每条规则：

1. `enabled: true` → 进入判定循环；
2. `scope_matches`(`guardrails.py:531-556`）过滤 scope；
3. `type` → 在 `DETECTORS`(`guardrails.py:859`）查到 `_detect_semantic_enum_consistency`;
4. `type` 命中 `_SEMANTIC_TRACE_DETECTOR_TYPES` → 分发时额外传入 `semantic_trace` + `question`（其他 5 种 detector 只拿 sql + rule);
5. detector 产出 conflict → 按 `action` 走 validate 链已有的 repair 协议（拒发 Receipt、结构化 diff、Generator 重生成，协议见 `tools/database/sql_validate_tool.py`)。

### 8.2 scope 全空 = 全局，但有自门控

`scope_matches` 的三个过滤器（table_scope / semantic_assets / intent_any）对空值全部跳过、直接返回 True，所以空 scope 的规则**每次都执行**。不误伤的原因是 detector 内部自门控：

- 只检查本次解析到的资产中、由 `governed` 声明的受治理列（物理列 / EAV type_name);
- 资产没声明 `classifications` 的维度只查 `enum_universe` / `forbidden_patterns`;
- 资产未覆盖的列一律放行，返回 `None`（无 conflict)。

即：**规则文件决定"这个检查存在"，资产 frontmatter 决定"检查什么"**。

### 8.3 与旧规则（如 400V）的本质区别

旧规则把业务知识（"物理枚举是 `400V平台`"）硬编码成 `forbid_sql_pattern` 正则，知识死在规则文件里；新规则文件是"搬运工"，知识在 `semantic-assets/dimensions/<dim>/dimension.md` 的 frontmatter(`governed` / `enum_universe` / `classifications` / `forbidden_patterns`)。演进路径随之反转：**新增治理维度只改资产 frontmatter，不再需要新建规则文件**——除非要加新的 detector 类型。

### 8.4 维护注意

- `version` / `updated_at` 仅作元信息，引擎不消费，演进时靠人工同步；
- `params: {}` 当前不被该 detector 读取，判定参数全部来自资产；
- 修改 detector 判定逻辑（`guardrails.py:_detect_semantic_enum_consistency` 与 `sql_enum_extract.py`）后，跑 `backend/tests/test_semantic_enum_guardrail.py` + 全量回归；
- sqlglot 依赖已锁定：`pyproject.toml` 声明 `sqlglot>=30.0`,`uv.lock` 锁定 30.13.0。

### 8.5 别名归因规则（2026-07-25 误报修复）

提取器只把**裸列透传**别名（`SELECT energy_type AS et`）归因到治理列；CASE / 计算表达式的别名（`CASE ... END AS energy_group`）输出的是派生值（分类标签），其上的谓词不归因。事故经过：question 被 agent 塞入柴油枚举 → 首版 SQL 被拦（正确）→ 重写版按资产口径去掉柴油（正确）→ 但重写版用 `FILTER (WHERE energy_group = '传统能源')` 按标签过滤，标签经别名映射泄漏进 `column:energy_type` 字面量集，正确 SQL 被误杀。修复见 `sql_enum_extract.py:_build_alias_map`，回归测试 `test_case_label_alias_predicates_are_not_attributed_to_governed_column`（含柴油变体仍必须被拦）。
