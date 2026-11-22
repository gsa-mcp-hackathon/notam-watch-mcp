"""Regression coverage ported from the NOTAM Filter Service."""

# ruff: noqa: E501

from unittest import TestCase

from nfs_ingest.services.notam_altitude import (
    merge_vertical_limits,
    normalize_vertical_limits,
)
from nfs_ingest.services.notam_text_parser import AirspaceTextParser

POLYGON_TEXT = "AIRSPACE BOUNDED BY 40.0N 75.0W 40.0N 74.0W 41.0N 74.0W 41.0N 75.0W 5000FT MSL-10000FT MSL"


def parse(text, translation="", references=None, airports=None):
    return AirspaceTextParser.parse(text, translation, reference_lookup=references or {}, airport_lookup=airports or {})


class TextEnrichmentTests(TestCase):
    def test_duplicate_translation_does_not_double_polygon_ring(self):
        original = parse(POLYGON_TEXT)
        repeated = parse(POLYGON_TEXT, POLYGON_TEXT)
        self.assertEqual(repeated["geometry"], original["geometry"])
        self.assertEqual(repeated["point_count"], 4)
        self.assertEqual(repeated["vertical_limits"], original["vertical_limits"])

    def test_lowercase_hemispheres_keep_sign(self):
        self.assertEqual(parse(POLYGON_TEXT.lower())["geometry"], parse(POLYGON_TEXT)["geometry"])

    def test_mixed_coordinate_formats_keep_text_order(self):
        result = parse("AIRSPACE BOUNDED BY 400000N 0750000W 40.0N 74.0W 4100N 07400W 410000N 0750000W")
        self.assertEqual(result["geometry"]["coordinates"][0], [[-75, 40], [-74, 40], [-74, 41], [-75, 41], [-75, 40]])

    def test_invalid_minutes_are_not_silently_normalized(self):
        result = parse("AIRSPACE WI 5NM RADIUS OF 406100N 0750000W")
        self.assertIsNone(result["geometry"])
        self.assertTrue(result["geometry_warnings"])

    def test_translation_is_geometry_fallback_not_an_extra_component(self):
        result = parse("AIRSPACE CLOSED", POLYGON_TEXT)
        self.assertEqual(result["geometry"], parse(POLYGON_TEXT)["geometry"])

    def test_multiple_altitude_ranges_are_not_collapsed_to_first(self):
        result = parse("R3103 5000FT MSL-10000FT MSL AND W107 20000FT MSL-25000FT MSL")
        self.assertEqual(result["vertical_limits"], {})
        self.assertTrue(result["vertical_warnings"])

    def test_inverted_text_bounds_are_rejected(self):
        result = parse("AIRSPACE 10000FT MSL-5000FT MSL")
        self.assertEqual(result["vertical_limits"], {})
        self.assertTrue(result["vertical_warnings"])

    def test_vertical_datums_units_and_unlimited_are_preserved(self):
        result = parse("AIRSPACE SFC TO FL 180")
        limits = normalize_vertical_limits(result["vertical_limits"])
        self.assertEqual(limits, {"lower_ft": 0, "lower_reference": "AGL", "upper_ft": 18000, "upper_reference": "STD"})
        self.assertEqual(parse("1000M AMSL-UNL")["vertical_limits"]["upper"]["reference"], "UNBOUNDED")

    def test_negative_msl_bounds_keep_the_sign(self):
        result = parse("AIRSPACE -500FT MSL-2000FT MSL")
        self.assertEqual(result["vertical_limits"]["lower"]["value"], -500)

    def test_known_bounds_are_not_overwritten_by_conflicting_text(self):
        known = {"lower_ft": 5000, "upper_ft": 10000, "lower_reference": "MSL", "upper_reference": "MSL"}
        proposed = parse("1000FT AGL-2000FT AGL")["vertical_limits"]
        merged, warnings = merge_vertical_limits(known, proposed)
        self.assertEqual(merged, known)
        self.assertEqual(len(warnings), 2)

    def test_unknown_bounds_can_be_filled_from_explicit_text(self):
        merged, warnings = merge_vertical_limits({}, parse("5000FT MSL-10000FT MSL")["vertical_limits"])
        self.assertEqual(merged["lower_ft"], 5000)
        self.assertEqual(merged["upper_reference"], "MSL")
        self.assertEqual(warnings, [])

    def test_partial_fill_cannot_invert_known_interval(self):
        merged, warnings = merge_vertical_limits({"upper_ft": 4000, "upper_reference": "MSL"}, parse("5000FT MSL-10000FT MSL")["vertical_limits"])
        self.assertIsNone(merged["lower_ft"])
        self.assertEqual(merged["upper_ft"], 4000)
        self.assertTrue(warnings)

    def test_unrelated_distance_does_not_create_a_circle(self):
        result = parse("OBST 40.0N 75.0W 5NM EAST OF AIRPORT")
        self.assertEqual(result["geometry"]["type"], "Point")
        self.assertIsNone(result["circle_center"])

    def test_explicit_circle_has_queryable_center_and_radius(self):
        result = parse("AIRSPACE WI 5NM RADIUS OF 40.0N 75.0W 5000FT MSL-10000FT MSL")
        self.assertEqual(result["circle_center"], [-75, 40])
        self.assertEqual(result["horizontal_radius_nm"], 5)
        self.assertEqual(result["geometry_source"], "TEXT_CIRCLE")

    def test_kilometer_circle_radius_is_converted(self):
        self.assertEqual(parse("AIRSPACE RADIUS 1.852KM 40.0N 75.0W")["horizontal_radius_nm"], 1)

    def test_leading_decimal_circle_radius_is_not_ten_times_too_large(self):
        text = "AIRSPACE NUMEROUS UAS WI AN AREA DEFINED AS .5NM RADIUS OF 334834N0840838W (9.6NM SE PDK) SFC-400FT AGL DLY 1700-0500"
        result = parse(text)
        self.assertEqual(result["horizontal_radius_nm"], 0.5)
        self.assertAlmostEqual(result["circle_center"][0], -(84 + 8 / 60 + 38 / 3600))
        self.assertAlmostEqual(result["circle_center"][1], 33 + 48 / 60 + 34 / 3600)
        self.assertIsNone(result["airport_reference"])
        self.assertEqual(normalize_vertical_limits(result["vertical_limits"])["upper_ft"], 400)

    def test_leading_decimal_radius_variants(self):
        for description in ("RADIUS .5NM", ".5NM RADIUS OF", "WITHIN .5NM OF"):
            with self.subTest(description=description):
                self.assertEqual(parse(f"AIRSPACE {description} 40.0N 75.0W")["horizontal_radius_nm"], 0.5)

    def test_multiple_radii_do_not_choose_the_first(self):
        result = parse("AIRSPACE 5NM RADIUS AND 10NM RADIUS OF 40.0N 75.0W")
        self.assertIsNone(result["geometry"])
        self.assertIsNone(result["circle_center"])

    def test_multiple_circle_centers_are_not_joined_into_a_polygon(self):
        result = parse("AIRSPACE 5NM RADIUS OF EACH 40.0N 75.0W 40.0N 74.0W 41.0N 74.0W")
        self.assertIsNone(result["geometry"])
        self.assertTrue(result["geometry_warnings"])

    def test_q_line_center_is_not_added_to_body_polygon(self):
        result = parse("Q) KZNY/QWALW/IV/M/W/000/100/4050N 07450W 025 A) KZNY E) " + POLYGON_TEXT)
        self.assertEqual(result["geometry"], parse(POLYGON_TEXT)["geometry"])
        self.assertEqual(result["point_count"], 4)

    def test_q_line_center_cannot_supply_missing_circle_center(self):
        result = parse("Q) KZNY/QWALW/IV/M/W/000/100/4050N 07450W 025\nE) AIRSPACE WI 5NM RADIUS")
        self.assertIsNone(result["geometry"])
        self.assertIsNone(result["circle_center"])

    def test_notam_identifier_is_not_an_airspace_reference(self):
        self.assertEqual(AirspaceTextParser.reference_candidates("A1234/26 AIRSPACE"), [])

    def test_reference_lookup_uses_only_unique_matches(self):
        reference = {"id": 1, "geometry": parse(POLYGON_TEXT)["geometry"], "designator": "R3103", "name": "TEST"}
        result = parse("R-3103 ACTIVE 5000FT MSL-10000FT MSL", references={"R3103": [reference]})
        self.assertEqual(result["geometry_source"], "SAA_AIRSPACE_LOOKUP")
        ambiguous = parse("R-3103 ACTIVE", references={"R3103": [reference, {**reference, "id": 2}]})
        self.assertIsNone(ambiguous["geometry"])

    def test_multiple_airspaces_do_not_choose_first_footprint(self):
        geometry = parse(POLYGON_TEXT)["geometry"]
        references = {
            "R3103": [{"id": 1, "geometry": geometry, "designator": "R3103", "name": "FIRST"}],
            "W107": [{"id": 2, "geometry": geometry, "designator": "W107", "name": "SECOND"}],
        }
        result = parse("R3103 AND W107 ACTIVE", references=references)
        self.assertIsNone(result["geometry"])
        self.assertTrue(result["geometry_warnings"])

    def test_arc_is_not_replaced_by_straight_polygon_edges(self):
        result = parse(POLYGON_TEXT + " THEN CLOCKWISE ARC")
        self.assertIsNone(result["geometry"])

    def test_missing_limits_stay_unknown(self):
        self.assertEqual(parse("AIRSPACE CLOSED")["vertical_limits"], {})

