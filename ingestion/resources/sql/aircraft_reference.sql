-- FAA Aircraft Characteristics Database (ACD) reference tables.
--
-- Source: aircraft_data.xlsx (sheets ACD_Data A1:BD389 = 388 ICAO types, and
-- Data_Dictionary). Loaded 2026-10-28; source SHA-256 prefix 3e68848b6bb9 is
-- stored per row. To reload, upload the workbook to
-- /Volumes/users/nfs_demo/default/reference/aircraft_data.xlsx
-- and replace :source below with that path.
--
-- Rules for NOTAM aircraft-restriction applicability (do not infer):
--   * MTOW/MALW are maximums, never actual weight; actual weight is unknown.
--   * wingspan_ft_without_winglets and wingspan_ft_with_winglets are separate
--     alternatives (32 types have a winglet span); AAC min/max alternatives
--     (12 types) stay unresolved until selected.
--   * tail_height_oew_ft is at Operating Empty Weight.
--   * NULL means unknown (never zero). 'N/A' text in the workbook is NULL for
--     numeric columns; raw_json keeps every original value.
--   * UNKNOWN applicability must retain the NOTAM.

CREATE OR REPLACE TABLE `users`.`nfs_demo`.`nfs_aircraft_reference`
COMMENT 'FAA ACD reference by ICAO type designator for NOTAM aircraft-restriction applicability.'
AS
WITH src AS (
  SELECT * FROM read_files(:source, format => 'excel', headerRows => 1,
    dataAddress => 'ACD_Data!A1:BD389', schemaEvolutionMode => 'none')
), bin AS (
  SELECT sha2(content, 256) AS sha FROM read_files(:source, format => 'binaryFile')
)
SELECT
  upper(trim(ICAO_Code)) AS icao_code,
  nullif(trim(FAA_Designator), '') AS faa_designator,
  nullif(trim(Manufacturer), '') AS manufacturer,
  nullif(trim(Model_FAA), '') AS model_faa,
  nullif(trim(Model_BADA), '') AS model_bada,
  nullif(trim(Physical_Class_Engine), '') AS engine_class,
  CAST(Num_Engines AS INT) AS num_engines,
  nullif(trim(AAC), '') AS aac,
  nullif(trim(AAC_minimum), '') AS aac_minimum,
  nullif(trim(AAC_maximum), '') AS aac_maximum,
  nullif(trim(ADG), '') AS adg,
  nullif(trim(TDG), '') AS tdg,
  try_cast(nullif(trim(Approach_Speed_knot), 'N/A') AS DOUBLE) AS approach_speed_kt,
  CAST(Approach_Speed_minimum_knot AS DOUBLE) AS approach_speed_min_kt,
  CAST(Approach_Speed_maximum_knot AS DOUBLE) AS approach_speed_max_kt,
  CAST(Wingspan_ft_without_winglets_sharklets AS DOUBLE) AS wingspan_ft_without_winglets,
  CAST(Wingspan_ft_with_winglets_sharklets AS DOUBLE) AS wingspan_ft_with_winglets,
  CAST(Length_ft AS DOUBLE) AS length_ft,
  CAST(Tail_Height_at_OEW_ft AS DOUBLE) AS tail_height_oew_ft,
  CAST(Wheelbase_ft AS DOUBLE) AS wheelbase_ft,
  CAST(Cockpit_to_Main_Gear_ft AS DOUBLE) AS cockpit_to_main_gear_ft,
  CAST(Main_Gear_Width_ft AS DOUBLE) AS main_gear_width_ft,
  CAST(MTOW_lb AS DOUBLE) AS mtow_lb,
  try_cast(replace(nullif(trim(MALW_lb), 'N/A'), ',', '') AS DOUBLE) AS malw_lb,
  nullif(trim(Main_Gear_Config), '') AS main_gear_config,
  nullif(trim(ICAO_WTC), '') AS icao_wtc,
  CAST(Parking_Area_ft2 AS DOUBLE) AS parking_area_ft2,
  nullif(trim(Class), '') AS aircraft_class,
  nullif(trim(FAA_Weight), '') AS faa_weight_class,
  nullif(trim(CWT), '') AS cwt,
  nullif(trim(One_Half_Wake_Category), '') AS recat_1_5_category,
  nullif(trim(Two_Wake_Category_Appx_A), '') AS recat_2_appx_a,
  nullif(trim(Two_Wake_Category_Appx_B), '') AS recat_2_appx_b,
  CAST(Rotor_Diameter_ft AS DOUBLE) AS rotor_diameter_ft,
  nullif(trim(SRS), '') AS srs,
  nullif(trim(LAHSO), '') AS lahso,
  nullif(trim(FAA_Registry), '') AS faa_registry,
  CAST(Registration_Count AS BIGINT) AS registration_count,
  CAST(TMFS_Operations_FY24 AS BIGINT) AS tfms_operations_fy24,
  nullif(trim(Remarks), '') AS remarks,
  CAST(LastUpdate AS TIMESTAMP) AS source_last_update,
  to_json(struct(ICAO_Code, FAA_Designator, Manufacturer, Model_FAA, Model_BADA,
    Physical_Class_Engine, Num_Engines, AAC, AAC_minimum, AAC_maximum, ADG, TDG,
    Approach_Speed_knot, Approach_Speed_minimum_knot, Approach_Speed_maximum_knot,
    Wingspan_ft_without_winglets_sharklets, Wingspan_ft_with_winglets_sharklets,
    Length_ft, Tail_Height_at_OEW_ft, Wheelbase_ft, Cockpit_to_Main_Gear_ft,
    Main_Gear_Width_ft, MTOW_lb, MALW_lb, Main_Gear_Config, ICAO_WTC,
    Parking_Area_ft2, Class, FAA_Weight, CWT, One_Half_Wake_Category,
    Two_Wake_Category_Appx_A, Two_Wake_Category_Appx_B, Rotor_Diameter_ft, SRS,
    LAHSO, FAA_Registry, Registration_Count, TMFS_Operations_FY24, Remarks,
    LastUpdate)) AS raw_json,
  'aircraft_data.xlsx#ACD_Data' AS source_file,
  bin.sha AS source_sha256,
  current_timestamp() AS loaded_at
FROM src CROSS JOIN bin
WHERE ICAO_Code IS NOT NULL;

CREATE OR REPLACE TABLE `users`.`nfs_demo`.`nfs_aircraft_reference_dictionary`
COMMENT 'Data dictionary for nfs_aircraft_reference (workbook Data_Dictionary sheet).'
AS SELECT
  trim(Characteristic) AS source_column,
  trim(`Full Elaboration`) AS description,
  nullif(trim(Unit), 'Not Applicable') AS unit,
  trim(Note) AS note,
  nullif(trim(`Reference `), '') AS reference,
  trim(`Link to Reference`) AS reference_url
FROM read_files(:source, format => 'excel', headerRows => 1,
  dataAddress => 'Data_Dictionary!A1:F95', schemaEvolutionMode => 'none')
WHERE Characteristic IS NOT NULL AND trim(Characteristic) <> '';
