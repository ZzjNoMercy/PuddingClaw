-- 产品配置分析_2026 图表数据验收 SQL
-- 数据源: insight_data (dbs_77982e981bac4a6fa8)
-- 方言: PostgreSQL 16
-- 默认范围: 2020-2026 上市的中国狭义乘用车，vehicle_level 排除“皮卡”
-- 款型键: brand + serial_name + car_name
-- 说明: 所有查询均为只读；2026 为截至数据源最大 launch_date 的非完整年度。

SET statement_timeout = '180s';

-- Q01 数据截止日与分母覆盖
SELECT
  MIN(launch_date) AS min_launch_date,
  MAX(launch_date) AS data_cutoff,
  COUNT(*) FILTER (WHERE launch_year BETWEEN 2020 AND 2026
                   AND vehicle_level IS DISTINCT FROM '皮卡') AS eligible_models,
  COUNT(*) FILTER (WHERE launch_year = 2026
                   AND vehicle_level IS DISTINCT FROM '皮卡') AS eligible_models_2026
FROM vehicle_model_base;

-- Q02 renewalChart：分能源组更新次数和平均上市周期。
-- 强制遵守 measure:launch_cycle：先在完整历史事件序列计算 LAG，再筛 2020-2026。
WITH model_flags AS (
  SELECT
    brand,
    serial_name,
    car_name,
    MAX(CASE WHEN type_name = '上市时间'
              AND type_value ~ '^\d{4}-\d{2}-\d{2}$'
             THEN type_value::date END) AS launch_date,
    MAX(type_value) FILTER (WHERE type_name = '能源类型') AS energy_type,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup
  FROM vehicle_params
  WHERE type_name IN ('上市时间', '能源类型', '级别')
  GROUP BY brand, serial_name, car_name
), events AS (
  SELECT DISTINCT
    brand,
    serial_name,
    CASE
      WHEN energy_type IN ('纯电', '插电混合', '增程式纯电动') THEN '新能源'
      ELSE '传统能源'
    END AS energy_group,
    launch_date
  FROM model_flags
  WHERE launch_date IS NOT NULL
    AND NOT is_pickup
    AND energy_type IS NOT NULL
), sequenced AS (
  SELECT
    *,
    LAG(launch_date) OVER (
      PARTITION BY brand, serial_name, energy_group
      ORDER BY launch_date
    ) AS previous_launch_date
  FROM events
), calc AS (
  SELECT
    EXTRACT(YEAR FROM launch_date)::int AS launch_year,
    energy_group,
    launch_date - previous_launch_date AS cycle_days
  FROM sequenced
)
SELECT
  launch_year,
  energy_group,
  COUNT(*) AS update_count,
  COUNT(cycle_days) AS valid_cycle_count,
  ROUND(AVG(cycle_days), 2) AS avg_cycle_days
FROM calc
WHERE launch_year BETWEEN 2020 AND 2026
GROUP BY launch_year, energy_group
ORDER BY launch_year, energy_group;

-- Q03 wheelbaseTrendChart：轴距段款型结构；未知值保留为单独分段。
WITH base AS (
  SELECT
    launch_year,
    brand,
    serial_name,
    car_name,
    CASE
      WHEN wheelbase_mm IS NULL THEN '未知'
      WHEN wheelbase_mm < 2600 THEN '2600以下'
      WHEN wheelbase_mm < 2650 THEN '2600-2650'
      WHEN wheelbase_mm < 2700 THEN '2650-2700'
      WHEN wheelbase_mm < 2750 THEN '2700-2750'
      WHEN wheelbase_mm < 2800 THEN '2750-2800'
      WHEN wheelbase_mm < 2850 THEN '2800-2850'
      WHEN wheelbase_mm < 2900 THEN '2850-2900'
      WHEN wheelbase_mm < 2950 THEN '2900-2950'
      WHEN wheelbase_mm < 3000 THEN '2950-3000'
      ELSE '3000以上'
    END AS wheelbase_band
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
)
SELECT
  launch_year,
  wheelbase_band,
  COUNT(*) AS model_count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY launch_year), 2) AS model_share_pct
FROM base
GROUP BY launch_year, wheelbase_band
ORDER BY launch_year, wheelbase_band;

-- Q04 motorPowerTrendChart：新能源且功率有效的款型结构；同时披露覆盖率。
WITH nev AS (
  SELECT *
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND energy_type IN ('纯电', '插电混合', '增程式纯电动')
), valid AS (
  SELECT
    *,
    CASE
      WHEN motor_power_kw < 50 THEN '0-50kW'
      WHEN motor_power_kw < 100 THEN '50-100kW'
      WHEN motor_power_kw < 150 THEN '100-150kW'
      WHEN motor_power_kw < 200 THEN '150-200kW'
      WHEN motor_power_kw < 250 THEN '200-250kW'
      WHEN motor_power_kw < 300 THEN '250-300kW'
      WHEN motor_power_kw < 350 THEN '300-350kW'
      WHEN motor_power_kw < 400 THEN '350-400kW'
      WHEN motor_power_kw < 450 THEN '400-450kW'
      WHEN motor_power_kw < 500 THEN '450-500kW'
      ELSE '500kW以上'
    END AS power_band
  FROM nev
  WHERE motor_power_kw IS NOT NULL AND motor_power_kw >= 0
)
SELECT
  v.launch_year,
  v.power_band,
  COUNT(*) AS model_count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY v.launch_year), 2) AS valid_sample_share_pct,
  ROUND(100.0 * (SELECT COUNT(*) FROM valid x WHERE x.launch_year = v.launch_year)
        / NULLIF((SELECT COUNT(*) FROM nev n WHERE n.launch_year = v.launch_year), 0), 2) AS field_coverage_pct
FROM valid v
GROUP BY v.launch_year, v.power_band
ORDER BY v.launch_year, v.power_band;

-- Q05 sizePowerHeatmapChart：轴距 x 电机功率唯一款型数。
WITH base AS (
  SELECT
    launch_year,
    CASE
      WHEN wheelbase_mm < 2600 THEN '2600以下'
      WHEN wheelbase_mm < 2650 THEN '2600-2650'
      WHEN wheelbase_mm < 2700 THEN '2650-2700'
      WHEN wheelbase_mm < 2750 THEN '2700-2750'
      WHEN wheelbase_mm < 2800 THEN '2750-2800'
      WHEN wheelbase_mm < 2850 THEN '2800-2850'
      WHEN wheelbase_mm < 2900 THEN '2850-2900'
      WHEN wheelbase_mm < 2950 THEN '2900-2950'
      WHEN wheelbase_mm < 3000 THEN '2950-3000'
      ELSE '3000以上'
    END AS wheelbase_band,
    CASE
      WHEN motor_power_kw < 50 THEN '0-50kW'
      WHEN motor_power_kw < 100 THEN '50-100kW'
      WHEN motor_power_kw < 150 THEN '100-150kW'
      WHEN motor_power_kw < 200 THEN '150-200kW'
      WHEN motor_power_kw < 250 THEN '200-250kW'
      WHEN motor_power_kw < 300 THEN '250-300kW'
      WHEN motor_power_kw < 350 THEN '300-350kW'
      ELSE '350kW以上'
    END AS power_band
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND energy_type IN ('纯电', '插电混合', '增程式纯电动')
    AND wheelbase_mm IS NOT NULL
    AND motor_power_kw IS NOT NULL
    AND motor_power_kw >= 0
)
SELECT launch_year, power_band, wheelbase_band, COUNT(*) AS model_count
FROM base
GROUP BY launch_year, power_band, wheelbase_band
ORDER BY launch_year, power_band, wheelbase_band;

-- Q06 bevVoltageTrendChart / nevVoltageTrendChart：
-- v400/v800/v900 为已披露高压平台内部结构，platform_coverage_pct 为全部适格款型中的披露率。
WITH base AS (
  SELECT
    CASE WHEN energy_type = '纯电' THEN 'BEV' ELSE 'NEV' END AS scope,
    launch_year,
    brand,
    serial_name,
    car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND energy_type IN ('纯电', '插电混合', '增程式纯电动')
), platform AS (
  SELECT DISTINCT
    brand,
    serial_name,
    car_name,
    CASE
      WHEN type_value = '400V平台' THEN '400V'
      WHEN type_value = '800V平台' THEN '800V'
      WHEN type_value IN ('900V平台', '897V平台') THEN '900V'
    END AS platform
  FROM vehicle_params
  WHERE type_name = '高压快充平台'
), scopes AS (
  SELECT * FROM base
  UNION ALL
  SELECT 'ALL_NEV', launch_year, brand, serial_name, car_name FROM base
)
SELECT
  s.scope,
  s.launch_year,
  COUNT(*) AS eligible_models,
  COUNT(p.platform) AS disclosed_models,
  ROUND(100.0 * COUNT(p.platform) / COUNT(*), 2) AS platform_coverage_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '400V') / NULLIF(COUNT(p.platform), 0), 2) AS v400_internal_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '800V') / NULLIF(COUNT(p.platform), 0), 2) AS v800_internal_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '900V') / NULLIF(COUNT(p.platform), 0), 2) AS v900_internal_pct
FROM scopes s
LEFT JOIN platform p USING (brand, serial_name, car_name)
WHERE s.scope IN ('BEV', 'ALL_NEV')
GROUP BY s.scope, s.launch_year
ORDER BY s.scope, s.launch_year;

-- Q07 bevVoltagePriceChart / nevVoltagePriceChart：按价格段重算平台内部结构和披露率。
WITH base0 AS (
  SELECT
    energy_type,
    brand,
    serial_name,
    car_name,
    CASE
      WHEN price >= 10 AND price < 15 THEN '10-15万'
      WHEN price >= 15 AND price < 20 THEN '15-20万'
      WHEN price >= 20 AND price < 30 THEN '20-30万'
      WHEN price >= 30 AND price < 50 THEN '30-50万'
      WHEN price >= 50 THEN '50万以上'
    END AS price_band
  FROM vehicle_model_base
  WHERE launch_year = 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND energy_type IN ('纯电', '插电混合', '增程式纯电动')
), platform AS (
  SELECT DISTINCT
    brand,
    serial_name,
    car_name,
    CASE
      WHEN type_value = '400V平台' THEN '400V'
      WHEN type_value = '800V平台' THEN '800V'
      WHEN type_value IN ('900V平台', '897V平台') THEN '900V'
    END AS platform
  FROM vehicle_params
  WHERE type_name = '高压快充平台'
), scopes AS (
  SELECT 'BEV' AS scope, * FROM base0 WHERE energy_type = '纯电'
  UNION ALL
  SELECT 'ALL_NEV' AS scope, * FROM base0
)
SELECT
  s.scope,
  s.price_band,
  COUNT(*) AS eligible_models,
  COUNT(p.platform) AS disclosed_models,
  ROUND(100.0 * COUNT(p.platform) / COUNT(*), 2) AS platform_coverage_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '400V') / NULLIF(COUNT(p.platform), 0), 2) AS v400_internal_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '800V') / NULLIF(COUNT(p.platform), 0), 2) AS v800_internal_pct,
  ROUND(100.0 * COUNT(*) FILTER (WHERE p.platform = '900V') / NULLIF(COUNT(p.platform), 0), 2) AS v900_internal_pct
FROM scopes s
LEFT JOIN platform p USING (brand, serial_name, car_name)
WHERE s.price_band IS NOT NULL
GROUP BY s.scope, s.price_band
ORDER BY s.scope, s.price_band;

-- Q08 l2TrendChart：物理字段只能证明 L2/L3，不能命名为 L2+。
WITH base AS (
  SELECT launch_year, brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), equipped AS (
  SELECT DISTINCT brand, serial_name, car_name
  FROM vehicle_params
  WHERE type_name = '驾驶辅助级别'
    AND type_value LIKE 'L2%'
), model_flag AS (
  SELECT b.*, (e.brand IS NOT NULL)::int AS equipped
  FROM base b LEFT JOIN equipped e USING (brand, serial_name, car_name)
), series_flag AS (
  SELECT launch_year, brand, serial_name, MAX(equipped) AS equipped
  FROM model_flag
  GROUP BY launch_year, brand, serial_name
)
SELECT
  m.launch_year,
  COUNT(*) AS eligible_models,
  SUM(m.equipped) AS equipped_models,
  ROUND(100.0 * SUM(m.equipped) / COUNT(*), 2) AS model_rate_pct,
  (SELECT COUNT(*) FROM series_flag s WHERE s.launch_year = m.launch_year) AS eligible_series,
  (SELECT SUM(equipped) FROM series_flag s WHERE s.launch_year = m.launch_year) AS equipped_series,
  ROUND(100.0 * (SELECT SUM(equipped) FROM series_flag s WHERE s.launch_year = m.launch_year)
        / NULLIF((SELECT COUNT(*) FROM series_flag s WHERE s.launch_year = m.launch_year), 0), 2) AS series_rate_pct
FROM model_flag m
GROUP BY m.launch_year
ORDER BY m.launch_year;

-- Q09 l2PriceBandChart：L2 款型配备率按上市年、价格带。
WITH base AS (
  SELECT
    launch_year, brand, serial_name, car_name,
    CASE
      WHEN price IS NULL THEN NULL
      WHEN price < 10 THEN '10万以下'
      WHEN price < 15 THEN '10-15万'
      WHEN price < 20 THEN '15-20万'
      WHEN price < 30 THEN '20-30万'
      ELSE '30万以上'
    END AS price_band
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND price IS NOT NULL
), equipped AS (
  SELECT DISTINCT brand, serial_name, car_name
  FROM vehicle_params
  WHERE type_name = '驾驶辅助级别' AND type_value LIKE 'L2%'
)
SELECT
  b.launch_year,
  b.price_band,
  COUNT(*) AS eligible_models,
  COUNT(e.brand) AS equipped_models,
  ROUND(100.0 * COUNT(e.brand) / COUNT(*), 2) AS model_rate_pct
FROM base b
LEFT JOIN equipped e USING (brand, serial_name, car_name)
GROUP BY b.launch_year, b.price_band
ORDER BY b.launch_year, b.price_band;

-- Q10 l2PriceBoxplotChart：L2 款型价格箱线图，须使用 1.5*IQR 范围内真实观测值作为须。
WITH equipped AS (
  SELECT DISTINCT brand, serial_name, car_name
  FROM vehicle_params
  WHERE type_name = '驾驶辅助级别' AND type_value LIKE 'L2%'
), prices AS (
  SELECT b.launch_year, b.price
  FROM vehicle_model_base b
  JOIN equipped e USING (brand, serial_name, car_name)
  WHERE b.launch_year BETWEEN 2020 AND 2026
    AND b.vehicle_level IS DISTINCT FROM '皮卡'
    AND b.price IS NOT NULL
), quartiles AS (
  SELECT
    launch_year,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY price) AS q1,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY price) AS median,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY price) AS q3
  FROM prices
  GROUP BY launch_year
)
SELECT
  q.launch_year,
  COUNT(*) AS sample_n,
  ROUND(MIN(p.price) FILTER (WHERE p.price >= q.q1 - 1.5 * (q.q3 - q.q1)), 2) AS lower_whisker,
  ROUND(q.q1::numeric, 2) AS q1,
  ROUND(q.median::numeric, 2) AS median,
  ROUND(q.q3::numeric, 2) AS q3,
  ROUND(MAX(p.price) FILTER (WHERE p.price <= q.q3 + 1.5 * (q.q3 - q.q1)), 2) AS upper_whisker,
  COUNT(*) FILTER (WHERE p.price < q.q1 - 1.5 * (q.q3 - q.q1)
                    OR p.price > q.q3 + 1.5 * (q.q3 - q.q1)) AS outlier_n,
  ROUND(MAX(p.price), 2) AS absolute_max
FROM quartiles q
JOIN prices p USING (launch_year)
GROUP BY q.launch_year, q.q1, q.median, q.q3
ORDER BY q.launch_year;

-- Q11 highAdasTrendChart：按页面标题采用“高速辅助驾驶”物理字段。
WITH base AS (
  SELECT launch_year, brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), equipped AS (
  SELECT DISTINCT brand, serial_name, car_name
  FROM vehicle_params
  WHERE type_name = '高速辅助驾驶'
    AND type_value IS NOT NULL
    AND type_value NOT IN ('', '-', '无', '未配备', '不配备')
), model_flag AS (
  SELECT b.*, (e.brand IS NOT NULL)::int AS equipped
  FROM base b LEFT JOIN equipped e USING (brand, serial_name, car_name)
), series_flag AS (
  SELECT launch_year, brand, serial_name, MAX(equipped) AS equipped
  FROM model_flag
  GROUP BY launch_year, brand, serial_name
)
SELECT
  m.launch_year,
  COUNT(*) AS eligible_models,
  SUM(m.equipped) AS equipped_models,
  ROUND(100.0 * SUM(m.equipped) / COUNT(*), 2) AS model_rate_pct,
  (SELECT COUNT(*) FROM series_flag s WHERE s.launch_year = m.launch_year) AS eligible_series,
  (SELECT SUM(equipped) FROM series_flag s WHERE s.launch_year = m.launch_year) AS equipped_series,
  ROUND(100.0 * (SELECT SUM(equipped) FROM series_flag s WHERE s.launch_year = m.launch_year)
        / NULLIF((SELECT COUNT(*) FROM series_flag s WHERE s.launch_year = m.launch_year), 0), 2) AS series_rate_pct
FROM model_flag m
GROUP BY m.launch_year
ORDER BY m.launch_year;

-- Q12 adasChipShareChart：2026 年有效披露芯片款型内部份额。
WITH base AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026 AND vehicle_level IS DISTINCT FROM '皮卡'
), chip AS (
  SELECT DISTINCT b.brand, b.serial_name, b.car_name, vp.type_value
  FROM base b
  JOIN vehicle_params vp USING (brand, serial_name, car_name)
  WHERE vp.type_name = '辅助驾驶芯片'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
), normalized AS (
  SELECT
    brand, serial_name, car_name,
    CASE
      WHEN type_value ILIKE '%Orin-X%' THEN '英伟达 Orin-X'
      WHEN type_value ILIKE '%Mobileye EyeQ4%' THEN 'Mobileye EyeQ4'
      WHEN type_value ILIKE '%地平线征程3%' THEN '地平线征程3'
      WHEN type_value ILIKE '%MDC610%' THEN '华为 MDC610'
      WHEN type_value ILIKE '%神玑NX9031%' THEN '神玑NX9031'
      WHEN type_value ILIKE '%凌芯01%' THEN '凌芯01'
      ELSE '其他'
    END AS chip_family
  FROM chip
)
SELECT
  chip_family,
  COUNT(DISTINCT (brand, serial_name, car_name)) AS models,
  ROUND(100.0 * COUNT(DISTINCT (brand, serial_name, car_name))
        / NULLIF((SELECT COUNT(DISTINCT (brand, serial_name, car_name)) FROM chip), 0), 2) AS internal_share_pct
FROM normalized
GROUP BY chip_family
ORDER BY models DESC;

-- Q13 lidarShareChart：全观察期有效激光雷达品牌披露的原始枚举份额。
WITH base AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), lidar AS (
  SELECT DISTINCT b.brand, b.serial_name, b.car_name, vp.type_value
  FROM base b
  JOIN vehicle_params vp USING (brand, serial_name, car_name)
  WHERE vp.type_name = '激光雷达品牌'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
)
SELECT
  type_value AS supplier_value,
  COUNT(*) AS models,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS disclosed_value_share_pct
FROM lidar
GROUP BY type_value
ORDER BY models DESC;

-- Q14 cockpitChipRateChart / Q15 hudRateChart / Q18 四项舒适配置：一次扫描统一计算。
WITH base AS (
  SELECT launch_year, brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), flags AS (
  SELECT
    brand, serial_name, car_name,
    BOOL_OR(type_name = '车机芯片'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS has_cockpit_chip,
    BOOL_OR(type_name = 'HUD抬头显示'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS has_hud,
    BOOL_OR(type_name = '车载冰箱'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS has_fridge,
    BOOL_OR(type_name = '后排多媒体屏幕数量'
            AND NULLIF(regexp_replace(type_value, '[^0-9.]', '', 'g'), '')::numeric > 0) AS has_rear_screen,
    BOOL_OR(type_name = '零重力座椅功能'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS has_zero_gravity,
    BOOL_OR(type_name = '第二排座椅功能' AND type_value LIKE '%按摩%') AS has_rear_massage
  FROM vehicle_params
  WHERE type_name IN ('车机芯片', 'HUD抬头显示', '车载冰箱',
                      '后排多媒体屏幕数量', '零重力座椅功能', '第二排座椅功能')
  GROUP BY brand, serial_name, car_name
)
SELECT
  b.launch_year,
  COUNT(*) AS eligible_models,
  COUNT(*) FILTER (WHERE f.has_cockpit_chip) AS cockpit_chip_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_cockpit_chip) / COUNT(*), 2) AS cockpit_chip_rate_pct,
  COUNT(*) FILTER (WHERE f.has_hud) AS hud_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_hud) / COUNT(*), 2) AS hud_rate_pct,
  COUNT(*) FILTER (WHERE f.has_fridge) AS fridge_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_fridge) / COUNT(*), 2) AS fridge_rate_pct,
  COUNT(*) FILTER (WHERE f.has_rear_screen) AS rear_screen_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_rear_screen) / COUNT(*), 2) AS rear_screen_rate_pct,
  COUNT(*) FILTER (WHERE f.has_zero_gravity) AS zero_gravity_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_zero_gravity) / COUNT(*), 2) AS zero_gravity_rate_pct,
  COUNT(*) FILTER (WHERE f.has_rear_massage) AS rear_massage_models,
  ROUND(100.0 * COUNT(*) FILTER (WHERE f.has_rear_massage) / COUNT(*), 2) AS rear_massage_rate_pct
FROM base b
LEFT JOIN flags f USING (brand, serial_name, car_name)
GROUP BY b.launch_year
ORDER BY b.launch_year;

-- Q16 screenSizeChart：只接受单一可解析英寸数值，同时披露有效样本数。
WITH base AS (
  SELECT launch_year, brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2020 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), screen AS (
  SELECT DISTINCT
    b.launch_year, b.brand, b.serial_name, b.car_name,
    vp.type_value::numeric AS screen_inches
  FROM base b
  JOIN vehicle_params vp USING (brand, serial_name, car_name)
  WHERE vp.type_name = '中控屏幕尺寸[英寸]'
    AND vp.type_value ~ '^\s*[0-9]+(\.[0-9]+)?\s*$'
    AND vp.type_value::numeric BETWEEN 5 AND 40
)
SELECT
  launch_year,
  COUNT(*) AS valid_sample_n,
  ROUND(AVG(screen_inches), 2) AS average_screen_inches,
  ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY screen_inches)::numeric, 2) AS median_screen_inches
FROM screen
GROUP BY launch_year
ORDER BY launch_year;

-- Q17 cockpitChipShareChart：2026 年有效披露车机芯片款型内部份额。
WITH base AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026 AND vehicle_level IS DISTINCT FROM '皮卡'
), chip AS (
  SELECT DISTINCT b.brand, b.serial_name, b.car_name, vp.type_value
  FROM base b
  JOIN vehicle_params vp USING (brand, serial_name, car_name)
  WHERE vp.type_name = '车机芯片'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
), normalized AS (
  SELECT
    brand, serial_name, car_name,
    CASE
      WHEN type_value ILIKE '%8155%' THEN '高通8155'
      WHEN type_value ILIKE '%8295%' THEN '高通8295'
      WHEN type_value ILIKE '%6125%' THEN '高通6125'
      WHEN type_value ILIKE '%龙鹰一号%' OR type_value ILIKE '%龍鷹一号%' OR type_value ILIKE '%龍鹰一号%' THEN '龙鹰一号'
      WHEN type_value ILIKE '%D100%' THEN 'D100'
      ELSE '其他'
    END AS chip_family
  FROM chip
)
SELECT
  chip_family,
  COUNT(DISTINCT (brand, serial_name, car_name)) AS models,
  ROUND(100.0 * COUNT(DISTINCT (brand, serial_name, car_name))
        / NULLIF((SELECT COUNT(DISTINCT (brand, serial_name, car_name)) FROM chip), 0), 2) AS internal_share_pct
FROM normalized
GROUP BY chip_family
ORDER BY models DESC;

-- Q19 coreConfigTrendChart：空气悬架、激光雷达、HUD 的款型率和车系覆盖率。
WITH base AS (
  SELECT launch_year, brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), flags AS (
  SELECT
    brand, serial_name, car_name,
    BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS air_suspension,
    BOOL_OR(type_name = '激光雷达数量'
            AND NULLIF(regexp_replace(type_value, '[^0-9.]', '', 'g'), '')::numeric >= 1) AS lidar,
    BOOL_OR(type_name = 'HUD抬头显示'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS hud
  FROM vehicle_params
  WHERE type_name IN ('可调悬架种类', '激光雷达数量', 'HUD抬头显示')
  GROUP BY brand, serial_name, car_name
), model_flags AS (
  SELECT
    b.*,
    COALESCE(f.air_suspension, false) AS air_suspension,
    COALESCE(f.lidar, false) AS lidar,
    COALESCE(f.hud, false) AS hud
  FROM base b LEFT JOIN flags f USING (brand, serial_name, car_name)
), long_flags AS (
  SELECT launch_year, brand, serial_name, car_name, config_name, equipped
  FROM model_flags
  CROSS JOIN LATERAL (VALUES
    ('空气悬架', air_suspension),
    ('激光雷达', lidar),
    ('HUD', hud)
  ) v(config_name, equipped)
), series_flags AS (
  SELECT launch_year, brand, serial_name, config_name, BOOL_OR(equipped) AS equipped
  FROM long_flags
  GROUP BY launch_year, brand, serial_name, config_name
), model_agg AS (
  SELECT
    launch_year, config_name,
    COUNT(*) AS eligible_models,
    COUNT(*) FILTER (WHERE equipped) AS equipped_models
  FROM long_flags
  GROUP BY launch_year, config_name
), series_agg AS (
  SELECT
    launch_year, config_name,
    COUNT(*) AS eligible_series,
    COUNT(*) FILTER (WHERE equipped) AS equipped_series
  FROM series_flags
  GROUP BY launch_year, config_name
)
SELECT
  m.launch_year,
  m.config_name,
  m.eligible_models,
  m.equipped_models,
  ROUND(100.0 * m.equipped_models / m.eligible_models, 2) AS model_rate_pct,
  s.eligible_series,
  s.equipped_series,
  ROUND(100.0 * s.equipped_series / s.eligible_series, 2) AS series_rate_pct
FROM model_agg m
JOIN series_agg s USING (launch_year, config_name)
ORDER BY m.config_name, m.launch_year;

-- Q20 coreConfigHeatmapChart：三种配置 x 三种维度 x 两种颗粒度的独立配置率。
-- 车系口径在每个维度分组内独立去重，不复用款型矩阵，也不做比例放大。
WITH base AS (
  SELECT
    launch_year, brand, serial_name, car_name,
    CASE
      WHEN price IS NULL THEN NULL
      WHEN price < 10 THEN '10万以下'
      WHEN price < 15 THEN '10-15万'
      WHEN price < 20 THEN '15-20万'
      WHEN price < 25 THEN '20-25万'
      WHEN price < 30 THEN '25-30万'
      WHEN price < 40 THEN '30-40万'
      WHEN price < 50 THEN '40-50万'
      ELSE '50万以上'
    END AS price_band,
    CASE
      WHEN wheelbase_mm IS NULL THEN NULL
      WHEN wheelbase_mm < 2600 THEN '2600以下'
      WHEN wheelbase_mm < 2700 THEN '2600-2700'
      WHEN wheelbase_mm < 2800 THEN '2700-2800'
      WHEN wheelbase_mm < 2900 THEN '2800-2900'
      WHEN wheelbase_mm < 3000 THEN '2900-3000'
      ELSE '3000以上'
    END AS wheelbase_band,
    CASE
      WHEN vehicle_level LIKE '%MPV%' THEN 'MPV'
      WHEN vehicle_level IN ('微型车', '小型车', '小型SUV') THEN 'A0级'
      WHEN vehicle_level IN ('紧凑型车', '紧凑型SUV') THEN 'A级'
      WHEN vehicle_level IN ('中型车', '中型SUV') THEN 'B级'
      WHEN vehicle_level IN ('中大型车', '中大型SUV') THEN 'C级'
      WHEN vehicle_level IN ('大型车', '大型SUV') THEN 'D级'
      ELSE '其他'
    END AS level_band
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
), flags AS (
  SELECT
    brand, serial_name, car_name,
    BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS air_suspension,
    BOOL_OR(type_name = '激光雷达数量'
            AND NULLIF(regexp_replace(type_value, '[^0-9.]', '', 'g'), '')::numeric >= 1) AS lidar,
    BOOL_OR(type_name = 'HUD抬头显示'
            AND type_value IS NOT NULL
            AND type_value NOT IN ('', '-', '无', '未配备', '不配备')) AS hud
  FROM vehicle_params
  WHERE type_name IN ('可调悬架种类', '激光雷达数量', 'HUD抬头显示')
  GROUP BY brand, serial_name, car_name
), model_flags AS (
  SELECT
    b.*,
    COALESCE(f.air_suspension, false) AS air_suspension,
    COALESCE(f.lidar, false) AS lidar,
    COALESCE(f.hud, false) AS hud
  FROM base b LEFT JOIN flags f USING (brand, serial_name, car_name)
), long_flags AS (
  SELECT
    m.launch_year, m.brand, m.serial_name, m.car_name,
    cfg.config_name, cfg.equipped,
    dim.dimension_name, dim.band
  FROM model_flags m
  CROSS JOIN LATERAL (VALUES
    ('空气悬架', m.air_suspension),
    ('激光雷达', m.lidar),
    ('HUD', m.hud)
  ) cfg(config_name, equipped)
  CROSS JOIN LATERAL (VALUES
    ('价格段', m.price_band),
    ('轴距段', m.wheelbase_band),
    ('级别', m.level_band)
  ) dim(dimension_name, band)
), trim_result AS (
  SELECT
    '款型' AS grain, launch_year, config_name, dimension_name, band,
    COUNT(*) AS eligible_count,
    COUNT(*) FILTER (WHERE equipped) AS equipped_count
  FROM long_flags
  WHERE band IS NOT NULL AND band <> '其他'
  GROUP BY launch_year, config_name, dimension_name, band
), series_band_flags AS (
  SELECT
    launch_year, brand, serial_name, config_name, dimension_name, band,
    BOOL_OR(equipped) AS equipped
  FROM long_flags
  WHERE band IS NOT NULL AND band <> '其他'
  GROUP BY launch_year, brand, serial_name, config_name, dimension_name, band
), series_result AS (
  SELECT
    '车系' AS grain, launch_year, config_name, dimension_name, band,
    COUNT(*) AS eligible_count,
    COUNT(*) FILTER (WHERE equipped) AS equipped_count
  FROM series_band_flags
  GROUP BY launch_year, config_name, dimension_name, band
)
SELECT
  grain, launch_year, config_name, dimension_name, band,
  eligible_count, equipped_count,
  ROUND(100.0 * equipped_count / NULLIF(eligible_count, 0), 2) AS config_rate_pct
FROM (
  SELECT * FROM trim_result
  UNION ALL
  SELECT * FROM series_result
) x
ORDER BY config_name, grain, dimension_name, band, launch_year;
