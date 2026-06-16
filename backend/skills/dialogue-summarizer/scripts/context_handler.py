#!/usr/bin/env python3
"""
上下文处理器
处理对话上下文的获取、分段和优化
"""

from typing import List, Dict, Any, Optional
import re


class ContextHandler:
    """上下文处理器类"""
    
    def __init__(self, max_turns: int = 20, segment_size: int = 5):
        self.max_turns = max_turns
        self.segment_size = segment_size
    
    def get_recent_context(self, full_context: List[Dict], max_turns: Optional[int] = None) -> List[Dict]:
        """获取最近的对话上下文"""
        if not max_turns:
            max_turns = self.max_turns
        
        if len(full_context) <= max_turns:
            return full_context
        else:
            return full_context[-max_turns:]
    
    def segment_dialogue(self, dialogue: List[Dict]) -> List[List[Dict]]:
        """将长对话分段"""
        if len(dialogue) <= self.segment_size:
            return [dialogue]
        
        segments = []
        for i in range(0, len(dialogue), self.segment_size):
            segment = dialogue[i:i + self.segment_size]
            segments.append(segment)
        
        return segments
    
    def extract_topics(self, dialogue: List[Dict]) -> List[str]:
        """提取对话主题"""
        topics = []
        
        # 主题关键词
        topic_keywords = {
            '技能': ['技能', '功能', '工具', '创建', '开发'],
            '天气': ['天气', '温度', '气候', '预报', '气象'],
            '项目': ['项目', '任务', '计划', '规划', '方案'],
            '代码': ['代码', '编程', '程序', '开发', '实现'],
            '数据': ['数据', '分析', '统计', '处理', '数据库'],
            '设计': ['设计', '界面', 'UI', 'UX', '用户体验'],
            '测试': ['测试', '验证', '检查', '评估', '质量']
        }
        
        topic_counts = {topic: 0 for topic in topic_keywords.keys()}
        
        for turn in dialogue:
            text = turn.get('content', '')
            for topic, keywords in topic_keywords.items():
                if any(keyword in text for keyword in keywords):
                    topic_counts[topic] += 1
        
        # 选择出现次数最多的主题
        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
        for topic, count in sorted_topics[:3]:
            if count > 0:
                topics.append(topic)
        
        return topics if topics else ['综合讨论']
    
    def calculate_dialogue_stats(self, dialogue: List[Dict]) -> Dict[str, Any]:
        """计算对话统计信息"""
        user_turns = 0
        ai_turns = 0
        total_words = 0
        questions = 0
        
        question_patterns = [r'吗\?', r'呢\?', r'什么\?', r'如何\?', r'怎样\?', r'为什么\?', r'\?']
        
        for turn in dialogue:
            speaker = turn.get('speaker', '')
            content = turn.get('content', '')
            
            if 'user' in speaker.lower() or '用户' in speaker:
                user_turns += 1
            else:
                ai_turns += 1
            
            total_words += len(content)
            
            # 统计问题数量
            for pattern in question_patterns:
                if re.search(pattern, content):
                    questions += 1
                    break
        
        return {
            'total_turns': len(dialogue),
            'user_turns': user_turns,
            'ai_turns': ai_turns,
            'total_words': total_words,
            'avg_words_per_turn': total_words / len(dialogue) if dialogue else 0,
            'questions': questions,
            'question_rate': questions / len(dialogue) if dialogue else 0
        }
    
    def identify_action_items(self, dialogue: List[Dict]) -> List[Dict]:
        """识别行动项"""
        action_items = []
        
        # 行动项模式
        action_patterns = [
            (r'需要(.+?)(?:。|！|!|；|;|$)', 'need'),
            (r'应该(.+?)(?:。|！|!|；|;|$)', 'should'),
            (r'将要(.+?)(?:。|！|!|；|;|$)', 'will'),
            (r'计划(.+?)(?:。|！|!|；|;|$)', 'plan'),
            (r'安排(.+?)(?:。|！|!|；|;|$)', 'arrange'),
            (r'负责(.+?)(?:。|！|!|；|;|$)', 'responsible'),
            (r'完成(.+?)(?:。|！|!|；|;|$)', 'complete'),
            (r'跟进(.+?)(?:。|！|!|；|;|$)', 'followup')
        ]
        
        for turn in dialogue:
            text = turn.get('content', '')
            speaker = turn.get('speaker', 'unknown')
            
            for pattern, action_type in action_patterns:
                matches = re.findall(pattern, text)
                for match in matches:
                    if isinstance(match, tuple):
                        match_text = match[0]
                    else:
                        match_text = match
                    
                    if len(match_text.strip()) > 2:  # 过滤太短的匹配
                        action_items.append({
                            'action': match_text.strip(),
                            'type': action_type,
                            'speaker': speaker,
                            'original_text': text[:100] + '...' if len(text) > 100 else text
                        })
        
        return action_items
    
    def detect_decisions(self, dialogue: List[Dict]) -> List[Dict]:
        """检测决策点"""
        decisions = []
        
        decision_keywords = ['决定', '同意', '选择', '确定', '确认', '达成', '共识', '结论']
        decision_patterns = [
            r'我们(.+?)决定',
            r'我(.+?)同意',
            r'选择(.+?)方案',
            r'确定(.+?)为',
            r'达成(.+?)共识'
        ]
        
        for turn in dialogue:
            text = turn.get('content', '')
            speaker = turn.get('speaker', 'unknown')
            
            # 关键词匹配
            for keyword in decision_keywords:
                if keyword in text:
                    # 提取决策上下文
                    start = max(0, text.find(keyword) - 50)
                    end = min(len(text), text.find(keyword) + 100)
                    context = text[start:end]
                    
                    decisions.append({
                        'decision': keyword,
                        'context': context,
                        'speaker': speaker,
                        'position': dialogue.index(turn)
                    })
                    break
        
        return decisions
    
    def generate_context_fallback_prompt(self, missing_info: str = "") -> str:
        """生成上下文回退提示"""
        base_prompt = """## 对话总结请求

我注意到对话上下文不完整，无法提供全面的总结。请提供以下信息：

1. **对话主题**: 这次讨论主要是关于什么？
2. **关键讨论点**: 有哪些重要的讨论内容？
3. **已达成共识**: 我们达成了哪些共识？
4. **待办事项**: 有哪些需要跟进的事项？

或者，您可以简要描述需要总结的对话内容，我将基于您的描述生成总结。"""
        
        if missing_info:
            return f"{base_prompt}\n\n**已提供信息**: {missing_info}\n\n请补充其他信息。"
        
        return base_prompt


def main():
    """测试函数"""
    handler = ContextHandler()
    
    # 测试数据
    test_dialogue = [
        {'speaker': 'user', 'content': '我想创建一个对话总结技能，需要能够提取关键信息'},
        {'speaker': 'ai', 'content': '好的，我们可以设计一个智能总结对话的技能。您需要什么功能？'},
        {'speaker': 'user', 'content': '需要支持多种总结模式，比如快速总结和详细复盘'},
        {'speaker': 'ai', 'content': '我同意，这将很有用。我们应该设计三种模式：快速、标准、详细'},
        {'speaker': 'user', 'content': '好的，请开始创建吧。我需要尽快完成这个技能'},
        {'speaker': 'ai', 'content': '没问题，我将负责创建核心脚本，您需要测试功能吗？'}
    ]
    
    print("测试主题提取:")
    topics = handler.extract_topics(test_dialogue)
    print(f"主题: {topics}")
    
    print("\n测试统计信息:")
    stats = handler.calculate_dialogue_stats(test_dialogue)
    for key, value in stats.items():
        print(f"{key}: {value}")
    
    print("\n测试行动项识别:")
    actions = handler.identify_action_items(test_dialogue)
    for action in actions:
        print(f"- {action['action']} ({action['type']})")
    
    print("\n测试决策检测:")
    decisions = handler.detect_decisions(test_dialogue)
    for decision in decisions:
        print(f"- {decision['decision']}: {decision['context'][:50]}...")
    
    print("\n测试上下文回退:")
    print(handler.generate_context_fallback_prompt())


if __name__ == "__main__":
    main()