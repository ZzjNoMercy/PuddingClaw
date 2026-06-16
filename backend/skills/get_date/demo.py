#!/usr/bin/env python3
"""
获取日期时间技能演示脚本
展示优化后技能的功能和特性
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scripts.get_datetime import get_current_datetime, detect_intent, format_by_intent

def demo_basic_functionality():
    """演示基本功能"""
    print("=" * 60)
    print("获取日期时间技能演示")
    print("=" * 60)
    print()
    
    # 获取当前时间
    print("1. 获取当前系统时间:")
    datetime_info = get_current_datetime()
    
    if datetime_info["is_valid"]:
        print(f"   ✓ 时间获取成功")
        print(f"   - 完整格式: {datetime_info['full_datetime_zh']}")
        print(f"   - 日期: {datetime_info['date_zh']}")
        print(f"   - 时间: {datetime_info['time_24h']}")
        print(f"   - 星期: {datetime_info['weekday_zh']}")
        print(f"   - 月份: {datetime_info['month']}月")
        print(f"   - 年份: {datetime_info['year']}年")
    else:
        print(f"   ✗ 时间获取失败: {datetime_info.get('error', '未知错误')}")
    
    print()

def demo_intent_detection():
    """演示意图检测"""
    print("2. 意图检测演示:")
    print("-" * 40)
    
    test_queries = [
        "现在几点了？",
        "今天星期几？",
        "今天是几月几号？",
        "告诉我完整的日期时间",
        "现在是几月？",
        "今年是哪一年？",
        "时间",
        "日期",
        "完整信息",
    ]
    
    for query in test_queries:
        intent = detect_intent(query)
        print(f"   '{query}' → {intent}")
    
    print()

def demo_format_output():
    """演示输出格式化"""
    print("3. 输出格式化演示:")
    print("-" * 40)
    
    datetime_info = get_current_datetime()
    
    if datetime_info["is_valid"]:
        test_cases = [
            ("现在几点了？", "time"),
            ("今天星期几？", "weekday"),
            ("今天的日期是什么？", "date"),
            ("告诉我完整的日期时间", "full"),
            ("现在是几月？", "month"),
            ("今年是哪一年？", "year"),
        ]
        
        for query, intent in test_cases:
            detected_intent = detect_intent(query)
            output = format_by_intent(datetime_info, detected_intent)
            print(f"   用户: {query}")
            print(f"   AI: {output}")
            print()
    
    print()

def demo_error_handling():
    """演示错误处理"""
    print("4. 错误处理演示:")
    print("-" * 40)
    
    # 模拟错误情况
    error_datetime = {
        "is_valid": False,
        "error": "模拟时间获取失败",
        "full_datetime_zh": "无法获取当前时间"
    }
    
    output = format_by_intent(error_datetime, "time")
    print(f"   模拟错误情况:")
    print(f"   用户: 现在几点了？")
    print(f"   AI: {output}")
    
    print()

def demo_command_line():
    """演示命令行使用"""
    print("5. 命令行使用演示:")
    print("-" * 40)
    
    print("   可以通过命令行直接调用技能:")
    print("   $ python scripts/get_datetime.py \"现在几点了？\"")
    print("   $ python scripts/get_datetime.py \"今天星期几？\"")
    print("   $ python scripts/get_datetime.py \"今天的日期是什么？\"")
    
    print()

def summary():
    """技能特性总结"""
    print("6. 技能特性总结:")
    print("-" * 40)
    
    features = [
        "✅ 基于 Operator 范式设计",
        "✅ 脚本优先，可靠执行",
        "✅ 智能意图识别（6种查询类型）",
        "✅ 多格式输出支持",
        "✅ 完整的错误处理",
        "✅ 全面的测试覆盖",
        "✅ 符合 skill-creator-pro 最佳实践",
        "✅ 支持中英文查询",
    ]
    
    for feature in features:
        print(f"   {feature}")
    
    print()
    print("=" * 60)
    print("演示完成！技能已按照最佳实践优化。")
    print("=" * 60)

if __name__ == "__main__":
    demo_basic_functionality()
    demo_intent_detection()
    demo_format_output()
    demo_error_handling()
    demo_command_line()
    summary()