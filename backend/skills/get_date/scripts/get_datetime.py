#!/usr/bin/env python3
"""
获取日期时间核心脚本
用于可靠地获取当前系统时间并提供多种格式化选项
"""

import sys
from datetime import datetime

def get_current_datetime():
    """
    获取当前系统日期时间信息
    
    Returns:
        dict: 包含各种格式的日期时间信息
    Raises:
        Exception: 如果时间获取失败
    """
    try:
        now = datetime.now()
        
        # 中文星期映射
        weekday_zh = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
        
        # 构建返回信息
        info = {
            # 原始数据
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "hour": now.hour,
            "minute": now.minute,
            "second": now.second,
            "weekday_num": now.weekday(),  # 0=周一, 6=周日
            
            # 格式化字符串
            "weekday_en": now.strftime("%A"),
            "weekday_zh": weekday_zh,
            "date_ymd": now.strftime("%Y-%m-%d"),
            "date_zh": now.strftime("%Y年%m月%d日"),
            "time_24h": now.strftime("%H:%M:%S"),
            "time_12h": now.strftime("%I:%M:%S %p"),
            "full_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "full_datetime_zh": f"{now.strftime('%Y年%m月%d日')} {weekday_zh} {now.strftime('%H:%M:%S')}",
            
            # 验证信息
            "timestamp": now.timestamp(),
            "is_valid": True,
            "error": None
        }
        
        return info
        
    except Exception as e:
        # 返回错误信息
        return {
            "is_valid": False,
            "error": str(e),
            "timestamp": None,
            "full_datetime_zh": "无法获取当前时间"
        }

def format_by_intent(datetime_info, intent):
    """
    根据查询意图格式化输出
    
    Args:
        datetime_info: 日期时间信息字典
        intent: 查询意图字符串
    
    Returns:
        str: 格式化后的日期时间字符串
    """
    if not datetime_info.get("is_valid", False):
        return "无法获取当前时间，请检查系统时间设置"
    
    # 根据意图选择输出格式
    if intent == "time":
        return f"当前时间是 {datetime_info['time_24h']}"
    
    elif intent == "weekday":
        return f"今天是{datetime_info['weekday_zh']}"
    
    elif intent == "date":
        return f"今天是{datetime_info['date_zh']} {datetime_info['weekday_zh']}"
    
    elif intent == "full":
        return f"当前日期时间是：{datetime_info['full_datetime_zh']}"
    
    elif intent == "month":
        return f"现在是{datetime_info['month']}月"
    
    elif intent == "year":
        return f"今年是{datetime_info['year']}年"
    
    else:
        # 默认返回完整信息
        return f"当前日期时间：{datetime_info['full_datetime_zh']}"

def detect_intent(user_query):
    """
    检测用户查询意图
    
    Args:
        user_query: 用户查询字符串
    
    Returns:
        str: 意图分类
    """
    query_lower = user_query.lower()
    
    # 按优先级检查意图
    # 1. 完整信息（最高优先级）
    if any(keyword in query_lower for keyword in ["完整", "全部", "complete", "full"]):
        return "full"
    
    # 2. 日期查询
    if any(keyword in query_lower for keyword in ["日期", "几月几号", "几号", "date", "today"]):
        return "date"
    
    # 3. 时间查询
    if any(keyword in query_lower for keyword in ["几点了", "时间", "现在几点", "几点钟", "hour", "time"]):
        return "time"
    
    # 4. 星期查询
    if any(keyword in query_lower for keyword in ["星期几", "周几", "weekday", "day of week"]):
        return "weekday"
    
    # 5. 月份查询（需要排除包含"年"的情况）
    if "几月" in query_lower and "年" not in query_lower:
        return "month"
    
    # 6. 年份查询
    if any(keyword in query_lower for keyword in ["哪一年", "今年", "year"]):
        return "year"
    
    return "full"  # 默认返回完整信息

if __name__ == "__main__":
    # 测试模式
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
        intent = detect_intent(user_query)
        datetime_info = get_current_datetime()
        result = format_by_intent(datetime_info, intent)
        print(result)
    else:
        # 演示模式
        datetime_info = get_current_datetime()
        print("当前日期时间信息:")
        print(f"  完整格式: {datetime_info['full_datetime_zh']}")
        print(f"  日期: {datetime_info['date_zh']}")
        print(f"  时间: {datetime_info['time_24h']}")
        print(f"  星期: {datetime_info['weekday_zh']}")
        print(f"  月份: {datetime_info['month']}月")
        print(f"  年份: {datetime_info['year']}年")