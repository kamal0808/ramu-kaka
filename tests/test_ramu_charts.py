import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_charts as rc  # noqa: E402


class LineChartTests(unittest.TestCase):
    def test_all_gaps_renders_placeholder(self):
        svg = rc.svg_line_chart([{"label": "25 Sep", "value": None}])
        self.assertIn("No data yet", svg)

    def test_renders_points_and_labels(self):
        pts = [{"label": "24 Sep", "value": 6.0}, {"label": "25 Sep", "value": 8.0}]
        svg = rc.svg_line_chart(pts)
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)
        self.assertIn("24 Sep", svg)
        self.assertIn("25 Sep", svg)

    def test_gap_breaks_the_line_instead_of_drawing_zero(self):
        pts = [{"label": "a", "value": 6.0}, {"label": "b", "value": None}, {"label": "c", "value": 8.0}]
        svg = rc.svg_line_chart(pts)
        self.assertEqual(svg.count("<polyline"), 2)

    def test_target_line_rendered(self):
        svg = rc.svg_line_chart([{"label": "a", "value": 6.0}], target=8)
        self.assertIn("chart-target", svg)

    def test_flat_series_does_not_divide_by_zero(self):
        pts = [{"label": "a", "value": 5.0}, {"label": "b", "value": 5.0}]
        svg = rc.svg_line_chart(pts)  # would raise ZeroDivisionError if lo == hi weren't guarded
        self.assertIn("<polyline", svg)


class BarChartTests(unittest.TestCase):
    def test_no_points_renders_placeholder(self):
        svg = rc.svg_bar_chart([])
        self.assertIn("No data yet", svg)

    def test_renders_a_bar_per_point(self):
        pts = [{"label": "a", "value": 1}, {"label": "b", "value": 3}]
        svg = rc.svg_bar_chart(pts)
        self.assertEqual(svg.count("<rect"), 2)

    def test_cap_line_rendered(self):
        svg = rc.svg_bar_chart([{"label": "a", "value": 1}], cap=3)
        self.assertIn("chart-target", svg)

    def test_all_zero_values_does_not_divide_by_zero(self):
        pts = [{"label": "a", "value": 0}, {"label": "b", "value": 0}]
        svg = rc.svg_bar_chart(pts)
        self.assertEqual(svg.count("<rect"), 2)


if __name__ == "__main__":
    unittest.main()
