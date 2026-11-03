"""
test_app_routes.py - automated test of Flask routes, template rendering, and privacy checks.
"""
import unittest
from pathlib import Path
import app as flask_app
import pandas as pd

class AppRouteTestCase(unittest.TestCase):
    def setUp(self):
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()

    def test_index_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"New Analysis Run", res.data)
        self.assertIn(b"uploadForm", res.data)
        self.assertIn(b"progress-fill", res.data)

    def test_method_page(self):
        res = self.client.get("/method")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Rules & Analysis Methodology", res.data)
        self.assertIn(b"Jaccard", res.data)

    def test_history_page(self):
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Past Analysis Runs", res.data)

    def test_stored_page(self):
        res = self.client.get("/stored")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Stored Outage Events Repository", res.data)

    def test_privacy_in_table_renderer(self):
        # Ensure consumer name / address columns are stripped
        df = pd.DataFrame({
            "Consumer No": ["1001", "1002"],
            "Consumer Name": ["Alice Smith", "Bob Jones"],
            "Consumer Address": ["123 Main St", "456 Oak Rd"],
            "Result": ["MATCH", "MISMATCH"]
        })
        rendered = flask_app.table(df)
        self.assertNotIn("Alice Smith", rendered)
        self.assertNotIn("Bob Jones", rendered)
        self.assertNotIn("123 Main St", rendered)
        self.assertIn("1001", rendered)
        self.assertIn("MATCH", rendered)

if __name__ == "__main__":
    unittest.main()
