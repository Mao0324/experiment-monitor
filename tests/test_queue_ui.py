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
        self.assertIn('data-view="current" data-status="running" data-host="" data-name="CPD004_running"', page)
        self.assertIn('data-view="history" data-status="completed" data-host="" data-name="FLIR_completed" data-id="completed-1" hidden>', page)
        self.assertIn('data-host-count="2"', page)
        self.assertIn('id="scriptSearch" class="filter-input" type="search" role="combobox"', page)
        self.assertIn('found.slice(0,10)', page)
        self.assertIn('id="queueStatusFilter"', page)
        self.assertIn('id="queueHostFilter"', page)
        self.assertLess(page.index('id="targetHost"'), page.index('id="projectFolder"'))
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
        self.assertEqual(page.count(f'<option value="{root_a}" data-hostname="training-host">'), 1)
        self.assertEqual(page.count(f'<option value="{root_b}" data-hostname="training-host">'), 1)
        self.assertIn(f'<option value="b" data-directory="{root_b}" data-hostname="training-host">', page)
        self.assertIn('item.host===host.value&&item.root===folder.value', page)
        self.assertIn('option.dataset.hostname===host.value&&option.dataset.directory===chosen', page)
        self.assertIn('workdir.value=folder.value', page)
        self.assertIn('search.disabled=!(host.value&&chosen)', page)
        self.assertNotIn('__FOLDER_OPTIONS__', page)

    def test_target_host_controls_preflight_and_claim(self):
        store = self.server.Store(str(Path(self.temp.name) / "host-routing.db"))
        store.heartbeat_agent({"agent_id": "old-slot", "hostname": "old-host", "gpus": []})
        payload = {
            "name": "legacy", "command": "python train.py", "working_directory": "/project",
            "idle_seconds": 1,
        }
        legacy = store.create_queue_job(payload)
        self.assertEqual(legacy["target_hostname"], "old-host")
        # Reopening an upgraded database assigns older blank-host jobs before
        # the second machine connects.
        with store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET target_hostname='' WHERE id=?", (legacy["id"],))
        store = self.server.Store(store.path)
        self.assertEqual(store.get_queue_job(legacy["id"])["target_hostname"], "old-host")

        store.heartbeat_agent({"agent_id": "new-slot", "hostname": "new-host", "gpus": []})
        with self.assertRaisesRegex(ValueError, "target_hostname"):
            store.create_queue_job(payload)
        selected = store.create_queue_job({**payload, "name": "new-machine", "target_hostname": "new-host"})
        store.update_queue_job(selected["id"], {"target_hostname": "old-host"})
        self.assertEqual(store.get_queue_job(selected["id"])["target_hostname"], "old-host")
        store.update_queue_job(selected["id"], {"target_hostname": "new-host"})
        self.assertEqual([job["id"] for job in store.pending_preflights("new-slot")], [selected["id"]])
        self.assertEqual([job["id"] for job in store.pending_preflights("old-slot")], [legacy["id"]])
        with self.assertRaises(PermissionError):
            store.complete_preflight(legacy["id"], {"agent_id": "new-slot", "status": "passed"})
        store.complete_preflight(legacy["id"], {"agent_id": "old-slot", "status": "passed"})
        gpu = [{"index": 0, "memory_free_mb": 20000, "utilization_percent": 0,
                "idle_for_seconds": 100, "cuda_usable": True, "telemetry_usable": True}]
        self.assertIsNone(store.claim_queue_job("new-slot", gpu))
        self.assertEqual(store.claim_queue_job("old-slot", gpu)["id"], legacy["id"])

    def test_identical_project_paths_remain_host_specific(self):
        folder = "/shared/project"
        agents = [
            {"id": "old-slot", "hostname": "old-host", "catalog": [{"id": "same-script", "name": "train.py", "working_directory": folder}], "gpus": []},
            {"id": "new-slot", "hostname": "new-host", "catalog": [{"id": "same-script", "name": "train.py", "working_directory": folder}], "gpus": []},
        ]
        page = self.server.queue_html([], agents)
        self.assertIn(f'<option value="{folder}" data-hostname="old-host">', page)
        self.assertIn(f'<option value="{folder}" data-hostname="new-host">', page)
        self.assertIn('option.dataset.hostname===host.value', page)
        self.assertNotIn('__HOST_OPTIONS__', page)

    def test_agent_host_view_is_independent_and_remembers_selection(self):
        agents = [
            {"id": "old-slot", "hostname": "old-host", "catalog": [], "gpus": []},
            {"id": "old-slot-2", "hostname": "old-host", "catalog": [], "gpus": []},
            {"id": "new-slot", "hostname": "new-host", "catalog": [], "gpus": []},
        ]
        page = self.server.queue_html([], agents)
        self.assertIn('id="agentHostFilter"', page)
        self.assertEqual(page.count('<option value="old-host">old-host · 2 Slot</option>'), 1)
        self.assertEqual(page.count('<option value="new-host">new-host · 1 Slot</option>'), 1)
        self.assertIn("localStorage.getItem(agentHostKey)", page)
        self.assertIn("localStorage.setItem(agentHostKey,agentHostFilter.value)", page)
        self.assertIn("hostFilter.addEventListener('change',filterQueue)", page)
        self.assertIn("card.dataset.host!==agentHostFilter.value", page)
        self.assertNotIn('__AGENT_HOST_OPTIONS__', page)

        if shutil.which("node"):
            start = page.index("const agentHostFilter=document.getElementById")
            end = page.index("const disclosure=document.getElementById", start)
            script = """
                const assert = require('assert');
                const memory = new Map([['mcong-lab-agent-host', 'new-host']]);
                const localStorage = {
                  getItem: key => memory.get(key) || null,
                  setItem: (key, value) => memory.set(key, value),
                };
                const listeners = {};
                const select = {
                  value: '',
                  options: [{value: ''}, {value: 'old-host'}, {value: 'new-host'}],
                  addEventListener: (name, callback) => {listeners[name] = callback},
                };
                const cards = ['old-host', 'old-host', 'new-host'].map(host => ({dataset: {host}, hidden: false}));
                const count = {textContent: ''};
                const document = {
                  getElementById: id => ({agentHostFilter: select, agentHostCount: count})[id],
                  querySelectorAll: selector => {
                    assert.equal(selector, '.agent-disclosure .agent-card');
                    return cards;
                  },
                };
            """ + page[start:end] + """
                assert.equal(select.value, 'new-host');
                assert.deepEqual(cards.map(card => card.hidden), [true, true, false]);
                assert.equal(count.textContent, '1 个 Worker Slot');
                select.value = 'old-host';
                listeners.change();
                assert.equal(memory.get('mcong-lab-agent-host'), 'old-host');
                assert.deepEqual(cards.map(card => card.hidden), [false, false, true]);
                select.value = '';
                listeners.change();
                assert.deepEqual(cards.map(card => card.hidden), [false, false, false]);
            """
            result = subprocess.run(["node"], input=script, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

        empty = self.server.queue_html([], [])
        self.assertIn('id="agentHostFilter"', empty)
        self.assertIn('还没有 Agent 上线', empty)

    def test_reconciliation_stays_on_the_job_training_host(self):
        store = self.server.Store(str(Path(self.temp.name) / "host-reconciliation.db"))
        store.heartbeat_agent({"agent_id": "old-slot", "hostname": "old-host", "gpus": []})
        store.heartbeat_agent({"agent_id": "new-slot", "hostname": "new-host", "gpus": []})
        job = store.create_queue_job({
            "name": "old-experiment", "command": "python train.py",
            "working_directory": "/project", "target_hostname": "old-host",
        })
        store.start_run({"run_id": job["run_id"], "name": job["name"], "total_epochs": 10})
        output_dir = "/project/out"
        with store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET status='failed',output_dir=?,output_results_csv=? WHERE id=?",
                         (output_dir, output_dir + "/results.csv", job["id"]))
            conn.execute("UPDATE runs SET status='stalled' WHERE id=?", (job["run_id"],))

        self.assertEqual(store.reconciliation_jobs("new-slot"), [])
        with self.assertRaisesRegex(PermissionError, "another training host"):
            store.reconcile_job(job["id"], {
                "agent_id": "new-slot", "output_dir": output_dir, "epochs": [], "evidence": {},
            })
        self.assertEqual([item["id"] for item in store.reconciliation_jobs("old-slot")], [job["id"]])


if __name__ == "__main__":
    unittest.main()
