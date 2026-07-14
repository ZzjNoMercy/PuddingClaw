-- Rebuild the model-grain vehicle parameter snapshot.
-- Wheelbase and motor-power bands are semantic rules and are intentionally not materialized here.

BEGIN;

CREATE TABLE IF NOT EXISTS public.vehicle_model_base (
  brand text NOT NULL,
  serial_name text NOT NULL,
  car_name text NOT NULL,
  car_name_full_year text NOT NULL,
  launch_date date,
  launch_year integer,
  launch_month integer,
  energy_type text,
  vehicle_level text,
  wheelbase_mm integer,
  motor_power_kw numeric(10, 2),
  price numeric(12, 2),
  price_band text,
  sale_status text,
  sale_status_matched boolean NOT NULL DEFAULT false,
  sale_status_source text,
  refreshed_at timestamp without time zone NOT NULL DEFAULT now(),
  PRIMARY KEY (brand, serial_name, car_name)
);

-- CREATE TABLE IF NOT EXISTS does not add new columns to an existing table.
ALTER TABLE public.vehicle_model_base
  ADD COLUMN IF NOT EXISTS wheelbase_mm integer,
  ADD COLUMN IF NOT EXISTS motor_power_kw numeric(10, 2);

TRUNCATE TABLE public.vehicle_model_base;

WITH base_models AS (
  SELECT DISTINCT
    brand,
    serial_name,
    car_name,
    regexp_replace(car_name, '^([0-9]{2})款', '20\1款') AS car_name_full_year
  FROM public.vehicle_params
  WHERE brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND car_name IS NOT NULL
),
model_dims AS (
  SELECT
    brand,
    serial_name,
    car_name,
    MAX(type_value) FILTER (WHERE type_name = '上市时间') AS launch_date_raw,
    MAX(type_value) FILTER (WHERE type_name = '能源类型') AS energy_type,
    MAX(type_value) FILTER (WHERE type_name = '级别') AS vehicle_level,
    MAX(type_value) FILTER (WHERE type_name = '轴距[mm]') AS wheelbase_raw,
    MAX(type_value) FILTER (WHERE type_name = '电动机总功率[kW]') AS motor_power_raw,
    MAX(type_value) FILTER (WHERE type_name = '厂商指导价') AS price_raw
  FROM public.vehicle_params
  WHERE type_name IN (
    '上市时间',
    '能源类型',
    '级别',
    '轴距[mm]',
    '电动机总功率[kW]',
    '厂商指导价'
  )
  GROUP BY brand, serial_name, car_name
),
sale_status_dedup AS (
  SELECT
    serial_name,
    btrim(car_name) AS car_name,
    CASE
      WHEN BOOL_OR(sale_status = '在售') THEN '在售'
      WHEN BOOL_OR(sale_status = '停售') THEN '停售'
      ELSE MAX(sale_status)
    END AS sale_status
  FROM public.vehicle_serial_info
  GROUP BY serial_name, btrim(car_name)
),
base_rows AS (
  SELECT
    b.brand,
    b.serial_name,
    b.car_name,
    b.car_name_full_year,
    CASE
      WHEN d.launch_date_raw ~ '^\d{4}-\d{2}-\d{2}$'
      THEN d.launch_date_raw::date
      ELSE NULL
    END AS launch_date,
    d.energy_type,
    d.vehicle_level,
    NULLIF(
      regexp_replace(COALESCE(d.wheelbase_raw, ''), '[^0-9]', '', 'g'),
      ''
    )::integer AS wheelbase_mm,
    CASE
      WHEN d.motor_power_raw ~ '^[0-9]+(\.[0-9]+)?$'
      THEN d.motor_power_raw::numeric(10, 2)
      ELSE NULL
    END AS motor_power_kw,
    CASE
      WHEN d.price_raw ~ '^[0-9]+(\.[0-9]+)?$'
      THEN d.price_raw::numeric(12, 2)
      ELSE NULL
    END AS price,
    s.sale_status
  FROM base_models b
  LEFT JOIN model_dims d
    ON d.brand = b.brand
   AND d.serial_name = b.serial_name
   AND d.car_name = b.car_name
  LEFT JOIN sale_status_dedup s
    ON s.serial_name = b.serial_name
   AND s.car_name = b.car_name_full_year
)
INSERT INTO public.vehicle_model_base (
  brand,
  serial_name,
  car_name,
  car_name_full_year,
  launch_date,
  launch_year,
  launch_month,
  energy_type,
  vehicle_level,
  wheelbase_mm,
  motor_power_kw,
  price,
  price_band,
  sale_status,
  sale_status_matched,
  sale_status_source,
  refreshed_at
)
SELECT
  brand,
  serial_name,
  car_name,
  car_name_full_year,
  launch_date,
  EXTRACT(YEAR FROM launch_date)::integer AS launch_year,
  EXTRACT(MONTH FROM launch_date)::integer AS launch_month,
  energy_type,
  vehicle_level,
  wheelbase_mm,
  motor_power_kw,
  price,
  CASE
    WHEN price IS NULL THEN NULL
    WHEN price < 5 THEN '5万以下'
    WHEN price >= 5 AND price < 10 THEN '5-10万元'
    WHEN price >= 10 AND price < 15 THEN '10-15万元'
    WHEN price >= 15 AND price < 20 THEN '15-20万元'
    WHEN price >= 20 AND price < 30 THEN '20-30万元'
    WHEN price >= 30 AND price < 40 THEN '30-40万元'
    WHEN price >= 40 AND price < 50 THEN '40-50万元'
    ELSE '50万以上'
  END AS price_band,
  sale_status,
  sale_status IS NOT NULL AS sale_status_matched,
  CASE WHEN sale_status IS NOT NULL THEN 'vehicle_serial_info' ELSE NULL END AS sale_status_source,
  now()
FROM base_rows;

CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_launch_year
  ON public.vehicle_model_base (launch_year);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_energy_type
  ON public.vehicle_model_base (energy_type);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_vehicle_level
  ON public.vehicle_model_base (vehicle_level);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_wheelbase_mm
  ON public.vehicle_model_base (wheelbase_mm);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_motor_power_kw
  ON public.vehicle_model_base (motor_power_kw);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_price_band
  ON public.vehicle_model_base (price_band);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_brand
  ON public.vehicle_model_base (brand);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_serial_name
  ON public.vehicle_model_base (serial_name);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_sale_status
  ON public.vehicle_model_base (sale_status);
CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_common_filters
  ON public.vehicle_model_base (launch_year, energy_type, vehicle_level, sale_status);

ANALYZE public.vehicle_model_base;

COMMENT ON TABLE public.vehicle_model_base IS
  '款型级基础属性当前快照；唯一键为 brand + serial_name + car_name，不包含全量配置参数及字段变更历史。';

COMMENT ON COLUMN public.vehicle_model_base.motor_power_kw IS
  '款型电动机总功率，单位 kW；来自 vehicle_params.type_name=电动机总功率[kW]，不是前后电机功率相加值。';

COMMIT;
