"""
test_e2e_job.py - Test results page, feeder drilldown, and download routes.
"""
import unittest
import shutil
from pathlib import Path
import app as flask_app
import topology_check as tc
import breakdown_check as bc
import build_hierarchy as bh
from run_check import load_reports, write_report
import pandas as pd

class JobViewsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flask_app.app.config["TESTING"] = True
        cls.client = flask_app.app.test_client()

        # Create a test job in JOBS
        cls.job_id = "test-job-12345"
        cls.folder = flask_app.DATA_DIR / "jobs" / cls.job_id
        cls.folder.mkdir(parents=True, exist_ok=True)

        cm, dm, fm = tc.load_master("Genus Consumer MI Data.xlsx")
        ev = load_reports(Path("data"))
        bres = bc.check(ev, cm, dm, fm, tol_min=2, max_events_per_day=40, suspect_share=0.10)
        feeders, tree, levels = bc.feeder_tables(bres)
        bc.write(bres, cls.folder / "Breakdown_Mapping_Check.xlsx")

        st = tc.Settings(window_min=2, max_events_per_day=40, min_alt_recall=0.6, max_rec_recall=0.4)
        r = tc.analyse(ev["consumer"], ev["dtr"], ev["feeder"], cm, dm, fm, st)
        write_report(r, cls.folder / "Mapping_Report.xlsx", cm, run_label="Test Run")
        bh.build(r, ev, cm, dm, fm, cls.folder / "Feeder_DTR_Consumer.xlsx", 2)

        raw = {k: len(v) for k, v in ev.items()}
        raw.update({k + "_ok": len(v) for k, v in ev.items()})
        checks = tc.data_checks(cm, dm, fm, ev, raw)

        job_data = {
            "state": "done",
            "log": ["Test finished"],
            "settings": {"tol": 2, "suspect_pct": 10.0, "max_events": 40, "min_alt_recall": 60.0, "max_rec_recall": 40.0},
            "mi_name": "Genus Consumer MI Data.xlsx",
            "mi_uploaded": True,
            "scope": "upload",
            "days": 7,
            "date_from": None,
            "date_to": None,
            "created": "2026-11-03 12:00:00",
            "files": ["test_data"],
            "folder": str(cls.folder),
            "ws": str(flask_app.DATA_DIR),
            "checks": checks,
            "breakdown": {"summary": bres["summary"], "feeders": feeders, "tree": tree, "levels": levels,
                          "status": bres["breakdowns"], "n": bres["n_breakdowns"]},
            "mapping": {"summary": r["summary"], "dtr_check": r["dtr_check"], "patterns": r["patterns"],
                        "suspects": r["suspects"], "wrong": r["wrong"]}
        }
        flask_app.write_combined(job_data, cls.folder / "All_Results.xlsx")
        flask_app.JOBS[cls.job_id] = job_data

    @classmethod
    def tearDownClass(cls):
        flask_app.JOBS.pop(cls.job_id, None)
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_results_page(self):
        res = self.client.get(f"/job/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        # Check client's numbers are in HTML
        self.assertIn(b"Outage Mapping Analysis Results", res.data)
        self.assertIn(b"663", res.data)   # Feeder -> DTR match
        self.assertIn(b"16,439", res.data) # Feeder -> Consumer match
        self.assertIn(b"11,767", res.data) # DTR -> Consumer match
        self.assertIn(b"7,042", res.data)  # Flagged consumers
        # Check download links exist
        self.assertIn(b"All_Results.xlsx", res.data)
        self.assertIn(b"Breakdown_Mapping_Check.xlsx", res.data)
        self.assertIn(b"Mapping_Report.xlsx", res.data)
        self.assertIn(b"Feeder_DTR_Consumer.xlsx", res.data)

    def test_feeder_detail_page(self):
        # Pick the first available feeder from the job
        fcode = str(flask_app.JOBS[self.job_id]["breakdown"]["feeders"]["Feeder code"].iat[0])
        res = self.client.get(f"/job/{self.job_id}/feeder/{fcode}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(fcode.encode(), res.data)
        self.assertIn(b"Back to Results", res.data)

    def test_download_all_results(self):
        for fname in ["All_Results.xlsx", "Breakdown_Mapping_Check.xlsx", "Mapping_Report.xlsx", "Feeder_DTR_Consumer.xlsx"]:
            res = self.client.get(f"/job/{self.job_id}/download/{fname}")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(len(res.data) > 1000)

if __name__ == "__main__":
    unittest.main()
