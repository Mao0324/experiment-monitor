import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SERVER = Path(__file__).resolve().parents[1] / "server" / "monitor_server.py"
AGENT = Path(__file__).resolve().parents[1] / "client" / "queue_agent.py"


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = {
            "MONITOR_DB": str(Path(self.temp.name) / "module.db"),
            "MONITOR_AI_CONFIG": str(Path(self.temp.name) / "ai.json"),
        }
        with patch.dict(os.environ, environment):
            spec = importlib.util.spec_from_file_location("monitor_reconciliation_test", SERVER)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.module = module
        self.store = module.Store(str(Path(self.temp.name) / "test.db"))
        now = module.utc_now()
        with self.store.connect() as conn:
            conn.execute("INSERT INTO runs(id,name,status,total_epochs,current_epoch,started_at,updated_at) VALUES ('run','test','stalled',3,1,?,?)", (now, now))
            conn.execute("""INSERT INTO queue_jobs(id,name,status,command_text,working_directory,run_id,created_at,updated_at,output_dir,output_results_csv)
                         VALUES ('job','test','failed','python train.py','/tmp','run',?,?, '/tmp/output','/tmp/output/results.csv')""", (now, now))
            conn.execute("INSERT INTO agents(id,hostname,updated_at) VALUES ('worker','host',?)", (now,))

    def report(self, completed):
        return self.store.reconcile_job("job", {
            "agent_id": "worker", "output_dir": "/tmp/output",
            "epochs": [{"epoch": i, "metrics": {"metrics/mAP50(B)": i / 10}} for i in range(1, 4)],
            "evidence": {"csv_max_epoch": 3, "process_alive": False,
                         "completed_log": completed, "checkpoint_exists": True},
        })

    def test_backfill_does_not_turn_post_training_failure_into_success(self):
        self.report(False)
        run = self.store.get_run("run")
        self.assertEqual(run["current_epoch"], 3)
        self.assertEqual(run["status"], "stalled")
        self.assertEqual(self.store.get_queue_job("job")["status"], "failed")
        self.assertEqual(len(self.store.metric_events("run")), 3)

    def test_verified_completion_repairs_queue_and_is_idempotent(self):
        self.report(True)
        self.report(True)
        self.assertEqual(self.store.get_run("run")["status"], "completed")
        self.assertEqual(self.store.get_queue_job("job")["status"], "completed")
        self.assertEqual(len(self.store.metric_events("run")), 3)

    def test_epoch_events_sort_backfill_and_merge_duplicate_metrics(self):
        with self.store.connect() as conn:
            for epoch, metrics in (
                (4, {"metrics/mAP50(B)": 0.84}),
                (5, {"metrics/mAP50(B)": 0.85}),
                (1, {"metrics/mAP50(B)": 0.41}),
                (2, {"metrics/mAP50(B)": 0.62}),
                (3, {"metrics/mAP50(B)": 0.73}),
                (2, {"metrics/mAP50(B)": 0.63, "val/box_loss": 1.2}),
                (5, {"val/box_loss": 0.5}),
            ):
                conn.execute(
                    "INSERT INTO metric_events(run_id,epoch,batch,phase,created_at,metrics_json) "
                    "VALUES ('run',?,NULL,'epoch',?,?)",
                    (epoch, self.module.utc_now(), json.dumps(metrics)),
                )
        events = self.store.metric_events("run")
        self.assertEqual([event["epoch"] for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(events[1]["metrics"], {"metrics/mAP50(B)": 0.63, "val/box_loss": 1.2})
        self.assertEqual(events[-1]["metrics"], {"metrics/mAP50(B)": 0.85, "val/box_loss": 0.5})

    def test_binding_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.reconcile_job("job", {"agent_id": "worker", "output_dir": "/tmp/other", "epochs": []})

    def test_stalled_requires_silent_agent_log_as_second_signal(self):
        old = datetime.fromtimestamp(time.time() - 120, timezone.utc).isoformat(timespec="seconds")
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET status='running',updated_at=? WHERE id='run'", (old,))
            conn.execute("UPDATE queue_jobs SET status='running',agent_id='worker' WHERE id='job'")
            conn.execute("UPDATE agents SET state='running',current_job_id='job',capabilities_json=? WHERE id='worker'",
                         (json.dumps({"current_log_age_seconds": 5}),))
        self.assertEqual(self.store.mark_stalled(60), [])
        with self.store.connect() as conn:
            conn.execute("UPDATE agents SET capabilities_json=? WHERE id='worker'",
                         (json.dumps({"current_log_age_seconds": 120}),))
        self.assertEqual([run["id"] for run in self.store.mark_stalled(60)], ["run"])

    def test_nvml_na_card_remains_visible_but_cannot_be_claimed(self):
        spec = importlib.util.spec_from_file_location("queue_reconciliation_test", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agent = module.Agent({"server_url": "https://example.test", "api_token": "x",
                              "allowed_roots": [self.temp.name], "log_directory": self.temp.name})
        output = "5, NVIDIA GeForce RTX 3090, [N/A], 19, 24557, 24576, 37\n"
        with patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            gpus = agent.gpu_status()
        self.assertEqual(len(gpus), 1)
        self.assertFalse(gpus[0]["telemetry_usable"])
        self.assertEqual(gpus[0]["memory_free_mb"], 24557)
        self.assertEqual(gpus[0]["idle_for_seconds"], 0)
        with patch.object(agent, "request", return_value={"job": None}) as request:
            agent.claim(gpus)
        self.assertEqual(request.call_args.args[1]["gpus"], [])


if __name__ == "__main__":
    unittest.main()
