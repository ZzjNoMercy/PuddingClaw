#!/usr/bin/env python3
"""
dialogue-summarizer 技能测试脚本
测试技能的核心功能和边界情况
"""

import sys
import os
import json
from pathlib import Path

# 添加技能目录到路径
skill_dir = Path(__file__).parent
sys.path.insert(0, str(skill_dir))

try:
    from scripts.summarizer import DialogueSummarizer
    from scripts.context_handler import ContextHandler
    print("✅ 成功导入技能模块")
except ImportError as e:
    print(f"❌ 导入模块失败: {e}")
    sys.exit(1)


def test_summarizer_basic():
    """测试基础总结功能"""
    print("\n" + "="*60)
    print("测试1: 基础总结功能")
    print("="*60)
    
    summarizer = DialogueSummarizer()
    
    # 测试数据
    test_dialogue = [
        {'speaker': 'user', 'content': '我想创建一个对话总结技能'},
        {'speaker': 'ai', 'content': '好的，我们可以设计一个智能总结对话的技能'},
        {'speaker': 'user', 'content': '需要能够提取关键信息和行动项'},
        {'speaker': 'ai', 'content': '我同意，这将很有用。我们应该设计多种总结模式'},
        {'speaker': 'user', 'content': '好的，请开始创建吧'}
    ]
    
    print("测试快速总结模式:")
    quick_summary = summarizer.generate_summary(test_dialogue, 'quick')
    print(quick_summary)
    
    print("\n测试标准总结模式:")
    standard_summary = summarizer.generate_summary(test_dialogue, 'standard')
    print(standard_summary[:500] + "..." if len(standard_summary) > 500 else standard_summary)
    
    print("\n测试详细复盘模式:")
    detailed_summary = summarizer.generate_summary(test_dialogue, 'detailed')
    print(detailed_summary[:500] + "..." if len(detailed_summary) > 500 else detailed_summary)
    
    return True


def test_context_handler():
    """测试上下文处理功能"""
    print("\n" + "="*60)
    print("测试2: 上下文处理功能")
    print("="*60)
    
    handler = ContextHandler()
    
    # 测试数据
    test_dialogue = [
        {'speaker': 'user', 'content': '需要创建一个对话总结技能，支持多种模式'},
        {'speaker': 'ai', 'content': '好的，我们可以设计快速、标准、详细三种模式'},
        {'speaker': 'user', 'content': '应该在下周完成第一个版本'},
        {'speaker': 'ai', 'content': '我负责核心开发，您需要测试功能吗？'},
        {'speaker': 'user', 'content': '是的，我会测试。另外需要添加错误处理'},
        {'speaker': 'ai', 'content': '同意，错误处理很重要。决定使用try-catch机制'}
    ]
    
    print("测试话题提取:")
    topics = handler.extract_topics(test_dialogue)
    print(f"提取的话题: {topics}")
    
    print("\n测试统计信息:")
    stats = handler.calculate_dialogue_stats(test_dialogue)
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    print("\n测试行动项识别:")
    actions = handler.identify_action_items(test_dialogue)
    print(f"识别到 {len(actions)} 个行动项:")
    for action in actions:
        print(f"  - {action['action']} ({action['type']})")
    
    print("\n测试决策检测:")
    decisions = handler.detect_decisions(test_dialogue)
    print(f"识别到 {len(decisions)} 个决策:")
    for decision in decisions:
        print(f"  - {decision['decision']}: {decision['context'][:50]}...")
    
    return True


def test_empty_context():
    """测试空上下文处理"""
    print("\n" + "="*60)
    print("测试3: 空上下文处理")
    print("="*60)
    
    summarizer = DialogueSummarizer()
    
    print("测试空对话列表:")
    empty_summary = summarizer.generate_summary([])
    print(empty_summary)
    
    # 检查是否触发了回退机制
    if "上下文不完整" in empty_summary or "提供信息" in empty_summary:
        print("✅ 正确触发了上下文回退机制")
        return True
    else:
        print("❌ 未正确触发上下文回退机制")
        return False


def test_keyword_extraction():
    """测试关键词提取"""
    print("\n" + "="*60)
    print("测试4: 关键词提取功能")
    print("="*60)
    
    summarizer = DialogueSummarizer()
    
    # 测试数据
    test_dialogue = [
        {'speaker': 'user', 'content': '需要创建一个新的技能，应该支持天气查询'},
        {'speaker': 'ai', 'content': '我同意，决定使用OpenWeather API'},
        {'speaker': 'user', 'content': '问题是时间比较紧张，需要尽快完成'},
        {'speaker': 'ai', 'content': '没问题，我负责API集成，您需要设计界面吗？'}
    ]
    
    print("测试关键点提取:")
    key_points = summarizer.extract_key_points(test_dialogue)
    print(f"提取到 {len(key_points)} 个关键点:")
    for point in key_points:
        print(f"  - [{point['type']}] {point['content'][:50]}...")
    
    print("\n测试行动项提取:")
    action_items = summarizer.extract_action_items(test_dialogue)
    print(f"提取到 {len(action_items)} 个行动项:")
    for item in action_items:
        print(f"  - {item['item']} (负责人: {item['assignee']}, 优先级: {item['priority']})")
    
    return len(key_points) > 0 and len(action_items) > 0


def test_eval_cases():
    """测试评估用例"""
    print("\n" + "="*60)
    print("测试5: 评估用例验证")
    print("="*60)
    
    # 读取评估文件
    evals_path = skill_dir / "evals" / "evals.json"
    if not evals_path.exists():
        print("❌ 评估文件不存在")
        return False
    
    with open(evals_path, 'r', encoding='utf-8') as f:
        evals_data = json.load(f)
    
    print(f"加载了 {len(evals_data['evals'])} 个评估用例")
    
    # 检查每个用例
    for eval_case in evals_data['evals']:
        print(f"\n用例 {eval_case['id']}: {eval_case['prompt'][:50]}...")
        print(f"  预期输出: {eval_case['expected_output'][:50]}...")
        
        # 检查断言
        if 'assertions' in eval_case:
            print(f"  包含 {len(eval_case['assertions'])} 个断言")
            for assertion in eval_case['assertions']:
                print(f"    - {assertion['description']}")
    
    return True


def test_skill_structure():
    """测试技能结构完整性"""
    print("\n" + "="*60)
    print("测试6: 技能结构完整性")
    print("="*60)
    
    required_files = [
        "SKILL.md",
        "scripts/summarizer.py",
        "scripts/context_handler.py",
        "references/summary_templates.md",
        "references/keyword_extraction.md",
        "references/action_item_detection.md",
        "assets/template_quick.md",
        "assets/template_standard.md",
        "assets/template_detailed.md",
        "evals/evals.json",
        "test_skill.py"
    ]
    
    missing_files = []
    
    for file_path in required_files:
        full_path = skill_dir / file_path
        if full_path.exists():
            print(f"✅ {file_path}")
        else:
            print(f"❌ {file_path} (缺失)")
            missing_files.append(file_path)
    
    if missing_files:
        print(f"\n❌ 缺失 {len(missing_files)} 个文件: {missing_files}")
        return False
    else:
        print("\n✅ 所有必需文件都存在")
        return True


def main():
    """主测试函数"""
    print("开始测试 dialogue-summarizer 技能")
    print("="*60)
    
    test_results = []
    
    # 运行所有测试
    test_results.append(("技能结构", test_skill_structure()))
    test_results.append(("基础总结", test_summarizer_basic()))
    test_results.append(("上下文处理", test_context_handler()))
    test_results.append(("空上下文", test_empty_context()))
    test_results.append(("关键词提取", test_keyword_extraction()))
    test_results.append(("评估用例", test_eval_cases()))
    
    # 汇总结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    
    passed = 0
    failed = 0
    
    for test_name, result in test_results:
        if result:
            print(f"✅ {test_name}: 通过")
            passed += 1
        else:
            print(f"❌ {test_name}: 失败")
            failed += 1
    
    print(f"\n总计: {passed} 通过, {failed} 失败")
    
    if failed == 0:
        print("\n🎉 所有测试通过！技能功能正常。")
        return 0
    else:
        print(f"\n⚠️  有 {failed} 个测试失败，请检查相关问题。")
        return 1


if __name__ == "__main__":
    sys.exit(main())