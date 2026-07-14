-- One-time migration from the legacy physical table to vehicle_model_base.
-- Run this before switching application metadata and Vanna training records.

BEGIN;

ALTER TABLE public.vehicle_params_wide
  RENAME TO vehicle_model_base;

ALTER TABLE public.vehicle_model_base
  RENAME CONSTRAINT vehicle_params_wide_pkey TO vehicle_model_base_pkey;

ALTER INDEX public.idx_vehicle_params_wide_brand
  RENAME TO idx_vehicle_model_base_brand;
ALTER INDEX public.idx_vehicle_params_wide_common_filters
  RENAME TO idx_vehicle_model_base_common_filters;
ALTER INDEX public.idx_vehicle_params_wide_energy_type
  RENAME TO idx_vehicle_model_base_energy_type;
ALTER INDEX public.idx_vehicle_params_wide_launch_year
  RENAME TO idx_vehicle_model_base_launch_year;
ALTER INDEX public.idx_vehicle_params_wide_price_band
  RENAME TO idx_vehicle_model_base_price_band;
ALTER INDEX public.idx_vehicle_params_wide_sale_status
  RENAME TO idx_vehicle_model_base_sale_status;
ALTER INDEX public.idx_vehicle_params_wide_serial_name
  RENAME TO idx_vehicle_model_base_serial_name;
ALTER INDEX public.idx_vehicle_params_wide_vehicle_level
  RENAME TO idx_vehicle_model_base_vehicle_level;
ALTER INDEX public.idx_vehicle_params_wide_wheelbase_mm
  RENAME TO idx_vehicle_model_base_wheelbase_mm;

ALTER TABLE public.vehicle_model_base
  ADD COLUMN IF NOT EXISTS motor_power_kw numeric(10, 2);

CREATE INDEX IF NOT EXISTS idx_vehicle_model_base_motor_power_kw
  ON public.vehicle_model_base (motor_power_kw);

COMMENT ON TABLE public.vehicle_model_base IS
  '款型级基础属性当前快照；唯一键为 brand + serial_name + car_name，不包含全量配置参数及字段变更历史。';

COMMENT ON COLUMN public.vehicle_model_base.motor_power_kw IS
  '款型电动机总功率，单位 kW；来自 vehicle_params.type_name=电动机总功率[kW]，不是前后电机功率相加值。';

CREATE VIEW public.vehicle_params_wide AS
SELECT *
FROM public.vehicle_model_base;

COMMENT ON VIEW public.vehicle_params_wide IS
  'vehicle_model_base 的迁移期兼容视图；新查询应使用 vehicle_model_base。';

COMMIT;
