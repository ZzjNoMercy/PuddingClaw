# 行动项识别规则参考文档

## 🎯 概述

本文档定义了对话中行动项的识别规则和提取方法，用于自动检测待办事项、任务分配和跟进事项。

## 📋 行动项定义

### 1. 行动项特征
一个完整的行动项应包含以下要素：

#### 核心要素
- **行动描述**: 要做什么（动词+宾语）
- **负责人**: 谁负责执行
- **时间要求**: 何时完成或开始
- **状态**: 当前进度状态

#### 可选要素
- **优先级**: 重要性和紧急程度
- **依赖关系**: 前置条件或依赖项
- **验收标准**: 完成的标准
- **资源需求**: 需要的资源支持

### 2. 行动项分类

#### 按类型分类
1. **创建类**: 创建、建立、编写、设计、开发
2. **修改类**: 修改、更新、优化、改进、调整
3. **检查类**: 检查、测试、验证、审核、评估
4. **沟通类**: 沟通、汇报、讨论、分享、反馈
5. **决策类**: 决定、选择、确定、确认、批准
6. **计划类**: 计划、安排、准备、组织、协调

#### 按优先级分类
- **P0 (紧急)**: 必须立即处理，影响核心功能
- **P1 (高)**: 重要任务，需要在短期内完成
- **P2 (中)**: 常规任务，按计划执行
- **P3 (低)**: 优化或改进任务，可延后处理

## 🔍 识别规则

### 1. 触发模式

#### 明确触发词
```
模式: (需要|应该|将要|计划|安排|负责|完成|跟进|处理|解决) + [行动描述]
示例: "需要创建一个测试用例" → 行动项: "创建一个测试用例"
```

#### 隐含触发词
```
模式: [行动描述] + (吧|啊|呢|吗|呀) + [时间要求]
示例: "我们测试一下功能吧，明天完成" → 行动项: "测试功能"
```

#### 责任分配模式
```
模式: [负责人] + (负责|处理|完成|跟进) + [行动描述]
示例: "我负责编写文档" → 行动项: "编写文档"，负责人: "我"
```

### 2. 时间识别规则

#### 明确时间
- **绝对时间**: "3月20日前"、"下周五"、"明天下午"
- **相对时间**: "三天内"、"一周后"、"尽快"、"立即"
- **周期时间**: "每天"、"每周"、"每月"、"每季度"

#### 时间模式
```
时间模式:
1. [行动] + [时间词]: "明天完成测试"
2. [时间词] + [行动]: "下周开始开发"
3. [行动] + [时间介词] + [时间]: "在月底前提交报告"
```

### 3. 负责人识别规则

#### 人称代词
- **第一人称**: 我、我们、本人、自己
- **第二人称**: 你、你们、您、AI、助手
- **第三人称**: 他、她、他们、某人、团队

#### 角色名称
- **用户角色**: 用户、客户、需求方
- **AI角色**: AI、助手、系统、机器人
- **团队角色**: 开发、测试、产品、设计

#### 识别算法
```python
def extract_assignee(text, action_phrase):
    """
    从文本中提取负责人
    """
    # 检查行动短语前的人称
    action_index = text.find(action_phrase)
    if action_index > 0:
        preceding_text = text[max(0, action_index-20):action_index]
        
        # 匹配人称模式
        patterns = [
            (r'(我|我们|本人)负责', 'user'),
            (r'(你|您|AI|助手)负责', 'ai'),
            (r'(他|她|他们|团队)负责', 'third_party')
        ]
        
        for pattern, assignee_type in patterns:
            if re.search(pattern, preceding_text):
                return assignee_type
    
    # 默认分配
    return '待确定'
```

## 🛠️ 提取算法

### 1. 基础提取流程

```python
def detect_action_items(text):
    """
    从单句文本中检测行动项
    """
    action_items = []
    
    # 行动项模式
    action_patterns = {
        'need': r'(需要|必须|应当|应该|得)\s*(.+?)(?:。|！|!|；|;|$|,)',
        'plan': r'(计划|安排|准备|打算|将要)\s*(.+?)(?:。|！|!|；|;|$|,)',
        'responsible': r'(负责|承担|主管|主导|牵头)\s*(.+?)(?:。|！|!|；|;|$|,)',
        'complete': r'(完成|实现|达成|做到|结束)\s*(.+?)(?:。|！|!|；|;|$|,)',
        'followup': r'(跟进|跟踪|监督|检查|确认)\s*(.+?)(?:。|！|!|；|;|$|,)',
        'implicit': r'(.+?)(?:吧|啊|呢|吗|呀)\s*(.+?)(?:。|！|!|；|;|$|,)'
    }
    
    for action_type, pattern in action_patterns.items():
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                action_text = match[1]
            else:
                action_text = match
            
            if len(action_text.strip()) > 2:  # 过滤太短的匹配
                action_item = {
                    'action': action_text.strip(),
                    'type': action_type,
                    'original_text': text
                }
                
                # 提取额外信息
                action_item.update(extract_additional_info(text, action_text))
                action_items.append(action_item)
    
    return action_items
```

### 2. 高级提取功能

#### 时间信息提取
```python
def extract_time_info(text, action_phrase):
    """
    提取与行动项相关的时间信息
    """
    time_info = {
        'deadline': None,
        'start_time': None,
        'duration': None,
        'priority': 'medium'
    }
    
    # 时间模式匹配
    time_patterns = [
        # 绝对时间
        (r'(\d{4}年\d{1,2}月\d{1,2}日)(?:前|之前|以前)', 'deadline'),
        (r'(\d{1,2}月\d{1,2}日)(?:前|之前|以前)', 'deadline'),
        (r'(\d{4}-\d{1,2}-\d{1,2})(?:前|之前|以前)', 'deadline'),
        
        # 相对时间
        (r'(今天|明天|后天)(?:完成|提交|结束)', 'deadline'),
        (r'(下周|下月|明年)(?:完成|提交|结束)', 'deadline'),
        (r'(\d+天|周|月|年)(?:内|之内|以内)', 'deadline'),
        
        # 紧急程度
        (r'(立即|马上|现在|即刻|尽快)', 'priority_high'),
        (r'(尽快|尽早|赶快)', 'priority_medium'),
        (r'(有空时|方便时|以后)', 'priority_low')
    ]
    
    for pattern, info_type in time_patterns:
        matches = re.findall(pattern, text)
        if matches:
            if info_type.startswith('priority_'):
                time_info['priority'] = info_type.split('_')[1]
            else:
                time_info[info_type] = matches[0]
    
    return time_info
```

#### 优先级评估
```python
def assess_priority(action_item, context):
    """
    评估行动项的优先级
    """
    priority_score = 0
    
    # 关键词权重
    priority_keywords = {
        '紧急': 3, '立即': 3, '马上': 3, '尽快': 2,
        '重要': 2, '关键': 2, '首要': 2, '优先': 2,
        '尽快': 1, '尽早': 1, '及时': 1
    }
    
    # 时间紧迫性
    if action_item.get('deadline'):
        if '今天' in action_item['deadline'] or '明天' in action_item['deadline']:
            priority_score += 2
        elif '本周' in action_item['deadline']:
            priority_score += 1
    
    # 关键词匹配
    for keyword, weight in priority_keywords.items():
        if keyword in action_item['original_text']:
            priority_score += weight
    
    # 上下文重要性
    if '问题' in context or '错误' in context or 'bug' in context:
        priority_score += 1
    
    # 确定优先级等级
    if priority_score >= 4:
        return 'high'
    elif priority_score >= 2:
        return 'medium'
    else:
        return 'low'
```

## 📊 结构化输出

### 1. 行动项表格格式

```markdown
| 事项 | 负责人 | 截止时间 | 状态 | 优先级 | 备注 |
|------|--------|----------|------|--------|------|
| [行动描述] | [负责人] | [时间] | [状态] | [优先级] | [备注] |
```

### 2. 状态定义
- **待处理**: 尚未开始
- **进行中**: 正在执行
- **已完成**: 已经完成
- **已取消**: 不再需要执行
- **阻塞中**: 因依赖项未完成而等待

### 3. 完整行动项对象

```python
action_item = {
    'id': 'unique_id',
    'action': '行动描述',
    'assignee': '负责人',
    'deadline': '截止时间',
    'status': '状态',
    'priority': '优先级',
    'dependencies': ['依赖项列表'],
    'notes': '备注信息',
    'created_at': '创建时间',
    'updated_at': '更新时间',
    'context': '原始上下文',
    'confidence': 0.95  # 识别置信度
}
```

## 🧪 测试用例

### 测试1：明确行动项
**输入**: "我需要创建一个测试脚本，明天完成"
**输出**:
```
行动项: 创建一个测试脚本
负责人: 用户
截止时间: 明天
状态: 待处理
优先级: 高
```

### 测试2：隐含行动项
**输入**: "我们测试一下新功能吧"
**输出**:
```
行动项: 测试新功能
负责人: 待确定
截止时间: 无
状态: 待处理
优先级: 中
```

### 测试3：责任分配
**输入**: "你负责编写文档，我负责测试"
**输出**:
```
行动项1: 编写文档
负责人: AI
状态: 待处理

行动项2: 测试
负责人: 用户
状态: 待处理
```

### 测试4：复杂行动项
**输入**: "需要在下周五前完成项目报告，这是关键任务，完成后请立即发给我"
**输出**:
```
行动项: 完成项目报告
负责人: 待确定
截止时间: 下周五前
状态: 待处理
优先级: 高
备注: 关键任务，完成后立即发送
```

## 🔧 优化建议

### 1. 上下文关联
- 将相关行动项分组
- 识别行动项之间的依赖关系
- 建立行动项链（前置任务→主任务→后续任务）

### 2. 智能补全
- 自动补充缺失的行动项要素
- 基于历史数据建议合理的时间安排
- 根据团队能力推荐合适的负责人

### 3. 跟踪更新
- 支持行动项状态更新
- 自动提醒即将到期的任务
- 生成行动项进度报告

### 4. 集成扩展
- 与项目管理工具集成
- 支持导出到日历或任务管理软件
- 提供API接口供其他系统调用

## 📈 性能指标

### 识别准确率
- **精确率**: 正确识别的行动项占所有识别行动项的比例
- **召回率**: 正确识别的行动项占所有真实行动项的比例
- **F1分数**: 精确率和召回率的调和平均

### 要素完整性
- **完整率**: 包含所有核心要素的行动项比例
- **准确率**: 各要素识别正确的比例
- **可用性**: 可直接用于任务管理的行动项比例

## 🔄 版本历史
- v1.0: 基础行动项识别规则
- v1.1: 添加时间识别和优先级评估
- v1.2: 优化提取算法，添加结构化输出
- v1.3: 添加测试用例和性能指标
- v1.4: 添加上下文关联和智能补全建议