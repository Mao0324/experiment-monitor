"""Queue-page rendering checks that do not require a listening HTTP socket."""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "monitor_server.py"


class QueueUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=ROOT)
        with patch.dict(os.environ, {"MONITOR_DB": str(Path(cls.temp.name) / "queue-ui.db")}):
            spec = importlib.util.spec_from_file_location("queue_ui_monitor_server", SERVER)
            cls.server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def job(identifier, name, status):
        return {
            "id": identifier,
            "name": name,
            "status": status,
            "priority": 0,
            "required_gpu_count": 1,
            "gpu_candidates": [],
            "preflight_status": "passed",
            "attempt_count": 0,
            "retry_limit": 0,
            "anomaly_policy": "notify",
        }

    def test_current_jobs_precede_collapsed_form_and_history_is_hidden(self):
        jobs = [
            self.job("running-1", "CPD004_running", "running"),
            self.job("completed-1", "FLIR_completed", "completed"),
        ]
        catalog = [{"id": "cpd-script", "name": "train_dronevehicle_cpd004.py"}]
        agents = [
            {"id": "slot-a", "hostname": "host-a", "catalog": catalog, "gpus": []},
            {"id": "slot-b", "hostname": "host-b", "catalog": [{"id": "flir-script", "name": "train_flir.py"}], "gpus": []},
        ]
        page = self.server.queue_html(jobs, agents)

        self.assertLess(page.index('class="queue-view-bar"'), page.index('id="new-job"'))
        self.assertIn('<details id="new-job" class="panel queue-create-disclosure">', page)
        self.assertIn('data-view="current" data-status="running" data-name="CPD004_running"', page)
        self.assertIn('data-view="history" data-status="completed" data-name="FLIR_completed" data-id="completed-1" hidden>', page)
        self.assertIn('data-host-count="2"', page)
        self.assertIn('id="scriptSearch" class="filter-input" type="search" role="combobox"', page)
        self.assertIn('found.slice(0,10)', page)
        self.assertIn('id="queueStatusFilter"', page)
        self.assertNotIn('__FORM_OPEN__', page)

        clone = self.server.queue_html(jobs, agents, {"id": "running-1", "name": "CPD004_running"})
        self.assertIn('<details id="new-job" class="panel queue-create-disclosure" open>', clone)

        if shutil.which("node"):
            for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page, re.S):
                with self.subTest(script=script[:45]):
                    result = subprocess.run(["node", "--check"], input=script, text=True, encoding="utf-8", capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
