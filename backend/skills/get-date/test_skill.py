#!/usr/bin/env python3
"""
获取日期时间技能测试脚本
测试技能的核心功能和边界情况
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scripts.get_datetime import get_current_datetime, detect_intent, format_by_intent

def test_datetime_retrieval():
    """测试时间获取功能"""
    print("测试 1: 时间获取功能")
    print("-" * 50)
    
    result = get_current_datetime()
    
    # 验证基本字段
    assert result["is_valid"] == True, "时间获取失败"
    assert "year" in result, "缺少年份字段"
    assert "month" in result, "缺少月份字段"
    assert "day" in result, "缺少日期字段"
    assert "hour" in result, "缺少小时字段"
    assert "minute" in result, "缺少分钟字段"
    assert "second" in result, "缺少秒数字段"
    
    # 验证格式字段
    assert "date_zh" in result, "缺少中文日期格式"
    assert "time_24h" in result, "缺少24小时制时间格式"
    assert "weekday_zh" in result, "缺少中文星期格式"
    assert "full_datetime_zh" in result, "缺少完整中文日期时间格式"
    
    print(f"✓ 时间获取成功")
    print(f"  当前时间: {result['full_datetime_zh']}")
    print(f"  时间戳: {result['timestamp']}")
    print()

def test_intent_detection():
    """测试意图检测功能"""
    print("测试 2: 意图检测功能")
    print("-" * 50)
    
    test_cases = [
        ("现在几点了？", "time"),
        ("今天星期几？", "weekday"),
        ("今天是几月几号？", "date"),
        ("告诉我完整的日期时间", "full"),
        ("现在是几月？", "month"),
        ("今年是哪一年？", "year"),
        ("时间", "time"),
        ("日期", "date"),
        ("完整信息", "full"),
    ]
    
    for query, expected_intent in test_cases:
        detected = detect_intent(query)
        assert detected == expected_intent, f"意图检测错误: '{query}' 期望 '{expected_intent}' 得到 '{detected}'"
        print(f"✓ '{query}' → {detected}")
    
    print()

def test_format_output():
    """测试输出格式化功能"""
    print("测试 3: 输出格式化功能")
    print("-" * 50)
    
    # 模拟时间信息
    mock_datetime = {
        "is_valid": True,
        "time_24h": "21:50:15",
        "weekday_zh": "星期四",
        "date_zh": "2026年03月19日",
        "full_datetime_zh": "2026年03月19日 星期四 21:50:15",
        "month": 3,
        "year": 2026
    }
    
    test_cases = [
        ("time", "当前时间是 21:50:15"),
        ("weekday", "今天是星期四"),
        ("date", "今天是2026年03月19日 星期四"),
        ("full", "当前日期时间是：2026年03月19日 星期四 21:50:15"),
        ("month", "现在是3月"),
        ("year", "今年是2026年"),
    ]
    
    for intent, expected_output in test_cases:
        output = format_by_intent(mock_datetime, intent)
        assert output == expected_output, f"格式化错误: '{intent}' 期望 '{expected_output}' 得到 '{output}'"
        print(f"✓ 意图: {intent}")
        print(f"  输出: {output}")
    
    print()

def test_error_handling():
    """测试错误处理功能"""
    print("测试 4: 错误处理功能")
    print("-" * 50)
    
    # 模拟错误情况
    error_datetime = {
        "is_valid": False,
        "error": "时间获取失败",
        "full_datetime_zh": "无法获取当前时间"
    }
    
    # 测试错误情况下的输出
    output = format_by_intent(error_datetime, "time")
    expected_error = "无法获取当前时间，请检查系统时间设置"
    assert output == expected_error, f"错误处理不正确: 期望 '{expected_error}' 得到 '{output}'"
    
    print(f"✓ 错误处理正常")
    print(f"  错误输出: {output}")
    print()

def test_integration():
    """测试集成功能"""
    print("测试 5: 集成测试")
    print("-" * 50)
    
    # 实际获取时间
    datetime_info = get_current_datetime()
    
    if datetime_info["is_valid"]:
        # 测试各种查询
        test_cases = [
            ("现在几点了？", "time", "当前时间是 {time_24h}"),
            ("今天星期几？", "weekday", "今天是{weekday_zh}"),
            ("今天的日期是什么？", "date", "今天是{date_zh} {weekday_zh}"),
            ("告诉我完整的日期时间", "full", "当前日期时间是：{full_datetime_zh}"),
            ("现在是几月？", "month", "现在是{month}月"),
            ("今年是哪一年？", "year", "今年是{year}年"),
        ]
        
        for query, expected_intent, expected_pattern in test_cases:
            intent = detect_intent(query)
            assert intent == expected_intent, f"意图检测错误: '{query}' 期望 '{expected_intent}' 得到 '{intent}'"
            
            output = format_by_intent(datetime_info, intent)
            
            # 验证输出包含关键信息
            if expected_intent == "time":
                assert datetime_info["time_24h"] in output, f"时间输出不包含时间信息: {output}"
            elif expected_intent == "weekday":
                assert datetime_info["weekday_zh"] in output, f"星期输出不包含星期信息: {output}"
            elif expected_intent == "date":
                assert datetime_info["date_zh"] in output, f"日期输出不包含日期信息: {output}"
                assert datetime_info["weekday_zh"] in output, f"日期输出不包含星期信息: {output}"
            elif expected_intent == "full":
                assert datetime_info["full_datetime_zh"] in output, f"完整输出不包含完整信息: {output}"
            elif expected_intent == "month":
                assert str(datetime_info["month"]) in output, f"月份输出不包含月份信息: {output}"
            elif expected_intent == "year":
                assert str(datetime_info["year"]) in output, f"年份输出不包含年份信息: {output}"
            
            print(f"✓ '{query}'")
            print(f"  意图: {intent}")
            print(f"  输出: {output}")
            print()
    else:
        print("⚠️ 时间获取失败，跳过集成测试")
        print(f"  错误: {datetime_info.get('error', '未知错误')}")

def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("获取日期时间技能测试套件")
    print("=" * 60)
    print()
    
    tests = [
        test_datetime_retrieval,
        test_intent_detection,
        test_format_output,
        test_error_handling,
        test_integration,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"✗ 测试失败: {e}")
            print()
        except Exception as e:
            failed += 1
            print(f"✗ 测试异常: {e}")
            print()
    
    print("=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    
    if failed == 0:
        print("✅ 所有测试通过！")
        return True
    else:
        print("❌ 有测试失败，请检查技能实现")
        return False

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)