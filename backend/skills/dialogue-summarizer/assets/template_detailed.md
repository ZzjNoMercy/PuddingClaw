# 详细复盘模板

## 适用场景
- 长对话（≥16轮）
- 复杂讨论和战略规划
- 项目复盘和评审会议
- 需要深度分析和多维度评估的场景

## 模板结构

```
## 详细对话复盘报告

### 📋 基本信息
- **对话时间**: {{start_time}} - {{end_time}}
- **对话时长**: {{exact_duration}}
- **参与方**: {{detailed_participants}}
- **对话类型**: {{conversation_type}}
- **总结模式**: 详细复盘
- **报告生成时间**: {{report_time}}

### 🎯 分层总结
#### 1. 整体概述
- **核心目标**: {{core_objective}}
- **主要成果**: {{key_achievements}}
- **关键决策**: {{critical_decisions}}
- **总体评价**: {{overall_assessment}}

#### 2. 详细分析
##### 2.1 话题分解
{{#each topics}}
- **{{this.name}}** (权重: {{this.weight}}%)
  - 讨论深度: {{this.depth}}
  - 关键观点: {{this.key_points}}
  - 未决问题: {{this.open_issues}}
{{/each}}

##### 2.2 讨论深度分析
- **深入讨论的话题**:
  {{#each deep_topics}}
  - {{this}}
  {{/each}}
  
- **浅层讨论的话题**:
  {{#each shallow_topics}}
  - {{this}}
  {{/each}}
  
- **未充分讨论的话题**:
  {{#each insufficient_topics}}
  - {{this}}
  {{/each}}

#### 3. 深度见解
- **模式识别**: {{pattern_recognition}}
- **潜在问题**: {{potential_issues}}
- **创新点**: {{innovation_points}}
- **认知偏差**: {{cognitive_biases}}

### 📊 多维分析
#### 1. 内容维度分析
- **技术性讨论**: {{technical_percentage}}%
  - 架构设计: {{architecture_points}}
  - 实现细节: {{implementation_details}}
  - 技术选型: {{technology_selection}}
  
- **战略性讨论**: {{strategic_percentage}}%
  - 目标设定: {{goal_setting}}
  - 路径规划: {{path_planning}}
  - 风险评估: {{risk_assessment}}
  
- **操作性讨论**: {{operational_percentage}}%
  - 任务分配: {{task_assignment}}
  - 时间安排: {{time_scheduling}}
  - 资源调配: {{resource_allocation}}

#### 2. 参与维度分析
- **参与度分布**:
  {{#each participation_distribution}}
  - {{this.participant}}: {{this.percentage}}% (发言{{this.turns}}次)
  {{/each}}
  
- **互动模式**:
  - 提问-回答: {{qa_count}}次
  - 建议-反馈: {{suggestion_feedback_count}}次
  - 辩论-共识: {{debate_consensus_count}}次
  
- **决策过程分析**:
  - 决策数量: {{decision_count}}
  - 平均决策时间: {{avg_decision_time}}
  - 决策质量评分: {{decision_quality_score}}/10

#### 3. 时间维度分析
- **话题演进轨迹**:
  {{#each topic_evolution}}
  - {{this.time_segment}}: {{this.main_topic}} → {{this.next_topic}}
  {{/each}}
  
- **讨论强度变化**:
  - 高峰期: {{peak_period}} ({{peak_intensity}})
  - 低谷期: {{low_period}} ({{low_intensity}})
  - 平均强度: {{avg_intensity}}
  
- **关键时间点**:
  {{#each critical_timestamps}}
  - {{this.time}}: {{this.event}}
  {{/each}}

### 🎯 战略建议
#### 1. 立即行动 (24小时内)
{{#each immediate_actions}}
- **{{this.action}}**
  - 负责人: {{this.assignee}}
  - 截止时间: {{this.deadline}}
  - 成功标准: {{this.success_criteria}}
{{/each}}

#### 2. 短期规划 (1-2周)
{{#each short_term_plans}}
- **{{this.plan}}**
  - 目标: {{this.objective}}
  - 关键步骤: {{this.key_steps}}
  - 预期成果: {{this.expected_outcomes}}
{{/each}}

#### 3. 长期考虑 (1-3个月)
{{#each long_term_considerations}}
- **{{this.consideration}}**
  - 战略意义: {{this.strategic_significance}}
  - 实施路径: {{this.implementation_path}}
  - 资源需求: {{this.resource_requirements}}
{{/each}}

### 📈 长期影响评估
#### 1. 对项目的影响
- **正面影响**:
  {{#each positive_project_impacts}}
  - {{this}}
  {{/each}}
  
- **潜在风险**:
  {{#each project_risks}}
  - {{this}}
  {{/each}}
  
- **依赖关系变化**:
  {{#each dependency_changes}}
  - {{this}}
  {{/each}}

#### 2. 对团队的影响
- **能力提升**:
  {{#each capability_improvements}}
  - {{this}}
  {{/each}}
  
- **流程优化**:
  {{#each process_optimizations}}
  - {{this}}
  {{/each}}
  
- **文化影响**:
  {{#each cultural_impacts}}
  - {{this}}
  {{/each}}

#### 3. 对业务的影响
- **效率提升**:
  {{#each efficiency_gains}}
  - {{this}}
  {{/each}}
  
- **成本影响**:
  {{#each cost_impacts}}
  - {{this}}
  {{/each}}
  
- **竞争优势**:
  {{#each competitive_advantages}}
  - {{this}}
  {{/each}}

### 🔍 综合风险评估
| 风险类别 | 具体风险 | 可能性 | 影响程度 | 综合风险 | 缓解措施 |
|----------|----------|--------|----------|----------|----------|
{{#each risks}}
| {{this.category}} | {{this.risk}} | {{this.likelihood}} | {{this.impact}} | {{this.composite_risk}} | {{this.mitigation}} |
{{/each}}

### 📝 附录
#### 1. 原始对话摘要
{{#each conversation_segments}}
**时间段**: {{this.time_range}}
```
{{this.content}}
```
{{/each}}

#### 2. 数据支持
- 对话统计数据: {{data_statistics}}
- 关键词频率: {{keyword_frequency}}
- 情感分析结果: {{sentiment_analysis}}

#### 3. 参考资料
{{#each references}}
- [{{this.title}}]({{this.url}})
{{/each}}

#### 4. 术语表
{{#each glossary}}
- **{{this.term}}**: {{this.definition}}
{{/each}}
```

## 字段说明

### 基本信息
- **exact_duration**: 精确时长（如：1小时28分钟）
- **detailed_participants**: 详细参与方信息
- **conversation_type**: 对话类型（战略规划、技术评审等）
- **report_time**: 报告生成时间

### 分层总结
- **core_objective**: 对话的核心目标
- **key_achievements**: 达成的主要成果
- **critical_decisions**: 做出的关键决策
- **overall_assessment**: 总体评价（优秀/良好/一般/需改进）

### 详细分析
- **topic weight**: 话题讨论权重（基于时间和重要性）
- **discussion depth**: 讨论深度（深入/中等/浅层）
- **pattern_recognition**: 发现的讨论模式
- **cognitive_biases**: 识别出的认知偏差

### 多维分析
- **technical_percentage**: 技术性讨论占比
- **participation_distribution**: 参与度分布
- **topic_evolution**: 话题演进轨迹
- **critical_timestamps**: 关键时间点

### 战略建议
- **immediate_actions**: 需要立即执行的动作
- **short_term_plans**: 短期规划
- **long_term_considerations**: 长期考虑事项

### 长期影响
- **positive_impacts**: 正面影响
- **potential_risks**: 潜在风险
- **dependency_changes**: 依赖关系变化

### 风险评估
- **likelihood**: 可能性（低/中/高）
- **impact**: 影响程度（低/中/高）
- **composite_risk**: 综合风险评分
- **mitigation**: 缓解措施

## 使用示例

### 示例场景
季度项目规划会议，时长1.5小时，涉及多个话题

### 关键部分示例
```
### 📊 多维分析
#### 1. 内容维度分析
- **技术性讨论**: 45%
  - 架构设计: 讨论了微服务架构和单体架构的优劣
  - 实现细节: 确定了API设计规范和数据库选型
  - 技术选型: 决定使用Python + FastAPI + PostgreSQL
  
- **战略性讨论**: 35%
  - 目标设定: 明确了Q2的3个核心业务目标
  - 路径规划: 制定了分阶段实施计划
  - 风险评估: 识别了技术债务和团队能力风险
  
- **操作性讨论**: 20%
  - 任务分配: 明确了各团队职责
  - 时间安排: 制定了详细的时间表
  - 资源调配: 确定了预算和人力需求
```

## 生成规则

### 1. 分析深度规则
1. **话题权重计算**: 基于讨论时间、参与度、重要性
2. **讨论深度评估**: 基于问题复杂度、解决方案完整性
3. **模式识别**: 分析讨论节奏、决策模式、互动特点

### 2. 多维度评估规则
1. **内容维度**: 技术性、战略性、操作性内容占比
2. **参与维度**: 各参与方贡献度、互动模式
3. **时间维度**: 话题演进、讨论强度变化

### 3. 战略建议生成规则
- **立即行动**: 紧急且重要的事项
- **短期规划**: 重要但不紧急的事项
- **长期考虑**: 战略性和前瞻性事项

### 4. 风险评估规则
1. **风险识别**: 技术风险、业务风险、团队风险
2. **风险评估**: 可能性和影响程度评估
3. **缓解措施**: 具体可行的应对方案

## 优化提示

### 分析深度
- 提供数据支持的分析结论
- 识别隐藏的模式和趋势
- 给出有洞察力的见解

### 结构组织
- 逻辑清晰，层次分明
- 各部分之间有机关联
- 便于读者理解和跟进

### 语言表达
- 使用专业但不晦涩的语言
- 保持客观中立的立场
- 提供具体可操作的描述

## 特殊情况处理

### 1. 高度技术性对话
```
### 🔬 技术深度分析
#### 1. 技术架构评估
- **当前架构**: {{current_architecture}}
- **建议改进**: {{suggested_improvements}}
- **迁移策略**: {{migration_strategy}}

#### 2. 性能考量
- **性能瓶颈**: {{performance_bottlenecks}}
- **优化建议**: {{optimization_suggestions}}
- **监控指标**: {{monitoring_metrics}}
```

### 2. 战略规划对话
```
### 🎯 战略框架
#### 1. 愿景与使命
- **长期愿景**: {{long_term_vision}}
- **中期目标**: {{mid_term_goals}}
- **短期重点**: {{short_term_focus}}

#### 2. 竞争优势分析
- **核心优势**: {{core_advantages}}
- **竞争态势**: {{competitive_landscape}}
- **差异化策略**: {{differentiation_strategy}}
```

### 3. 冲突解决对话
```
### ⚖️ 冲突分析与解决
#### 1. 冲突根源
- **表面冲突**: {{surface_conflict}}
- **深层原因**: {{root_causes}}
- **利益相关方**: {{stakeholders}}

#### 2. 解决方案
- **妥协方案**: {{compromise_solution}}
- **创新方案**: {{innovative_solution}}
- **实施计划**: {{implementation_plan}}
```

## 质量检查清单

### 分析质量
- [ ] 分析深度足够
- [ ] 多维度覆盖全面
- [ ] 见解有洞察力
- [ ] 数据支持充分

### 结构质量
- [ ] 逻辑结构清晰
- [ ] 各部分衔接自然
- [ ] 便于阅读和理解
- [ ] 格式规范统一

### 内容质量
- [ ] 信息准确完整
- [ ] 建议具体可行
- [ ] 风险评估合理
- [ ] 语言专业恰当

## 版本信息
- **版本**: v1.3
- **更新日期**: 2024-03-18
- **更新内容**: 完善多维度分析框架，优化战略建议结构，增强风险评估模块