import http.cookiejar
import ast
import gzip
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "monitor_server.py"
MAINTENANCE = ROOT / "server" / "monitor_maintenance.py"
QUEUE_AGENT = ROOT / "client" / "queue_agent.py"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.token = "a" * 64
        cls.download_dir = Path(cls.temp.name) / "downloads"
        cls.download_dir.mkdir()
        cls.desktop_bytes = b"desktop-update-smoke"
        (cls.download_dir / "YoloMonitorPet.exe").write_bytes(cls.desktop_bytes)
        cls.agent_bytes = b"queue-agent-smoke"
        (cls.download_dir / "yolo-queue-agent.zip").write_bytes(cls.agent_bytes)
        (cls.download_dir / "latest.json").write_text(
            json.dumps(
                {
                    "version": "9.9.9",
                    "download_url": cls.base + "/downloads/YoloMonitorPet.exe",
                    "sha256": "0" * 64,
                    "size": len(cls.desktop_bytes),
                    "notes": "desktop update smoke",
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "MONITOR_BIND": "127.0.0.1",
                "MONITOR_PORT": str(cls.port),
                "MONITOR_DB": str(Path(cls.temp.name) / "test.db"),
                "MONITOR_API_TOKEN": cls.token,
                "MONITOR_ADMIN_PASSWORD": "test-password",
                "MONITOR_SESSION_SECRET": "b" * 64,
                "MONITOR_COOKIE_SECURE": "false",
                "MONITOR_PUBLIC_URL": cls.base,
                "MONITOR_DOWNLOAD_DIR": str(cls.download_dir),
            }
        )
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if cls.process.poll() is not None:
                raise RuntimeError("server exited early")
            try:
                with urlopen(cls.base + "/health", timeout=0.3) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        cls.temp.cleanup()

    def post_json(self, path, payload, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_end_to_end(self):
        with self.assertRaises(HTTPError) as caught:
            self.post_json("/api/v1/runs/start", {}, token=False)
        self.assertEqual(caught.exception.code, 401)

        status, data = self.post_json(
            "/api/v1/runs/start",
            {"run_id": "smoke-run", "name": "100 epoch smoke", "total_epochs": 100},
        )
        self.assertEqual(status, 201)
        self.assertEqual(data["run"]["status"], "running")

        status, data = self.post_json(
            "/api/v1/runs/smoke-run/progress",
            {
                "epoch": 1,
                "batch": 5,
                "total_batches": 20,
                "phase": "epoch",
                "elapsed_seconds": 12,
                "eta_seconds": 1188,
                "metrics": {"metrics/mAP50(B)": 0.42, "train/box_loss": 1.2},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["current_epoch"], 1)

        status, data = self.post_json(
            "/api/v1/runs/smoke-run/finish",
            {
                "status": "completed",
                "elapsed_seconds": 1200,
                "metrics": {"metrics/mAP50(B)": 0.67},
                "result": {"best_model": "/runs/train/weights/best.pt"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["status"], "completed")

        opener = build_opener()
        with opener.open(self.base + "/", timeout=2) as response:
            dashboard = response.read().decode()
        self.assertIn("100 epoch smoke", dashboard)
        self.assertIn("公开只读访问", dashboard)
        self.assertNotIn('href="/logout"', dashboard)
        with opener.open(self.base + "/runs/smoke-run", timeout=2) as response:
            detail = response.read().decode()
        self.assertIn("metrics/mAP50(B)", detail)
        self.assertIn("0.67", detail)
        with opener.open(self.base + "/api/v1/public/runs/smoke-run", timeout=2) as response:
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["report"]["best"]["epoch"], 1)
        with opener.open(self.base + "/api/v1/public/runs/smoke-run/report.svg", timeout=2) as response:
            report = response.read().decode()
        self.assertIn("<svg", report)
        self.assertIn("YOLO 实验战报", report)

    def test_delete_requires_admin_password_and_cascades_metrics(self):
        self.post_json(
            "/api/v1/runs/start",
            {"run_id": "delete-run", "name": "delete smoke", "total_epochs": 2},
        )
        self.post_json(
            "/api/v1/runs/delete-run/progress",
            {"epoch": 1, "phase": "epoch", "metrics": {"loss": 1.0}},
        )

        opener = build_opener()

        wrong = Request(
            self.base + "/runs/delete-run/delete",
            data=urlencode({"password": "wrong-password"}).encode(),
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            opener.open(wrong, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        with opener.open(self.base + "/runs/delete-run", timeout=2) as response:
            self.assertIn("delete smoke", response.read().decode())

        correct = Request(
            self.base + "/runs/delete-run/delete",
            data=urlencode({"password": "test-password"}).encode(),
            method="POST",
        )
        with opener.open(correct, timeout=2) as response:
            dashboard = response.read().decode()
        self.assertNotIn("delete smoke", dashboard)

        conn = sqlite3.connect(str(Path(self.temp.name) / "test.db"))
        try:
            run_count = conn.execute("SELECT COUNT(*) FROM runs WHERE id='delete-run'").fetchone()[0]
            metric_count = conn.execute(
                "SELECT COUNT(*) FROM metric_events WHERE run_id='delete-run'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(run_count, 0)
        self.assertEqual(metric_count, 0)

    def test_server_source_is_python_310_compatible(self):
        source = SERVER.read_text(encoding="utf-8")
        ast.parse(source, filename=str(SERVER), feature_version=(3, 10))
        agent_source = QUEUE_AGENT.read_text(encoding="utf-8")
        ast.parse(agent_source, filename=str(QUEUE_AGENT), feature_version=(3, 10))

    def test_queue_agent_command_rewrite_and_anomaly_detection(self):
        spec = importlib.util.spec_from_file_location("queue_agent_smoke", QUEUE_AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        job = {
            "command_text": "python train.py --batch 64 resume={resume} batch={batch}",
            "batch_size": 32,
            "resume_checkpoint": "/runs/last.pt",
        }
        arguments = module.Agent.command_arguments(job)
        self.assertEqual(arguments[3], "32")
        self.assertIn("resume=/runs/last.pt", arguments)
        self.assertIn("batch=32", arguments)
        self.assertEqual(module.Agent.detect_anomaly("CUDA out of memory"), ("cuda_oom", "CUDA out of memory"))
        kind, _ = module.Agent.detect_anomaly('[YOLO monitor anomaly] {"kind":"loss_explosion","message":"boom"}')
        self.assertEqual(kind, "loss_explosion")

    def test_agent_output_binding_prevents_cross_run_checkpoint_selection(self):
        spec = importlib.util.spec_from_file_location("queue_agent_binding", QUEUE_AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            own_dir = root / "runs" / "own-experiment"
            other_dir = root / "runs" / "other-experiment"
            own_last = own_dir / "weights" / "last.pt"
            other_last = other_dir / "weights" / "last.pt"
            own_last.parent.mkdir(parents=True)
            other_last.parent.mkdir(parents=True)
            own_last.write_bytes(b"own")
            other_last.write_bytes(b"other")
            os.utime(own_last, (1, 1))
            os.utime(other_last, (2, 2))
            agent = module.Agent({
                "server_url": "https://example.invalid", "api_token": "x", "agent_id": "binding-test",
                "allowed_roots": [str(root)], "training_python": sys.executable,
                "log_directory": str(root / "state"),
            })
            job = {
                "id": "binding-job", "name": "own-experiment", "working_directory": str(root),
                "output_dir": str(own_dir), "output_last_checkpoint": str(own_last),
            }
            self.assertEqual(agent.discover_checkpoint(job), str(own_last.resolve()))
            relative_job = {
                "id": "relative-binding-job", "name": "own-experiment", "working_directory": str(root),
                "output_dir": str(own_dir.relative_to(root)),
            }
            self.assertEqual(agent.discover_checkpoint(relative_job), str(own_last.resolve()))
            own_last.unlink()
            self.assertEqual(agent.discover_checkpoint(job), "")
            legacy_job = {"id": "legacy-job", "name": "legacy", "working_directory": str(root)}
            self.assertEqual(agent.discover_checkpoint(legacy_job), str(other_last.resolve()))
            parsed = agent.output_binding_from_log(
                legacy_job,
                "initializing\nLogging results to \x1b[1mruns/own-experiment\x1b[0m\nStarting training\n",
            )
            self.assertEqual(parsed["save_dir"], str(own_dir.resolve()))
            self.assertEqual(parsed["last_model"], str(own_last.resolve()))
            with patch.object(module.os, "kill", return_value=None), patch.object(
                module.Path, "read_text", return_value="3143259 (python) Z 1 2 3"
            ):
                self.assertFalse(agent.process_alive(3143259))

    def test_agent_worker_slots_keep_legacy_slot_one_and_isolate_other_states(self):
        spec = importlib.util.spec_from_file_location("queue_agent_slots", QUEUE_AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "agent-state.json"
            configs = module.worker_configs({
                "agent_id": "training-host",
                "worker_slots": 4,
                "state_file": str(state),
            })
            self.assertEqual([item["agent_id"] for item in configs], [
                "training-host",
                "training-host-slot-2",
                "training-host-slot-3",
                "training-host-slot-4",
            ])
            self.assertEqual(Path(configs[0]["state_file"]), state)
            self.assertEqual(
                [Path(item["state_file"]).name for item in configs[1:]],
                ["agent-state.slot-2.json", "agent-state.slot-3.json", "agent-state.slot-4.json"],
            )

    def test_agent_script_catalog_autofill_metadata(self):
        spec = importlib.util.spec_from_file_location("queue_agent_catalog", QUEUE_AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            yaml_dir = root / "yaml"
            yaml_dir.mkdir()
            (yaml_dir / "father.yaml").write_text("# baseline\nnc: 5\n", encoding="utf-8")
            (yaml_dir / "child.yaml").write_text(
                "# 来源与唯一改动：\n"
                "# 本文件以 father.yaml 为父实验，\n"
                "# 仅将 attention 用 FP32 计算，\n"
                "# 然后转回原 dtype。\n"
                "# 这行不是修改摘要。\n"
                "nc: 5\n",
                encoding="utf-8",
            )
            script = root / "train_dronevehicle_ablation.py"
            script.write_text(
                '# 对应模型 YAML：yaml/child.yaml\nEXPERIMENT_NAME = "catalog-ablation"\nCHECKPOINT = str(repo_path("weights", "base.pt"))\nQUEUE = resolve_queue_runtime(CHECKPOINT, default_device="0,1")\ntracked_train(model, data=str(repo_path("data", "drone.yaml")), batch=64)\n',
                encoding="utf-8",
            )
            agent = module.Agent({
                "server_url": "https://example.invalid", "api_token": "x", "agent_id": "catalog-test",
                "allowed_roots": [str(root)], "training_python": sys.executable,
                "log_directory": str(root / "state"),
            })
            catalog = agent.scan_catalog()
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["task_name"], "catalog-ablation")
            self.assertEqual(catalog[0]["default_batch"], 64)
            self.assertEqual(catalog[0]["default_gpu_count"], 2)
            self.assertEqual(Path(catalog[0]["checkpoint"]).name, "base.pt")
            self.assertEqual(catalog[0]["model_yaml_name"], "child.yaml")
            self.assertEqual(catalog[0]["father_yaml_name"], "father.yaml")
            self.assertEqual(catalog[0]["lineage_relation"], "father")
            self.assertEqual(catalog[0]["lineage_confidence"], "exact")
            self.assertEqual(catalog[0]["change_summary"], "仅将 attention 用 FP32 计算，\n然后转回原 dtype。")

    def test_safe_pause_callback_stops_at_epoch_boundary(self):
        sys.path.insert(0, str(ROOT / "client"))
        from yolo_monitor import YoloExperimentMonitor, install_queue_control_callback

        class Owner:
            def __init__(self):
                self.callbacks = {}
            def add_callback(self, name, callback):
                self.callbacks[name] = callback

        with tempfile.TemporaryDirectory() as temp_dir:
            control = Path(temp_dir) / "control.json"
            control.write_text(json.dumps({"pause_after_epoch": True}), encoding="utf-8")
            owner = Owner()
            with patch.dict(os.environ, {"YOLO_QUEUE_CONTROL_FILE": str(control), "LOCAL_RANK": "0"}):
                install_queue_control_callback(owner)
                trainer = SimpleNamespace(stop=False, last=Path(temp_dir) / "last.pt")
                owner.callbacks["on_train_epoch_end"](trainer)
            self.assertTrue(trainer.stop)
            self.assertTrue(trainer._yolo_queue_paused)
            output = YoloExperimentMonitor._output_binding(SimpleNamespace(save_dir=Path(temp_dir) / "run"))
            self.assertEqual(output["last_model"], str(Path(temp_dir) / "run" / "weights" / "last.pt"))
            self.assertEqual(output["results_csv"], str(Path(temp_dir) / "run" / "results.csv"))

    def test_pause_resume_and_sweep_queue_workflow(self):
        base_payload = {
            "name": "pause-resume-smoke", "command": "python train.py", "working_directory": "/tmp/project",
            "required_gpu_count": 1, "min_free_memory_mb": 1000, "max_gpu_utilization": 10,
            "idle_seconds": 0, "anomaly_policy": "notify", "password": "test-password",
        }
        create = Request(self.base + "/api/v1/web/queue", data=json.dumps(base_payload).encode(), headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"}, method="POST")
        with urlopen(create, timeout=2) as response:
            created = json.loads(response.read())["job"]
        gpus = [{"index": 0, "utilization_percent": 0, "memory_free_mb": 20000, "idle_for_seconds": 60}]
        self.post_json("/api/v1/agents/heartbeat", {"agent_id": "pause-agent", "hostname": "trainer", "state": "idle", "gpus": gpus})
        self.post_json(f"/api/v1/agents/jobs/{created['id']}/preflight", {"agent_id": "pause-agent", "status": "passed", "message": "ok"})
        _, claimed = self.post_json("/api/v1/agents/claim", {"agent_id": "pause-agent", "gpus": gpus})
        job = claimed["job"]
        self.post_json(f"/api/v1/agents/jobs/{job['id']}/status", {"agent_id": "pause-agent", "status": "running", "runtime_pid": 1234})
        output_dir = "/tmp/project/runs"
        output_binding = {
            "save_dir": output_dir,
            "last_model": output_dir + "/weights/last.pt",
            "best_model": output_dir + "/weights/best.pt",
            "results_csv": output_dir + "/results.csv",
        }
        self.post_json("/api/v1/runs/start", {"run_id": job["run_id"], "name": job["name"], "total_epochs": 100})
        _, bound_progress = self.post_json(
            f"/api/v1/runs/{job['run_id']}/progress",
            {"epoch": 1, "phase": "binding", "output_binding": output_binding},
        )
        self.assertEqual(bound_progress["run"]["result"]["save_dir"], output_dir)
        _, agent_binding = self.post_json(
            f"/api/v1/agents/jobs/{job['id']}/binding",
            {"agent_id": "pause-agent", "output_binding": output_binding},
        )
        self.assertEqual(agent_binding["job"]["output_dir"], output_dir)
        self.assertEqual(agent_binding["job"]["output_last_checkpoint"], output_binding["last_model"])
        with urlopen(self.base + "/queue", timeout=2) as response:
            bound_page = response.read().decode("utf-8")
        self.assertIn("输出已绑定 · runs", bound_page)

        def web_action(action):
            request = Request(self.base + f"/api/v1/web/queue/{job['id']}/action", data=json.dumps({"password": "test-password", "action": action}).encode(), headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"}, method="POST")
            with urlopen(request, timeout=2) as response:
                return json.loads(response.read())["job"]

        paused_requested = web_action("pause")
        self.assertTrue(paused_requested["pause_requested"])
        _, heartbeat = self.post_json("/api/v1/agents/heartbeat", {"agent_id": "pause-agent", "hostname": "trainer", "state": "running", "current_job_id": job["id"], "gpus": gpus})
        self.assertTrue(heartbeat["current_job"]["pause_requested"])
        checkpoint = output_binding["last_model"]
        _, paused = self.post_json(f"/api/v1/agents/jobs/{job['id']}/status", {"agent_id": "pause-agent", "status": "paused", "resume_checkpoint": checkpoint, "last_checkpoint": checkpoint})
        self.assertEqual(paused["job"]["status"], "paused")
        resumed = web_action("resume")
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["preflight_status"], "pending")

        # A stopped run gets two explicit choices: resume from last.pt or start over.
        self.post_json(f"/api/v1/agents/jobs/{job['id']}/preflight", {"agent_id": "pause-agent", "status": "passed", "message": "resume ok"})
        _, claimed_again = self.post_json("/api/v1/agents/claim", {"agent_id": "pause-agent", "gpus": gpus})
        resumed_job = claimed_again["job"]
        self.assertEqual(resumed_job["id"], job["id"])
        self.assertEqual(resumed_job["resume_checkpoint"], checkpoint)
        self.post_json(f"/api/v1/agents/jobs/{job['id']}/status", {"agent_id": "pause-agent", "status": "failed", "last_checkpoint": checkpoint})
        old_run_id = resumed_job["run_id"]
        with urlopen(self.base + "/queue", timeout=2) as response:
            stopped_page = response.read().decode("utf-8")
        self.assertIn('data-action="requeue_resume"', stopped_page)
        self.assertIn("从 last.pt 续训", stopped_page)
        self.assertIn("从头重新排队", stopped_page)
        resumed_after_failure = web_action("requeue_resume")
        self.assertEqual(resumed_after_failure["status"], "queued")
        self.assertNotEqual(resumed_after_failure["run_id"], old_run_id)
        self.assertEqual(resumed_after_failure["preflight_status"], "pending")
        conn = sqlite3.connect(Path(self.temp.name) / "test.db")
        try:
            stored_resume, stored_output = conn.execute(
                "SELECT resume_checkpoint,output_dir FROM queue_jobs WHERE id=?", (job["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(stored_resume, checkpoint)
        self.assertEqual(stored_output, output_dir)

        # The original requeue action remains a clean start and clears resume state.
        cancelled = web_action("cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        restarted = web_action("requeue")
        self.assertEqual(restarted["status"], "queued")
        conn = sqlite3.connect(Path(self.temp.name) / "test.db")
        try:
            stored_resume, stored_output = conn.execute(
                "SELECT resume_checkpoint,output_dir FROM queue_jobs WHERE id=?", (job["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(stored_resume, "")
        self.assertEqual(stored_output, "")

        sweep = {**base_payload, "name": "sweep-base", "sweep_jobs": [{"name": "sweep-b16", "batch_size": 16}, {"name": "sweep-b32", "batch_size": 32}]}
        request = Request(self.base + "/api/v1/web/queue", data=json.dumps(sweep).encode(), headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"}, method="POST")
        with urlopen(request, timeout=2) as response:
            created_sweep = json.loads(response.read())
        self.assertEqual(len(created_sweep["jobs"]), 2)
        self.assertTrue(created_sweep["sweep_group_id"])

    def test_queue_claim_reserves_gpus_across_slots_on_the_same_host(self):
        payload = {
            "name": "gpu-reservation-a", "command": "python train.py", "working_directory": "/tmp/project",
            "priority": 1000, "required_gpu_count": 2, "gpu_candidates": [10, 11],
            "min_free_memory_mb": 1000, "max_gpu_utilization": 10, "idle_seconds": 0,
            "anomaly_policy": "notify", "password": "test-password",
        }

        def create_job(name):
            request = Request(
                self.base + "/api/v1/web/queue",
                data=json.dumps({**payload, "name": name}).encode(),
                headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                return json.loads(response.read())["job"]

        first = create_job("gpu-reservation-a")
        second = create_job("gpu-reservation-b")
        gpus = [
            {"index": 10, "utilization_percent": 0, "memory_free_mb": 20000, "idle_for_seconds": 60},
            {"index": 11, "utilization_percent": 0, "memory_free_mb": 20000, "idle_for_seconds": 60},
        ]
        for agent_id in ("reservation-host", "reservation-host-slot-2"):
            self.post_json(
                "/api/v1/agents/heartbeat",
                {"agent_id": agent_id, "hostname": "same-physical-host", "state": "idle", "gpus": gpus},
            )
        self.post_json(
            f"/api/v1/agents/jobs/{first['id']}/preflight",
            {"agent_id": "reservation-host", "status": "passed", "message": "ok"},
        )
        self.post_json(
            f"/api/v1/agents/jobs/{second['id']}/preflight",
            {"agent_id": "reservation-host-slot-2", "status": "passed", "message": "ok"},
        )
        _, claimed_first = self.post_json(
            "/api/v1/agents/claim", {"agent_id": "reservation-host", "gpus": gpus}
        )
        _, claimed_second = self.post_json(
            "/api/v1/agents/claim", {"agent_id": "reservation-host-slot-2", "gpus": gpus}
        )
        self.assertEqual(claimed_first["job"]["assigned_gpus"], [10, 11])
        self.assertIsNone(claimed_second["job"])
        with urlopen(self.base + "/api/v1/public/queue", timeout=2) as response:
            queue_snapshot = json.loads(response.read())
        waiting = next(job for job in queue_snapshot["jobs"] if job["id"] == second["id"])
        self.assertIn("预留", " ".join(waiting["queue_reasons"]))

        self.post_json(
            f"/api/v1/agents/jobs/{first['id']}/status",
            {"agent_id": "reservation-host", "status": "failed", "error": "test cleanup"},
        )
        cancel = Request(
            self.base + f"/api/v1/web/queue/{second['id']}/action",
            data=json.dumps({"password": "test-password", "action": "cancel"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(cancel, timeout=2):
            pass

    def test_queue_catalog_deduplicates_worker_slots_and_hides_sweep_fields(self):
        catalog = [{
            "id": "slot-dedup-script",
            "name": "train_dedup.py",
            "path": "/tmp/project/train_dedup.py",
            "working_directory": "/tmp/project",
            "command": "python /tmp/project/train_dedup.py",
        }]
        for agent_id in ("catalog-host", "catalog-host-slot-2"):
            self.post_json(
                "/api/v1/agents/heartbeat",
                {
                    "agent_id": agent_id,
                    "hostname": "catalog-physical-host",
                    "state": "idle",
                    "gpus": [],
                    "catalog": catalog,
                },
            )
        with urlopen(self.base + "/queue", timeout=2) as response:
            page_text = response.read().decode("utf-8")
        # One option in the normal selector and one in the batch selector,
        # rather than one copy per worker slot in both selectors.
        self.assertEqual(page_text.count('<option value="slot-dedup-script">'), 2)
        self.assertIn('id="queueMode"', page_text)
        self.assertIn('id="sweepFields" class="sweep-box form-wide" hidden', page_text)
        self.assertNotIn('id="sweepEnabled"', page_text)
        self.assertIn('<details class="panel agent-disclosure">', page_text)
        self.assertIn('<summary class="agent-summary">', page_text)
        self.assertNotIn('<details class="panel agent-disclosure" open', page_text)
        self.assertRegex(page_text, r'在线主机 <strong>\d+</strong> · Worker Slot <strong>\d+</strong>')

    def test_only_cancelled_queue_jobs_can_be_deleted(self):
        create = Request(
            self.base + "/api/v1/web/queue",
            data=json.dumps({
                "name": "delete-cancelled-queue-smoke",
                "command": "python train.py",
                "working_directory": "/tmp/project",
                "password": "test-password",
            }).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(create, timeout=2) as response:
            job = json.loads(response.read())["job"]
        self.post_json(
            "/api/v1/runs/start",
            {"run_id": job["run_id"], "name": job["name"], "total_epochs": 1},
        )

        def delete_request(password):
            return Request(
                self.base + f"/api/v1/web/queue/{job['id']}/delete",
                data=json.dumps({"password": password}).encode(),
                headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
                method="POST",
            )

        with self.assertRaises(HTTPError) as active_delete:
            urlopen(delete_request("test-password"), timeout=2)
        self.assertEqual(active_delete.exception.code, 409)

        cancel = Request(
            self.base + f"/api/v1/web/queue/{job['id']}/action",
            data=json.dumps({"password": "test-password", "action": "cancel"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(cancel, timeout=2):
            pass
        with urlopen(self.base + "/queue", timeout=2) as response:
            queue_page = response.read().decode("utf-8")
        self.assertIn(f'data-id="{job["id"]}" data-action="delete"', queue_page)

        with self.assertRaises(HTTPError) as wrong_password:
            urlopen(delete_request("wrong-password"), timeout=2)
        self.assertEqual(wrong_password.exception.code, 403)
        with urlopen(delete_request("test-password"), timeout=2) as response:
            deleted = json.loads(response.read())
        self.assertTrue(deleted["deleted"])

        with urlopen(self.base + "/api/v1/public/queue", timeout=2) as response:
            queue_snapshot = json.loads(response.read())
        self.assertNotIn(job["id"], {item["id"] for item in queue_snapshot["jobs"]})
        with urlopen(self.base + f"/api/v1/public/runs/{job['run_id']}", timeout=2) as response:
            self.assertEqual(json.loads(response.read())["run"]["id"], job["run_id"])
        conn = sqlite3.connect(Path(self.temp.name) / "test.db")
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events WHERE job_id=?", (job["id"],)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_live_log_host_status_and_multi_run_comparison(self):
        for run_id, name, value in (
            ("compare-a", "comparison alpha", 0.41),
            ("compare-b", "comparison beta", 0.52),
        ):
            self.post_json(
                "/api/v1/runs/start",
                {"run_id": run_id, "name": name, "total_epochs": 3},
            )
            for epoch in (1, 2):
                self.post_json(
                    f"/api/v1/runs/{run_id}/progress",
                    {
                        "epoch": epoch,
                        "phase": "epoch",
                        "metrics": {"metrics/mAP50-95(B)": value + epoch / 100},
                        "log_tail": f"{name} epoch {epoch}/3",
                        "host_status": {
                            "hostname": "gpu-lab",
                            "sampled_at": "2026-07-18 12:00:00 +0800",
                            "load": {"per_cpu_percent": 35.2},
                            "memory": {"used_mb": 2048, "total_mb": 4096, "used_percent": 50},
                            "gpus": [
                                {
                                    "index": 4,
                                    "name": "Test GPU",
                                    "utilization_percent": 88,
                                    "memory_used_mb": 1024,
                                    "memory_total_mb": 2048,
                                    "temperature_c": 67,
                                }
                            ],
                        },
                    },
                )

        jar = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        opener.open(
            Request(
                self.base + "/login",
                data=urlencode({"password": "test-password"}).encode(),
                method="POST",
            ),
            timeout=2,
        ).close()
        with opener.open(self.base + "/runs/compare-a", timeout=2) as response:
            detail = response.read().decode()
        self.assertIn("实时日志尾部", detail)
        self.assertIn("comparison alpha epoch 2/3", detail)
        self.assertIn("GPU / 主机状态", detail)
        self.assertIn("Test GPU", detail)
        self.assertIn("metricSelect", detail)
        self.assertIn("纵轴为真实数值", detail)
        self.assertNotIn("slice(0,6)", detail)

        with opener.open(
            self.base + "/compare?run=compare-a&run=compare-b", timeout=2
        ) as response:
            comparison = response.read().decode()
        self.assertIn("多实验指标对比", comparison)
        self.assertIn("comparison alpha", comparison)
        self.assertIn("comparison beta", comparison)
        self.assertIn("使用同一纵轴刻度", comparison)

    def test_stalled_transition_is_one_shot_and_progress_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "MONITOR_DB": str(Path(temp_dir) / "store.db"),
                "MONITOR_API_TOKEN": "a" * 64,
                "MONITOR_ADMIN_PASSWORD": "password",
                "MONITOR_SESSION_SECRET": "b" * 64,
            }
            with patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("monitor_server_store_test", SERVER)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            store = module.Store(str(Path(temp_dir) / "isolated.db"))
            store.start_run({"run_id": "stale-run", "name": "stale", "total_epochs": 3})
            with store.connect() as conn:
                conn.execute(
                    "UPDATE runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id='stale-run'"
                )
            first = store.mark_stalled(1)
            second = store.mark_stalled(1)
            self.assertEqual([run["id"] for run in first], ["stale-run"])
            self.assertEqual(second, [])
            self.assertEqual(store.get_run("stale-run")["status"], "stalled")
            recovered = store.progress("stale-run", {"epoch": 1, "phase": "batch"})
            self.assertEqual(recovered["status"], "running")

    def test_metadata_filters_best_epoch_and_sse(self):
        self.post_json(
            "/api/v1/runs/start",
            {
                "run_id": "metadata-run",
                "name": "PaperLAF searchable experiment",
                "total_epochs": 3,
                "parameters": {
                    "reproducibility": {
                        "python_version": "3.10.0",
                        "git": {"commit": "abc123", "dirty": False},
                    }
                },
            },
        )
        for epoch, value in ((1, 0.4), (2, 0.7), (3, 0.6)):
            self.post_json(
                "/api/v1/runs/metadata-run/progress",
                {
                    "epoch": epoch,
                    "phase": "epoch",
                    "metrics": {"metrics/mAP50-95(B)": value},
                },
            )

        opener = build_opener()
        with opener.open(self.base + "/", timeout=2) as response:
            public_dashboard = response.read().decode()
        self.assertIn("公开只读访问", public_dashboard)
        with opener.open(self.base + "/runs/metadata-run", timeout=2) as response:
            public_detail = response.read().decode()
        self.assertIn("metadataPassword", public_detail)
        with opener.open(self.base + "/api/v1/public/runs", timeout=2) as response:
            public_runs = json.loads(response.read())
        self.assertIn("metadata-run", [run["id"] for run in public_runs["runs"]])
        with opener.open(self.base + "/api/v1/public/runs/metadata-run", timeout=2) as response:
            public_run = json.loads(response.read())
        self.assertEqual(public_run["run"]["name"], "PaperLAF searchable experiment")
        self.assertEqual(public_run["best"]["epoch"], 2)
        self.assertEqual(len(public_run["events"]), 3)

        missing_password = Request(
            self.base + "/api/v1/web/runs/metadata-run/metadata",
            data=json.dumps({"favorite": True}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            opener.open(missing_password, timeout=2)
        self.assertEqual(caught.exception.code, 403)

        wrong_password = Request(
            self.base + "/api/v1/web/runs/metadata-run/metadata",
            data=json.dumps({"favorite": True, "password": "wrong-password"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            opener.open(wrong_password, timeout=2)
        self.assertEqual(caught.exception.code, 403)

        metadata = Request(
            self.base + "/api/v1/web/runs/metadata-run/metadata",
            data=json.dumps(
                {
                    "group_name": "DroneVehicle",
                    "tags": "PaperLAF, FP32Safe",
                    "favorite": True,
                    "password": "test-password",
                }
            ).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with opener.open(metadata, timeout=2) as response:
            saved = json.loads(response.read())["run"]
        self.assertEqual(saved["group_name"], "DroneVehicle")
        self.assertEqual(saved["tags"], ["PaperLAF", "FP32Safe"])
        self.assertTrue(saved["favorite"])

        with opener.open(self.base + "/", timeout=2) as response:
            dashboard = response.read().decode()
        for control in ("nameFilter", "statusFilter", "groupFilter", "tagFilter", "favoriteFilter"):
            self.assertIn(control, dashboard)
        self.assertIn("EventSource('/events/dashboard')", dashboard)

        with opener.open(self.base + "/runs/metadata-run", timeout=2) as response:
            detail = response.read().decode()
        self.assertIn("最佳 Epoch", detail)
        self.assertIn("Best Epoch", detail)
        self.assertIn("abc123", detail)
        self.assertIn("metadataForm", detail)
        self.assertIn("metadataPassword", detail)
        self.assertIn("EventSource('/events/runs/'", detail)

        with opener.open(self.base + "/events/runs/metadata-run", timeout=4) as response:
            line = response.readline().decode()
        self.assertTrue(line.startswith("data: "))
        event = json.loads(line[len("data: ") :])
        self.assertEqual(event["best"]["epoch"], 2)
        self.assertEqual(event["run"]["tags"], ["PaperLAF", "FP32Safe"])

    def test_daily_backup_is_compressed_and_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.db"
            backup_dir = Path(temp_dir) / "backups"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('backup-ok')")
            connection.commit()
            connection.close()
            env = os.environ.copy()
            env.update(
                {
                    "MONITOR_DB": str(db_path),
                    "MONITOR_BACKUP_DIR": str(backup_dir),
                    "MONITOR_BACKUP_KEEP_DAYS": "14",
                    "MONITOR_BACKUP_KEEP_COUNT": "14",
                    "MONITOR_RUN_RETENTION_DAYS": "0",
                }
            )
            subprocess.run([sys.executable, str(MAINTENANCE)], env=env, check=True, capture_output=True)
            backups = list(backup_dir.glob("monitor-*.db.gz"))
            self.assertEqual(len(backups), 1)
            restored = Path(temp_dir) / "restored.db"
            with gzip.open(backups[0], "rb") as source, restored.open("wb") as target:
                target.write(source.read())
            restored_db = sqlite3.connect(restored)
            try:
                value = restored_db.execute("SELECT value FROM sample").fetchone()[0]
            finally:
                restored_db.close()
            self.assertEqual(value, "backup-ok")

    def test_ultralytics_style_callback_and_early_failure(self):
        sys.path.insert(0, str(ROOT / "client"))
        from yolo_monitor import YoloExperimentMonitor

        class FakeModel:
            def __init__(self, fail_early=False):
                self.callbacks = {}
                self.fail_early = fail_early

            def add_callback(self, name, callback):
                self.callbacks[name] = callback

            def train(self, **kwargs):
                if self.fail_early:
                    raise RuntimeError("bad dataset")
                trainer = SimpleNamespace(
                    args=SimpleNamespace(name=kwargs.get("name", "fake"), epochs=2),
                    epochs=2,
                    epoch=0,
                    batch_i=0,
                    train_loader=[1, 2],
                    metrics={"metrics/mAP50(B)": 0.5},
                    lr={"lr/pg0": 0.01},
                    tloss=[1.2, 0.8],
                    loss_names=["box_loss", "cls_loss"],
                    save_dir=Path("runs/fake"),
                    best=Path("runs/fake/weights/best.pt"),
                    last=Path("runs/fake/weights/last.pt"),
                    best_fitness=0.5,
                )
                self.callbacks["on_train_start"](trainer)
                self.callbacks["on_train_batch_end"](trainer)
                self.callbacks["on_fit_epoch_end"](trainer)
                self.callbacks["on_train_end"](trainer)
                return "training-result"

        monitor = YoloExperimentMonitor(
            server_url=self.base,
            api_token=self.token,
            experiment_name="callback smoke",
            request_timeout=1,
        )
        result = monitor.train(FakeModel(), epochs=2)
        self.assertEqual(result, "training-result")

        failed_monitor = YoloExperimentMonitor(
            server_url=self.base,
            api_token=self.token,
            experiment_name="early failure smoke",
            request_timeout=1,
        )
        with self.assertRaisesRegex(RuntimeError, "bad dataset"):
            failed_monitor.train(FakeModel(fail_early=True), epochs=2)

        conn = sqlite3.connect(str(Path(self.temp.name) / "test.db"))
        try:
            completed = conn.execute(
                "SELECT status, parameters_json FROM runs WHERE id=?", (monitor.run_id,)
            ).fetchone()
            failed = conn.execute(
                "SELECT status, error_message FROM runs WHERE id=?", (failed_monitor.run_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(completed[0], "completed")
        reproducibility = json.loads(completed[1])["reproducibility"]
        self.assertIn("python_version", reproducibility)
        self.assertIn("platform", reproducibility)
        self.assertEqual(failed[0], "failed")
        self.assertIn("bad dataset", failed[1])

    def test_ddp_children_share_run_and_only_rank_zero_attaches(self):
        sys.path.insert(0, str(ROOT / "client"))
        from yolo_monitor import RemoteMonitorTrainerMixin, YoloExperimentMonitor

        class FakeBaseTrainer:
            def __init__(self):
                self.callbacks = {}
                self.args = SimpleNamespace(name="ddp smoke", epochs=2)
                self.epochs = 2
                self.epoch = 0
                self.batch_i = 0
                self.train_loader = [1, 2]
                self.metrics = {"metrics/mAP50(B)": 0.55}
                self.lr = {"lr/pg0": 0.01}
                self.tloss = [1.0, 0.7]
                self.loss_names = ["box_loss", "cls_loss"]
                self.save_dir = Path("runs/ddp")
                self.best = Path("runs/ddp/weights/best.pt")
                self.last = Path("runs/ddp/weights/last.pt")
                self.best_fitness = 0.55

            def add_callback(self, name, callback):
                self.callbacks.setdefault(name, []).append(callback)

        class FakeMonitoredTrainer(RemoteMonitorTrainerMixin, FakeBaseTrainer):
            pass

        env = {
            "YOLO_MONITOR_URL": self.base,
            "YOLO_MONITOR_TOKEN": self.token,
            "YOLO_EXPERIMENT_NAME": "ddp smoke",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("RANK", None)
            os.environ.pop("LOCAL_RANK", None)
            parent = YoloExperimentMonitor(experiment_name="ddp smoke", request_timeout=1)
            parent.start(total_epochs=2, parameters={"device": "4,5"})

            with patch.dict(os.environ, {"RANK": "1", "LOCAL_RANK": "1"}, clear=False):
                rank_one = FakeMonitoredTrainer()
                self.assertEqual(rank_one.callbacks, {})
                self.assertIsNone(rank_one._remote_experiment_monitor)

            with patch.dict(os.environ, {"RANK": "0", "LOCAL_RANK": "0"}, clear=False):
                rank_zero = FakeMonitoredTrainer()
                self.assertEqual(rank_zero._remote_experiment_monitor.run_id, parent.run_id)
                self.assertIn("on_train_start", rank_zero.callbacks)
                for event in ("on_train_start", "on_train_batch_end", "on_fit_epoch_end", "on_train_end"):
                    for callback in rank_zero.callbacks[event]:
                        callback(rank_zero)

        conn = sqlite3.connect(str(Path(self.temp.name) / "test.db"))
        try:
            rows = conn.execute(
                "SELECT id, status, current_epoch FROM runs WHERE id=?", (parent.run_id,)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "completed")
        self.assertEqual(rows[0][2], 1)

    def test_desktop_update_manifest_and_restricted_download(self):
        with urlopen(self.base + "/api/v1/public/desktop/latest", timeout=2) as response:
            manifest = json.loads(response.read())
        self.assertEqual(manifest["version"], "9.9.9")
        self.assertEqual(manifest["size"], len(self.desktop_bytes))

        with urlopen(self.base + "/downloads/YoloMonitorPet.exe", timeout=2) as response:
            executable = response.read()
            disposition = response.headers.get("Content-Disposition")
        self.assertEqual(executable, self.desktop_bytes)
        self.assertIn("YoloMonitorPet.exe", disposition)

        with urlopen(self.base + "/downloads/yolo-queue-agent.zip", timeout=2) as response:
            self.assertEqual(response.read(), self.agent_bytes)

        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/downloads/latest.json", timeout=2)
        self.assertEqual(caught.exception.code, 404)

    def test_queue_agent_gpu_gating_notes_and_anomaly_protocol(self):
        payload = {
            "name": "queued two gpu smoke",
            "command": "python train.py --batch {batch}",
            "working_directory": "/tmp/project",
            "priority": 9,
            "required_gpu_count": 2,
            "gpu_candidates": "4,5",
            "min_free_memory_mb": 20000,
            "max_gpu_utilization": 10,
            "idle_seconds": 300,
            "retry_limit": 2,
            "anomaly_policy": "retry_lower_batch",
            "batch_size": 64,
            "min_batch_size": 8,
            "baseline_run_id": "smoke-run",
            "password": "test-password",
        }
        missing_batch = dict(payload)
        missing_batch["name"] = "lower batch requires initial batch"
        missing_batch.pop("batch_size")
        invalid_request = Request(
            self.base + "/api/v1/web/queue",
            data=json.dumps(missing_batch).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(invalid_request, timeout=2)
        self.assertEqual(caught.exception.code, 400)

        request = Request(
            self.base + "/api/v1/web/queue",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            created = json.loads(response.read())["job"]
        self.assertEqual(created["required_gpu_count"], 2)
        self.assertNotIn("command_text", created)

        with urlopen(self.base + "/queue", timeout=2) as response:
            queue_page = response.read().decode("utf-8")
        self.assertIn(f'/queue/jobs/{created["id"]}', queue_page)
        self.assertIn("查看并修改任务配置", queue_page)
        self.assertIn('id="queuePolicy"', queue_page)
        self.assertIn("batch.required=required", queue_page)

        detail_url = self.base + f'/queue/jobs/{created["id"]}'
        with urlopen(detail_url, timeout=2) as response:
            detail_page = response.read().decode("utf-8")
        self.assertIn('id="queueEditForm"', detail_page)
        self.assertIn("python train.py --batch {batch}", detail_page)
        self.assertIn("/tmp/project", detail_page)

        wrong_update = Request(
            self.base + f'/api/v1/web/queue/{created["id"]}/update',
            data=json.dumps({"name": "must not change", "password": "wrong"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(wrong_update, timeout=2)
        self.assertEqual(caught.exception.code, 403)

        update_request = Request(
            self.base + f'/api/v1/web/queue/{created["id"]}/update',
            data=json.dumps({"name": "edited before claim", "priority": 10, "password": "test-password"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(update_request, timeout=2) as response:
            edited = json.loads(response.read())["job"]
        self.assertEqual(edited["name"], "edited before claim")
        self.assertEqual(edited["priority"], 10)
        self.assertNotIn("command_text", edited)
        with urlopen(detail_url, timeout=2) as response:
            edited_page = response.read().decode("utf-8")
        self.assertIn("edited before claim", edited_page)
        self.assertIn('id="editQueueBatch"', edited_page)

        invalid_update = Request(
            self.base + f'/api/v1/web/queue/{created["id"]}/update',
            data=json.dumps({"anomaly_policy": "retry_lower_batch", "batch_size": "", "password": "test-password"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(invalid_update, timeout=2)
        self.assertEqual(caught.exception.code, 400)

        gpus = [
            {"index": 4, "utilization_percent": 0, "memory_free_mb": 23000, "idle_for_seconds": 600},
            {"index": 5, "utilization_percent": 1, "memory_free_mb": 22500, "idle_for_seconds": 600},
        ]
        _, heartbeat = self.post_json(
            "/api/v1/agents/heartbeat",
            {"agent_id": "agent-smoke", "hostname": "trainer", "state": "idle", "gpus": gpus,
             "idle_reminder_seconds": 300, "idle_reminder_gpu_count": 2},
        )
        self.assertTrue(heartbeat["idle_reminder"])
        _, second_heartbeat = self.post_json(
            "/api/v1/agents/heartbeat",
            {"agent_id": "agent-smoke", "hostname": "trainer", "state": "idle", "gpus": gpus,
             "idle_reminder_seconds": 300, "idle_reminder_gpu_count": 2},
        )
        self.assertFalse(second_heartbeat["idle_reminder"])
        self.assertTrue(any(item["id"] == created["id"] for item in second_heartbeat["preflight_jobs"]))
        _, preflight = self.post_json(
            f"/api/v1/agents/jobs/{created['id']}/preflight",
            {"agent_id": "agent-smoke", "status": "passed", "checks": [{"name": "imports", "ok": True, "detail": "ok"}], "message": "all checks passed"},
        )
        self.assertEqual(preflight["job"]["preflight_status"], "passed")

        with urlopen(self.base + "/queue", timeout=2) as response:
            queue_page = response.read().decode("utf-8")
        self.assertIn('class="gpu-grid"', queue_page)
        self.assertIn('class="gpu-card is-idle"', queue_page)
        self.assertIn("GPU 4", queue_page)
        self.assertIn("当前可用 <strong>23,000 MiB", queue_page)

        _, claim = self.post_json("/api/v1/agents/claim", {"agent_id": "agent-smoke", "gpus": gpus})
        job = claim["job"]
        self.assertEqual(job["id"], created["id"])
        self.assertEqual(job["assigned_gpus"], [4, 5])
        self.assertIn("command_text", job)

        locked_update = Request(
            self.base + f'/api/v1/web/queue/{job["id"]}/update',
            data=json.dumps({"name": "too late", "password": "test-password"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(locked_update, timeout=2)
        self.assertEqual(caught.exception.code, 409)
        with self.assertRaises(HTTPError) as caught:
            urlopen(detail_url, timeout=2)
        self.assertEqual(caught.exception.code, 409)

        self.post_json(f"/api/v1/agents/jobs/{job['id']}/status", {"agent_id": "agent-smoke", "status": "running"})
        self.post_json(f"/api/v1/agents/jobs/{job['id']}/anomaly", {"agent_id": "agent-smoke", "kind": "cuda_oom", "message": "CUDA out of memory"})
        _, requeued = self.post_json(
            f"/api/v1/agents/jobs/{job['id']}/status",
            {"agent_id": "agent-smoke", "status": "queued", "batch_size": 32, "error": "oom"},
        )
        self.assertEqual(requeued["job"]["batch_size"], 32)

        with urlopen(self.base + "/api/v1/public/queue", timeout=2) as response:
            public = json.loads(response.read())
        public_job = next(item for item in public["jobs"] if item["id"] == job["id"])
        self.assertNotIn("command_text", public_job)

        cancel_request = Request(
            self.base + f"/api/v1/web/queue/{job['id']}/action",
            data=json.dumps({"password": "test-password", "action": "cancel"}).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(cancel_request, timeout=2):
            pass
        with urlopen(self.base + "/queue", timeout=2) as response:
            cancelled_page = response.read().decode("utf-8")
        self.assertIn('class="queue-row queue-cancelled"', cancelled_page)
        self.assertIn('class="queue-status status-cancelled">已取消', cancelled_page)
        self.assertIn("不会被 Agent 领取", cancelled_page)
        self.assertIn("从头重新排队", cancelled_page)

        metadata = {
            "hypothesis": "attention improves small targets",
            "change_notes": "replace fusion block",
            "result_notes": "mAP improved",
            "conclusion": "keep this branch",
            "next_step": "run ablation",
            "baseline_run_id": "smoke-run",
            "password": "test-password",
        }
        metadata_request = Request(
            self.base + "/api/v1/web/runs/smoke-run/metadata",
            data=json.dumps(metadata).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(metadata_request, timeout=2) as response:
            run = json.loads(response.read())["run"]
        self.assertEqual(run["hypothesis"], metadata["hypothesis"])
        self.assertEqual(run["next_step"], metadata["next_step"])

    def test_yaml_lineage_catalog_autofills_run_and_queue_without_legacy_heartbeat_erasing_it(self):
        father_name = "lineage-father-experiment"
        child_name = "lineage-child-experiment"
        father_id = "lineage-father-run"
        child_id = "lineage-child-run"
        self.post_json("/api/v1/runs/start", {"run_id": father_id, "name": father_name, "total_epochs": 1})
        self.post_json(f"/api/v1/runs/{father_id}/finish", {"status": "completed"})
        self.post_json("/api/v1/runs/start", {"run_id": child_id, "name": child_name, "total_epochs": 1})

        legacy_catalog = [
            {"id": "father-script", "name": "father.py", "path": "/project/father.py", "task_name": father_name},
            {"id": "child-script", "name": "child.py", "path": "/project/child.py", "task_name": child_name},
        ]
        self.post_json("/api/v1/agents/heartbeat", {
            "agent_id": "lineage-agent", "hostname": "trainer", "state": "running",
            "current_job_id": "do-not-change", "gpus": [], "catalog": legacy_catalog,
        })
        enriched_catalog = [
            {**legacy_catalog[0], "model_yaml": "/project/yaml/father.yaml", "model_yaml_name": "father.yaml"},
            {**legacy_catalog[1], "model_yaml": "/project/yaml/child.yaml", "model_yaml_name": "child.yaml",
             "father_yaml": "/project/yaml/father.yaml", "father_yaml_name": "father.yaml",
             "lineage_relation": "father", "lineage_confidence": "exact",
             "change_summary": "only attention math uses FP32"},
        ]
        _, published = self.post_json("/api/v1/agents/catalog", {
            "agent_id": "lineage-agent", "catalog": enriched_catalog,
        })
        child_item = next(item for item in published["agent"]["catalog"] if item["id"] == "child-script")
        self.assertEqual(child_item["father_run_id"], father_id)
        self.assertEqual(child_item["lineage_status"], "matched")
        self.assertEqual(published["agent"]["state"], "running")
        self.assertEqual(published["agent"]["current_job_id"], "do-not-change")

        # A legacy heartbeat from the already-running old process must not erase the one-shot metadata.
        _, heartbeat = self.post_json("/api/v1/agents/heartbeat", {
            "agent_id": "lineage-agent", "hostname": "trainer", "state": "running",
            "current_job_id": "do-not-change", "gpus": [], "catalog": legacy_catalog,
        })
        preserved = next(item for item in heartbeat["agent"]["catalog"] if item["id"] == "child-script")
        self.assertEqual(preserved["father_yaml_name"], "father.yaml")
        self.assertEqual(preserved["father_run_id"], father_id)

        with urlopen(self.base + f"/api/v1/public/runs/{child_id}", timeout=2) as response:
            child_run = json.loads(response.read())["run"]
        self.assertEqual(child_run["baseline_run_id"], father_id)
        self.assertEqual(child_run["change_notes"], "only attention math uses FP32")
        with urlopen(self.base + f"/runs/{child_id}", timeout=2) as response:
            detail = response.read().decode("utf-8")
        self.assertIn("YAML 继承关系", detail)
        self.assertIn("已精确匹配父 Run", detail)

        request = Request(
            self.base + "/api/v1/web/queue",
            data=json.dumps({
                "name": "lineage queued child", "command": "python child.py",
                "working_directory": "/project", "script_id": "child-script",
                "password": "test-password",
            }).encode(),
            headers={"Content-Type": "application/json", "X-Monitor-Request": "dashboard"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            queued = json.loads(response.read())["job"]
        self.assertEqual(queued["baseline_run_id"], father_id)
        with urlopen(self.base + "/queue", timeout=2) as response:
            queue_page = response.read().decode("utf-8")
        self.assertIn('id="lineageInfo"', queue_page)
        self.assertIn("选择脚本后自动匹配", queue_page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
