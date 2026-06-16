#!/usr/bin/env python3
"""
对话总结器核心逻辑
提供多种总结模式和上下文处理功能
"""

import re
from datetime import datetime
from typing import List, Dict, Any, Optional


class DialogueSummarizer:
    """对话总结器类"""
    
    def __init__(self):
        self.summary_modes = {
            'quick': self._generate_quick_summary,
            'standard': self._generate_standard_summary,
            'detailed': self._generate_detailed_summary
        }
        
    def determine_summary_mode(self, dialogue_length: int, complexity: str = "medium") -> str:
        """根据对话长度和复杂度确定总结模式"""
        if dialogue_length <= 5:
            return 'quick'
        elif dialogue_length <= 15:
            return 'standard'
        else:
            return 'detailed'
    
    def extract_key_points(self, dialogue: List[Dict]) -> List[Dict]:
        """从对话中提取关键点"""
        key_points = []
        
        # 关键词模式
        decision_keywords = ['决定', '同意', '选择', '确定', '确认', '达成']
        action_keywords = ['需要', '应该', '将要', '计划', '安排', '跟进']
        problem_keywords = ['问题', '困难', '挑战', '障碍', '麻烦']
        solution_keywords = ['解决', '方案', '方法', '建议', '推荐']
        
        for i, turn in enumerate(dialogue):
            text = turn.get('content', '')
            speaker = turn.get('speaker', 'unknown')
            
            # 检查决策语句
            if any(keyword in text for keyword in decision_keywords):
                key_points.append({
                    'type': 'decision',
                    'content': text,
                    'speaker': speaker,
                    'position': i
                })
            
            # 检查行动项
            elif any(keyword in text for keyword in action_keywords):
                key_points.append({
                    'type': 'action',
                    'content': text,
                    'speaker': speaker,
                    'position': i
                })
            
            # 检查问题和解决方案
            elif any(keyword in text for keyword in problem_keywords):
                key_points.append({
                    'type': 'problem',
                    'content': text,
                    'speaker': speaker,
                    'position': i
                })
            elif any(keyword in text for keyword in solution_keywords):
                key_points.append({
                    'type': 'solution',
                    'content': text,
                    'speaker': speaker,
                    'position': i
                })
        
        return key_points
    
    def extract_action_items(self, dialogue: List[Dict]) -> List[Dict]:
        """提取行动项"""
        action_items = []
        # 更新正则表达式以匹配中文标点
        action_patterns = [
            r'需要([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'应该([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'将要([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'计划([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'安排([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'跟进([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'负责([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)',
            r'完成([^。！!；;，,]+?)(?:。|！|!|；|;|，|,|$)'
        ]
        
        for turn in dialogue:
            text = turn.get('content', '')
            speaker = turn.get('speaker', 'unknown')
            
            for pattern in action_patterns:
                matches = re.findall(pattern, text)
                for match in matches:
                    if isinstance(match, tuple):
                        match_text = match[0]
                    else:
                        match_text = match
                    
                    if len(match_text.strip()) > 1:  # 降低长度限制
                        action_items.append({
                            'item': match_text.strip(),
                            'assignee': self._determine_assignee(speaker, match_text),
                            'priority': self._determine_priority(text)
                        })
        
        return action_items
    
    def _determine_assignee(self, speaker: str, action_text: str) -> str:
        """确定负责人"""
        if '我' in action_text or '自己' in action_text:
            return '用户' if 'user' in speaker.lower() else speaker
        elif '你' in action_text or 'AI' in action_text or '助手' in action_text:
            return 'AI助手'
        else:
            return '待确定'
    
    def _determine_priority(self, text: str) -> str:
        """确定优先级"""
        if '紧急' in text or '尽快' in text or '立即' in text:
            return '高'
        elif '重要' in text or '关键' in text:
            return '中'
        else:
            return '低'
    
    def _generate_quick_summary(self, dialogue: List[Dict], key_points: List[Dict]) -> str:
        """生成快速总结"""
        summary = "## 快速对话总结\n\n"
        
        # 基本信息
        summary += "### 📋 基本信息\n"
        summary += f"- **对话时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        summary += f"- **对话轮数**: {len(dialogue)}\n"
        
        # 提取话题
        topics = self._extract_topics(dialogue)
        if topics:
            summary += f"- **主要话题**: {', '.join(topics[:3])}\n"
        
        # 关键点
        if key_points:
            summary += "\n### 🎯 关键要点\n"
            for i, point in enumerate(key_points[:3], 1):
                # 截取前30个字符，避免过长
                content = point['content']
                if len(content) > 30:
                    content = content[:27] + '...'
                summary += f"{i}. {content}\n"
        
        # 行动项
        action_items = self.extract_action_items(dialogue)
        if action_items:
            summary += "\n### 📝 行动项\n"
            for item in action_items[:3]:
                summary += f"- {item['item']} ({item['assignee']})\n"
        else:
            summary += "\n### 📝 行动项\n- 无\n"
        
        # 建议
        summary += "\n### 💡 简要建议\n"
        if action_items:
            summary += "请跟进上述行动项，确保按时完成。\n"
        else:
            summary += "对话已完成，无需进一步行动。\n"
        
        return summary
    
    def _extract_topics(self, dialogue: List[Dict]) -> List[str]:
        """提取对话话题"""
        topics = []
        topic_keywords = {
            '技能': ['技能', '功能', '工具'],
            '天气': ['天气', '温度', '气候'],
            '项目': ['项目', '任务', '计划'],
            '代码': ['代码', '编程', '程序'],
            '总结': ['总结', '复盘', '回顾']
        }
        
        for turn in dialogue:
            text = turn.get('content', '')
            for topic, keywords in topic_keywords.items():
                if any(keyword in text for keyword in keywords):
                    if topic not in topics:
                        topics.append(topic)
        
        return topics if topics else ['综合讨论']
    
    def _generate_standard_summary(self, dialogue: List[Dict], key_points: List[Dict]) -> str:
        """生成标准总结"""
        # 这里返回模板，实际由AI填充
        return """## 对话总结报告

### 📋 基本信息
- **对话时间**: [自动生成时间戳]
- **对话时长**: [估算的对话时间]
- **参与方**: [用户和AI助手]
- **主要话题**: [1-3个主要话题]
- **总结模式**: 标准

### 🎯 关键讨论点
[关键点内容]

### ✅ 已达成共识
[共识内容]

### 📝 行动项与待办事项
[行动项表格]

### 💡 复盘建议
[建议内容]

### 📊 数据统计
[统计数据]"""
    
    def _generate_detailed_summary(self, dialogue: List[Dict], key_points: List[Dict]) -> str:
        """生成详细复盘"""
        # 这里返回模板，实际由AI填充
        return """## 详细对话复盘报告

### 📋 基本信息
[详细信息]

### 🎯 分层总结
#### 1. 整体概述
[整体概述]

#### 2. 详细分析
[详细分析]

#### 3. 深度见解
[深度见解]

### 📊 多维分析
[多维分析]

### 🎯 战略建议
[战略建议]

### 📈 长期影响
[长期影响分析]"""
    
    def generate_summary(self, dialogue: List[Dict], mode: Optional[str] = None) -> str:
        """生成总结的主函数"""
        if not dialogue:
            return self._generate_context_fallback()
        
        # 确定模式
        if not mode:
            mode = self.determine_summary_mode(len(dialogue))
        
        # 提取关键点
        key_points = self.extract_key_points(dialogue)
        
        # 生成总结
        if mode in self.summary_modes:
            return self.summary_modes[mode](dialogue, key_points)
        else:
            return self._generate_standard_summary(dialogue, key_points)
    
    def _generate_context_fallback(self) -> str:
        """上下文回退模板"""
        return """## 对话总结请求

我注意到对话上下文不完整，无法提供全面的总结。请提供以下信息：

1. **对话主题**: 这次讨论主要是关于什么？
2. **关键讨论点**: 有哪些重要的讨论内容？
3. **已达成共识**: 我们达成了哪些共识？
4. **待办事项**: 有哪些需要跟进的事项？

或者，您可以简要描述需要总结的对话内容，我将基于您的描述生成总结。"""


def main():
    """测试函数"""
    summarizer = DialogueSummarizer()
    
    # 测试数据
    test_dialogue = [
        {'speaker': 'user', 'content': '我想创建一个对话总结技能'},
        {'speaker': 'ai', 'content': '好的，我们可以设计一个智能总结对话的技能'},
        {'speaker': 'user', 'content': '需要能够提取关键信息和行动项'},
        {'speaker': 'ai', 'content': '我同意，这将很有用。我们应该设计多种总结模式'},
        {'speaker': 'user', 'content': '好的，请开始创建吧'}
    ]
    
    print("测试快速总结:")
    print(summarizer.generate_summary(test_dialogue, 'quick'))
    print("\n" + "="*50 + "\n")
    
    print("测试标准总结:")
    print(summarizer.generate_summary(test_dialogue, 'standard'))
    print("\n" + "="*50 + "\n")
    
    print("测试空对话:")
    print(summarizer.generate_summary([]))


if __name__ == "__main__":
    main()