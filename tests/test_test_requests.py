"""Test dispatch, confirmation, and GPU isolation without a real GPU."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class TestRequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=ROOT)
        with patch.dict(os.environ, {"MONITOR_DB": str(Path(cls.temp.name) / "tests.db")}):
            spec = importlib.util.spec_from_file_location("test_request_monitor_server", ROOT / "server" / "monitor_server.py")
            cls.server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.server.STORE = self.server.Store(str(Path(self.temp.name) / "tests.db"))
        with self.server.STORE.connect() as conn:
            conn.execute("DELETE FROM test_requests")
            conn.execute("DELETE FROM queue_jobs")
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM runs")
        self.profile = {"id": "esc", "label": "ESCVehicle", "run_prefixes": ["/data/runs/ESCVehicle"],
                        "supports_incomplete": True}
        self.server.STORE.heartbeat_agent({
            "agent_id": "ubuntu-slot-1", "hostname": "ubuntu",
            "capabilities": {"test_v1": True}, "test_profiles": [self.profile], "gpus": [],
        })

    def run_record(self, run_id, completed=True):
        store = self.server.STORE
        store.start_run({"run_id": run_id, "name": f"Experiment {run_id}", "total_epochs": 10})
        store.progress(run_id, {"epoch": 10 if completed else 3, "phase": "epoch", "metrics": {}})
        if completed:
            store.finish(run_id, {"status": "completed", "result": {"save_dir": f"/data/runs/ESCVehicle/{run_id}"}})
        else:
            store.bind_run_output(run_id, {"save_dir": f"/data/runs/ESCVehicle/{run_id}"})
        return store.get_run(run_id)

    @staticmethod
    def idle_gpus():
        return [{"index": index, "cuda_usable": True, "telemetry_usable": True,
                 "utilization_percent": 0, "memory_used_mb": 20,
                 "memory_free_mb": 24000, "idle_for_seconds": 60}
                for index in (0, 1)]

    def test_completed_run_retains_result_and_reserves_gpu(self):
        store = self.server.STORE
        run = self.run_record("complete")
        validation_before = store.metric_events(run["id"])
        self.assertEqual(len(store.test_profiles_for_run(run)), 1)
        test = store.create_test_request(run["id"], {"profile_id": "esc", "hostname": "ubuntu"})
        self.assertTrue(test["retain_results"])
        claimed = store.claim_test_request("ubuntu-slot-1", self.idle_gpus())
        self.assertEqual(claimed["assigned_gpu"], 0)
        second = self.run_record("complete2")
        store.create_test_request(second["id"], {"profile_id": "esc", "hostname": "ubuntu"})
        claimed2 = store.claim_test_request("ubuntu-slot-1", self.idle_gpus())
        self.assertEqual(claimed2["assigned_gpu"], 1)
        with self.assertRaises(PermissionError):
            store.update_test_request(test["id"], {"agent_id": "wrong", "status": "completed"})
        result = store.update_test_request(test["id"], {
            "agent_id": "ubuntu-slot-1", "status": "completed", "metrics": {"mAP": 0.5},
            "result_path": "/data/runs/ESCVehicle/complete/test/best/metrics.yaml",
        })
        self.assertEqual(result["metrics"], {"mAP": 0.5})
        self.assertTrue(result["result_path"].endswith("metrics.yaml"))
        repeated = store.update_test_request(test["id"], {"agent_id": "ubuntu-slot-1", "status": "completed"})
        self.assertEqual(repeated["metrics"], {"mAP": 0.5})
        self.assertEqual(store.metric_events(run["id"]), validation_before)

    def test_incomplete_run_needs_two_confirmations_and_never_retains_path(self):
        store = self.server.STORE
        run = self.run_record("partial", completed=False)
        request = {"profile_id": "esc", "hostname": "ubuntu"}
        with self.assertRaises(ValueError):
            store.create_test_request(run["id"], request)
        with self.assertRaises(ValueError):
            store.create_test_request(run["id"], {**request, "confirm_incomplete": True,
                                                 "confirm_name": "wrong"})
        test = store.create_test_request(run["id"], {**request, "confirm_incomplete": True,
                                                     "confirm_name": run["name"]})
        self.assertFalse(test["retain_results"])
        with self.assertRaises(ValueError):
            store.create_test_request(run["id"], {**request, "confirm_incomplete": True,
                                                 "confirm_name": run["name"]})
        store.claim_test_request("ubuntu-slot-1", self.idle_gpus())
        result = store.update_test_request(test["id"], {
            "agent_id": "ubuntu-slot-1", "status": "completed",
            "result_path": "/data/runs/ESCVehicle/partial/test/metrics.yaml",
        })
        self.assertEqual(result["result_path"], "")

    def test_script_profile_is_not_a_web_command(self):
        run = self.run_record("safe")
        with self.assertRaises(ValueError):
            self.server.STORE.create_test_request(run["id"], {
                "profile_id": "arbitrary", "hostname": "ubuntu", "command": "rm -rf /",
            })

    def test_training_claim_excludes_test_gpu_and_test_claim_excludes_training_gpu(self):
        store = self.server.STORE
        run = self.run_record("test-reserved")
        store.create_test_request(run["id"], {"profile_id": "esc", "hostname": "ubuntu"})
        self.assertEqual(store.claim_test_request("ubuntu-slot-1", self.idle_gpus())["assigned_gpu"], 0)
        job = store.create_queue_job({"name": "next train", "command": "python train.py",
                                      "working_directory": "/data", "target_hostname": "ubuntu"})
        with store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET preflight_status='passed' WHERE id=?", (job["id"],))
        claimed = store.claim_queue_job("ubuntu-slot-1", self.idle_gpus())
        self.assertEqual(claimed["assigned_gpus"], [1])
        other = self.run_record("after-train")
        store.create_test_request(other["id"], {"profile_id": "esc", "hostname": "ubuntu"})
        self.assertIsNone(store.claim_test_request("ubuntu-slot-1", self.idle_gpus()))

if __name__ == "__main__":
    unittest.main()
