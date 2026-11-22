-- Adds source-circle metadata required by text-derived NOTAM hazards.
-- This migration is intended for the existing development table and runs once.

ALTER TABLE `users`.`nfs_demo`.`nfs_notam_hazard_volumes`
ADD COLUMNS (
  horizontal_radius_nm DOUBLE,
  circle_center_longitude DOUBLE,
  circle_center_latitude DOUBLE
);

