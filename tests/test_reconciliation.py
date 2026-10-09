import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
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
            "MONITOR_API_TOKEN": "reconciliation-test-token",
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

    def test_exact_output_evidence_can_repair_a_false_recovery_pause(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET status='paused',last_error=? WHERE id='job'",
                         ("Agent 重启后发现原训练进程已经消失",))
        self.assertEqual([job["id"] for job in self.store.reconciliation_jobs("worker")], ["job"])
        self.report(True)
        self.assertEqual(self.store.get_run("run")["status"], "completed")
        self.assertEqual(self.store.get_queue_job("job")["status"], "completed")

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

    def test_completed_run_suppresses_recovery_email_and_repairs_queue(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET status='completed',current_epoch=3 WHERE id='run'")
            conn.execute("UPDATE queue_jobs SET status='running',agent_id='worker' WHERE id='job'")
        with patch.object(self.module, "STORE", self.store), patch.object(self.module.Handler, "safe_anomaly_email") as email:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.module.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps({"agent_id": "worker", "kind": "recovery_process_missing",
                                      "message": "Agent 重启后发现原训练进程已经消失"}).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/agents/jobs/job/anomaly",
                    data=payload, headers={"Authorization": "Bearer reconciliation-test-token",
                                           "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                email.assert_not_called()
            finally:
                httpd.shutdown()
                httpd.server_close()
        self.assertEqual(self.store.get_queue_job("job")["status"], "completed")
        self.assertEqual(self.store.agent_job_update("job", {"agent_id": "worker", "status": "paused",
            "error": "Agent 重启后发现原训练进程已经消失"})["status"], "completed")
        with self.store.connect() as conn:
            kinds = [row[0] for row in conn.execute("SELECT kind FROM job_events WHERE job_id='job'")]
        self.assertNotIn("anomaly:recovery_process_missing", kinds)

    def test_late_completion_repairs_recovery_pause_but_real_failure_remains(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET status='running',agent_id='worker' WHERE id='job'")
        payload = {"agent_id": "worker", "kind": "recovery_process_missing",
                   "message": "Agent 重启后发现原训练进程已经消失"}
        self.assertFalse(self.store.record_job_anomaly("job", payload).get("_suppress_anomaly_email"))
        self.store.agent_job_update("job", {"agent_id": "worker", "status": "paused",
            "error": payload["message"]})
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET status='completed',current_epoch=3 WHERE id='run'")
        self.assertTrue(self.store.repair_completed_recovery_job("job"))
        self.assertEqual(self.store.get_queue_job("job")["status"], "completed")
        with self.store.connect() as conn:
            conn.execute("UPDATE queue_jobs SET status='running',agent_id='worker' WHERE id='job'")
        self.store.agent_job_update("job", {"agent_id": "worker", "status": "failed",
            "error": "training process exited with code 1"})
        self.assertEqual(self.store.get_queue_job("job")["status"], "failed")
        self.assertTrue(self.store.repair_completed_recovery_job("job"))
        self.assertEqual(self.store.get_queue_job("job")["status"], "failed")

    def test_delayed_recovery_email_rechecks_completion(self):
        job = {"id": "job"}
        payload = {"kind": "recovery_process_missing"}
        with patch.object(self.module, "STORE", self.store), \
             patch.object(self.module.time, "sleep") as sleep, \
             patch.object(self.module, "send_anomaly_email") as email:
            self.module.Handler.safe_anomaly_email(job, payload)
            email.assert_called_once_with(job, payload)
            self.assertEqual(sleep.call_args.args, (20,))
            email.reset_mock()
            with self.store.connect() as conn:
                conn.execute("UPDATE runs SET status='completed',current_epoch=3 WHERE id='run'")
            self.module.Handler.safe_anomaly_email(job, payload)
            email.assert_not_called()

    def test_agent_recovery_checks_completed_run_before_anomaly(self):
        spec = importlib.util.spec_from_file_location("queue_recovery_test", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agent = module.Agent({"server_url": "https://example.test", "api_token": "x",
                              "allowed_roots": [self.temp.name], "log_directory": self.temp.name})
        agent.current_job = {"id": "job", "name": "test", "working_directory": self.temp.name}
        agent.current_pid = 123
        def stop_after_clear():
            agent.stop_requested = True
        with patch.object(agent, "process_matches", return_value=False), \
             patch.object(agent, "gpu_status", return_value=[]), \
             patch.object(agent, "heartbeat", return_value={"current_job": {
                 "id": "job", "status": "running", "run_verified_complete": True}}), \
             patch.object(agent, "discover_checkpoint", return_value=""), \
             patch.object(agent, "update_job", return_value={}) as update, \
             patch.object(agent, "anomaly") as anomaly, \
             patch.object(agent, "clear_runtime", side_effect=stop_after_clear), \
             patch.object(module.time, "sleep"):
            agent.run_forever()
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1], "completed")
        anomaly.assert_not_called()


if __name__ == "__main__":
    unittest.main()
