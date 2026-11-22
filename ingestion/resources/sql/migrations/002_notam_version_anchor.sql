-- Adds a location anchor to every NOTAM version, including NOTAMs without a
-- usable hazard shape (airport/obstacle point NOTAMs). The anchor is a
-- reference point for proximity and corridor queries; it is NOT a hazard
-- volume. anchor_source priority: NMS_POINT, HAZARD_CENTER, TEXT_COORDINATE,
-- NMS_QLINE, AIRPORT_REFERENCE, NONE. anchor_qline_offset_nm is a data-quality
-- signal (distance between the NMS point and the coarse Q-line coordinate).
-- Run once against the existing development table before loading parser v2.

ALTER TABLE `users`.`nfs_demo`.`nfs_notam_versions`
ADD COLUMNS (
  anchor_longitude DOUBLE,
  anchor_latitude DOUBLE,
  anchor_h3_r7 BIGINT,
  anchor_source STRING,
  anchor_radius_nm DOUBLE,
  anchor_qline_offset_nm DOUBLE
);
