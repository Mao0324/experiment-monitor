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


    def test_project_folder_precedes_and_filters_script_catalog(self):
        root_a = "/media/project-a"
        root_b = "/data1/MCONG/TwoStream_Yolov8"
        catalog = [
            {"id": "a", "name": "train_dronevehicle.py", "working_directory": root_a},
            {"id": "b", "name": "train_escvehicle.py", "working_directory": root_b},
            {"id": "c", "name": "train_gf_obb.py", "working_directory": root_b},
        ]
        agents = [
            {"id": "slot-1", "hostname": "training-host", "catalog": catalog, "gpus": []},
            {"id": "slot-2", "hostname": "training-host", "catalog": catalog, "gpus": []},
        ]
        page = self.server.queue_html([], agents)
        self.assertLess(page.index('id="projectFolder"'), page.index('id="scriptSearch"'))
        self.assertEqual(page.count(f'<option value="{root_a}">'), 1)
        self.assertEqual(page.count(f'<option value="{root_b}">'), 1)
        self.assertIn(f'<option value="b" data-directory="{root_b}">', page)
        self.assertIn('item.root===folder.value', page)
        self.assertIn('option.dataset.directory===chosen', page)
        self.assertIn('workdir.value=folder.value', page)
        self.assertIn('search.disabled=!chosen', page)
        self.assertNotIn('__FOLDER_OPTIONS__', page)


if __name__ == "__main__":
    unittest.main()
