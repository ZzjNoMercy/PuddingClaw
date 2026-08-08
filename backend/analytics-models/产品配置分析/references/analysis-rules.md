# 产品配置分析通用规则

本文件只保存所有产品配置分析 Query 和报告模板共同使用的数据口径、字段映射与计算规则。章节、页面结构、Payload、图表 DOM id 和交付验收属于具体模板，应放在对应模板目录的 `TEMPLATE.md` 中。

## 数据资产与颗粒度

### 数据表职责

| 数据表 | 颗粒度 | 用途 |
| --- | --- | --- |
| `vehicle_model_base` | 一行一个款型 | 分母、时间/品牌/车系/能源/级别/轴距/电机总功率/价格/销售状态筛选 |
| `vehicle_params` | 一行一个款型配置项 | 判断具体配置、配置值、配置供应商或硬件型号 |

### 标准键

- 款型：`brand + serial_name + car_name`。
- 车系：`brand + serial_name`；跨源分析时使用 `dimension:vehicle_series` 的 `entity_key`。
- 默认车型范围：狭义乘用车，排除 `vehicle_level = '皮卡'`。
- 默认时间口径：使用 `launch_date / launch_year`，不得从款型名称推断年份。

### 配置查询路径

1. 用 `vehicle_model_base` 形成唯一款型分母。
2. 用完整款型键连接 `vehicle_params`。
3. 按目标配置的 `type_name + type_value` 规则生成搭载标记。
4. 先在款型颗粒度去重，再聚合到车系、品牌、价格带或年份。

## 物理字段映射

| 逻辑维度 | 首选字段 | 回退规则 |
| --- | --- | --- |
| 品牌 | `vehicle_model_base.brand` | `vehicle_params.brand` |
| 车系 | `vehicle_model_base.serial_name` | `vehicle_params.serial_name` |
| 款型 | `vehicle_model_base.car_name` | `vehicle_params.car_name` |
| 上市日期/年/月 | `launch_date / launch_year / launch_month` | EAV `type_name='上市时间'` 后解析 |
| 能源类型 | `energy_type` | EAV `type_name='能源类型'` |
| 车型级别 | `vehicle_level` | EAV `type_name='级别'` |
| 轴距/轴距段 | `wheelbase_mm` + `dimension:wheelbase` | EAV `type_name='轴距[mm]'` 后数值化 |
| 电机总功率/功率段 | `motor_power_kw` + `dimension:motor_power` | EAV `type_name='电动机总功率[kW]'` 后数值化；不得由前后电机功率相加 |
| 指导价/价格带 | `price / price_band` | EAV `type_name='厂商指导价'` 后数值化 |
| 销售状态 | `sale_status` | 无可靠值时标记未知 |
| 配置类别 | `vehicle_params.category` | 不可从 `type_name` 猜测 |
| 配置项 | `vehicle_params.type_name` | 必须使用数据库实际枚举 |
| 配置值 | `vehicle_params.type_value` | 空值、`-`、`无`、`未配备`、`不配备`默认视为未搭载 |

## 语义资产目录

### 已注册资产

- 度量：`measure:config_rate`、`measure:charging_c_rate`、`measure:launch_update_count`、`measure:launch_cycle`。
- 维度：`dimension:launch_time`、`dimension:price_band`、`dimension:wheelbase`、`dimension:motor_power`、`dimension:brand`、`dimension:energy_type`、`dimension:vehicle_level`、`dimension:vehicle_series`。
- 颗粒度：`grain:car_model`、`grain:series`。

已注册的维度和度量必须优先使用其语义资产。未注册的逻辑字段必须在查询结果中显式派生并记录规则，不得把逻辑字段名当成数据库物理列。

### 常用派生维度

| 维度 | 示例 | 实现要求 |
| --- | --- | --- |
| 款型 | 2025 款某配置版 | 使用 `grain:car_model` 与完整款型键 |
| 配置类别/配置项/配置值 | 智能驾驶 / 激光雷达 / 1 个 | 使用 EAV `category/type_name/type_value` |
| 更新类型 | 新增、改款、换代 | 由同车系上市序列派生并保留规则说明 |
| 轴距段 | 2800–2850mm | 使用 `dimension:wheelbase` |
| 电机功率段 | 150–200kW | 使用 `dimension:motor_power` |
| 排量段 | 1.5L、2.0L | 从发动机排量标准化 |
| 电压平台 | 400V、800V、900V | 从平台电压/电池电压配置标准化 |
| 智驾能力 | L2、L2+、高速 NOA、城市 NOA | 按配置识别规则派生，不只匹配名称 |
| 智驾硬件 | 芯片、激光雷达及供应商 | 从配置值标准化厂商与型号 |
| 座舱舒适配置 | HUD、冰箱、后排屏、按摩、零重力座椅 | 每项使用明确配置识别规则 |
| 指导价统计位置 | 最小值、Q1、中位数、Q3、最大值 | 从款型指导价分布派生 |

## 通用度量口径

| 度量 | 公式/口径 |
| --- | --- |
| `model_count` | PostgreSQL 使用 `COUNT(DISTINCT (brand, serial_name, car_name))`；三列必须放在行构造器中 |
| `series_count` | `COUNT(DISTINCT brand, serial_name)`；跨源时统计 `entity_key` |
| `new_model_count` | 统计期内首次上市的唯一款型数 |
| `renewal_count` | 按明确的新增/改款/换代规则统计更新事件数 |
| `renewal_cycle_days` | 同一分析对象相邻有效更新日期的天数；明确使用均值或中位数 |
| `equipped_model_count` | 分母款型中满足配置识别规则的唯一款型数 |
| `config_rate` | `equipped_model_count / eligible_model_count` |
| `series_coverage_rate` | 至少一个款型搭载的车系数 / 分母车系数 |
| `internal_share` | 某配置子类型款型数 / 已搭载该大类配置的款型数 |
| `market_share` | 某配置款型数 / 当前市场范围全部有效款型数 |
| `year_over_year_change` | 本期值 / 上期值 - 1；单位为百分比 |
| `percentage_point_change` | 本期比例 - 上期比例；单位为百分点 |
| `median_price`、`price_q1`、`price_q3` | 在去重款型价格上计算，不在 EAV 行上计算 |
| `average_screen_size` | 有效屏幕尺寸数值的平均值，同时披露有效样本数 |

## 查询与证据规则

- 先计算任务共用的分母数据集，再计算配置标记和聚合，避免重复定义口径。
- 每个比率必须保留分子、分母、颗粒度、筛选和数据截止日。
- 每个派生指标必须能追溯到查询结果；不得在图表配置或结论文案中临时计算业务指标。
- 查询成功但无数据时必须说明已验证的空数据原因，不得用示例值或估算值补齐。
- 车系覆盖率、款型配备率、内部占比和市场占比不得混用。
- 纯电与全新能源必须使用独立分母。
- 时间序列必须说明缺失值、异常值和不连续年份。
