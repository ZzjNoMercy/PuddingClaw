"""Migrate Vanna training records from the legacy model table name.

Run from the repository root:

    PYTHONPATH=backend backend/.venv/bin/python \
      backend/scripts/migrate_vanna_vehicle_model_base.py --apply

The migration adds the replacement DDL, documentation, and SQL examples first.
Previous DDL/documentation for vehicle_model_base and legacy table-name records
are deleted only after every replacement has been trained.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from analytics.nl2sql.training import (
    list_vanna_training_data,
    remove_vanna_training_data,
    train_vanna_ddl,
    train_vanna_documentation,
    train_vanna_sql,
)
from db import get_sessionmaker
from knowledge.database_sources import get_database_source

SOURCE_ID = "dbs_77982e981bac4a6fa8"
LEGACY_TABLE = "vehicle_params_wide"
MODEL_BASE_TABLE = "vehicle_model_base"

DOCUMENTATION = """数据库表：vehicle_model_base

vehicle_model_base 是 insight_data 中基于 vehicle_params 物化的款型基础表，一行代表一个款型，业务主键为 brand + serial_name + car_name。

表定位：该表只保存查询频繁的款型基础属性当前快照，不是把 vehicle_params 的全部配置参数横向展开，也不保存同一款型字段变化的历史版本。空气悬架、智驾、座舱等可扩展配置仍保存在 vehicle_params EAV 明细表中。

核心字段：
- brand：品牌，来自 vehicle_params.brand。
- serial_name：车系，来自 vehicle_params.serial_name。
- car_name：款型名称，保持 vehicle_params 的年款格式。
- car_name_full_year：对齐销售状态表的款型名称，例如 26款转换为2026款。
- launch_date / launch_year / launch_month：真实上市日期及派生年份、月份，来自 type_name='上市时间'。
- energy_type：能源类型，来自 type_name='能源类型'。
- vehicle_level：车型级别，来自 type_name='级别'。
- wheelbase_mm：款型轴距，单位毫米，来自 type_name='轴距[mm]'；无法解析时为 NULL。
- motor_power_kw：款型电动机总功率，单位 kW，来自 type_name='电动机总功率[kW]'；它是原始整车总功率，不得通过前后电机最大功率相加计算，无法解析时为 NULL。
- price：款型厂商指导价，单位万元，来自 type_name='厂商指导价'。
- price_band：基于 price 预计算的价格段。
- sale_status：先对 vehicle_serial_info.car_name 执行 btrim，再按 serial_name + car_name_full_year 左连接得到；同一规范化键同时出现“在售/停售”时，只要任一记录在售就记为在售，否则记为停售。
- sale_status_matched：是否成功匹配销售状态。
- sale_status_source：销售状态来源。
- refreshed_at：基础表刷新时间。

查询规则：
- 上市时间、能源类型、车型级别、轴距、电动机总功率、价格、价格段、品牌、车系和销售状态等基础筛选、分组与款型分母统计，优先查询 vehicle_model_base。
- 配置率、配备率和搭载率的分母优先来自 vehicle_model_base；分子通过 brand + serial_name + car_name 连接 vehicle_params 判断具体配置。
- 不要从 car_name 的年款文本推断上市年份，应使用 launch_year。
- 不要从 vehicle_params 反复 EAV 自关联计算已经物化到 vehicle_model_base 的基础字段。
- 同一车系可能包含多个轴距、电机功率或价格，按这些属性分析时必须保留 brand + serial_name + car_name 款型颗粒度。
- 默认排除 vehicle_level='皮卡'，除非用户明确要求包含或只看皮卡。
"""


async def migrate(*, apply: bool) -> dict[str, Any]:
    legacy = list_vanna_training_data(table_name=LEGACY_TABLE)
    records = list(legacy.get("records") or [])
    current = list_vanna_training_data(table_name=MODEL_BASE_TABLE)
    current_records = list(current.get("records") or [])
    summary: dict[str, Any] = {
        "apply": apply,
        "legacy_count": len(records),
        "legacy_counts": legacy.get("counts") or {},
        "current_count": len(current_records),
        "current_counts": current.get("counts") or {},
        "added_ids": [],
        "removed_ids": [],
    }
    if not apply:
        return summary

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        source = await get_database_source(session, SOURCE_ID)

    ddl_result = await train_vanna_ddl(source, table_names=[MODEL_BASE_TABLE])
    summary["added_ids"].extend(ddl_result.ids)

    documentation_result = await train_vanna_documentation(DOCUMENTATION)
    summary["added_ids"].extend(documentation_result.ids)

    for record in records:
        if record.get("training_type") != "sql":
            continue
        question = str(record.get("question") or "").strip()
        sql = str(record.get("content") or "").replace(LEGACY_TABLE, MODEL_BASE_TABLE)
        result = await train_vanna_sql(question, sql)
        summary["added_ids"].extend(result.ids)

    # Vanna training records are immutable. A logical update is implemented by
    # training the replacement first, then deleting older DDL/documentation.
    added_ids = set(summary["added_ids"])
    for record in current_records:
        if record.get("training_type") not in {"ddl", "documentation"}:
            continue
        record_id = str(record.get("id") or "").strip()
        if record_id and record_id not in added_ids and remove_vanna_training_data(record_id):
            summary["removed_ids"].append(record_id)

    for record in records:
        record_id = str(record.get("id") or "").strip()
        if record_id and remove_vanna_training_data(record_id):
            summary["removed_ids"].append(record_id)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Vanna model-base training records.")
    parser.add_argument("--apply", action="store_true", help="Write replacements and remove legacy records.")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(migrate(apply=args.apply)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
