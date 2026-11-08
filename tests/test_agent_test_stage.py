"""A small filesystem-only check for incomplete-run test isolation."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


AGENT = Path(__file__).resolve().parents[1] / "client" / "queue_agent.py"


class AgentTestStageTest(unittest.TestCase):
    def test_running_training_claims_test_without_waiting_for_idle(self):
        spec = importlib.util.spec_from_file_location("test_concurrent_queue_agent", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            agent = module.Agent({"server_url": "https://example.invalid", "api_token": "test",
                                  "allowed_roots": [temporary], "log_directory": temporary,
                                  "poll_seconds": 2})
            job = {"id": "training", "name": "training"}
            test = {"id": "test-while-training"}
            agent.current_job = job
            agent.current_pid = 123
            agent.current_process = Mock()
            agent.current_process.poll.side_effect = [None, 0, 0]
            agent.read_new_log = Mock()
            agent.heartbeat = Mock(return_value={})
            agent.gpu_status = Mock(return_value=[{"index": 0, "memory_free_mb": 5120}])
            agent.claim_test = Mock(return_value=test)
            agent.start_test_thread = Mock()
            agent.finish_process = Mock()
            with patch.object(module.time, "sleep", return_value=None):
                agent.monitor_current_process()
            agent.claim_test.assert_called_once()
            agent.start_test_thread.assert_called_once_with(test)
            agent.finish_process.assert_called_once_with(job, 0)

    def test_incomplete_run_uses_isolated_directory(self):
        spec = importlib.util.spec_from_file_location("test_stage_queue_agent", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset = base / "runs" / "ESCVehicle"
            run_dir = dataset / "run-a"
            weights = run_dir / "weights"
            weights.mkdir(parents=True)
            (weights / "best.pt").write_bytes(b"small fake checkpoint")
            (run_dir / "args.yaml").write_text("batch: 4\n", encoding="utf-8")
            (run_dir / "data.yaml").write_text("test: images\n", encoding="utf-8")
            script = base / "test_escvehicle.py"
            script.write_text("print('not executed')\n", encoding="utf-8")
            agent = module.Agent({"server_url": "https://example.invalid", "api_token": "test",
                                  "allowed_roots": [str(base)], "training_python": sys.executable,
                                  "test_profiles": [{"id": "esc", "script": str(script),
                                                     "run_prefixes": [str(dataset)],
                                                     "args_complete": ["--experiment", "{run_dir}", "--save-metrics"],
                                                     "args_incomplete": ["--experiment", "{run_dir}",
                                                                         "--checkpoint", "{checkpoint}"]}],
                                  "log_directory": str(base / "logs")})
            test = {"id": "temporary", "profile_id": "esc", "run_dir": str(run_dir),
                    "retain_results": False, "assigned_gpu": 0}
            try:
                cwd, command = agent._test_command(test)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege is unavailable")
                raise
            stage = Path(agent.test_stage)
            self.assertEqual(cwd, base)
            self.assertEqual(command[3], str(stage))
            self.assertEqual(command[-1], str(stage / "weights" / "best.pt"))
            self.assertNotIn("--save-metrics", command)
            self.assertEqual((stage / "weights" / "best.pt").read_bytes(), b"small fake checkpoint")
            self.assertNotEqual((stage / "weights" / "best.pt").stat().st_ino,
                                (run_dir / "weights" / "best.pt").stat().st_ino)
            self.assertEqual((stage / "args.yaml").read_text(encoding="utf-8"), "batch: 4\n")
            agent._cleanup_test_stage(test)
            self.assertFalse(stage.exists())
            self.assertEqual((run_dir / "weights" / "best.pt").read_bytes(), b"small fake checkpoint")

    def test_runner_records_metrics_without_leaking_partial_artifacts(self):
        spec = importlib.util.spec_from_file_location("test_runner_queue_agent", AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset = base / "runs" / "ESCVehicle"
            run_dir = dataset / "run-b"
            (run_dir / "weights").mkdir(parents=True)
            (run_dir / "weights" / "best.pt").write_bytes(b"small fake checkpoint")
            (run_dir / "args.yaml").write_text("batch: 4\n", encoding="utf-8")
            script = base / "test_escvehicle.py"
            script.write_text(
                "import pathlib,sys\n"
                "run=pathlib.Path(sys.argv[sys.argv.index('--experiment')+1])\n"
                "print('metrics:\\n  metrics/mAP50-95(B): 0.42', flush=True)\n"
                "result=run/'test'/'best'/'metrics.yaml'\n"
                "result.parent.mkdir(parents=True,exist_ok=True)\n"
                "result.write_text('metrics:\\n  map: 0.42\\n')\n"
                "if '--save-metrics' in sys.argv: print(f'Metrics saved to {result}',flush=True)\n",
                encoding="utf-8",
            )
            agent = module.Agent({"server_url": "https://example.invalid", "api_token": "test",
                                  "allowed_roots": [str(base)], "training_python": sys.executable,
                                  "test_profiles": [{"id": "esc", "script": str(script),
                                                     "run_prefixes": [str(dataset)],
                                                     "args_complete": ["--experiment", "{run_dir}", "--save-metrics"],
                                                     "args_incomplete": ["--experiment", "{run_dir}"]}],
                                  "log_directory": str(base / "logs"), "poll_seconds": 2})
            reports = []
            agent.update_test = lambda test, status, **values: reports.append({"status": status, **values}) or {}
            agent.heartbeat = lambda gpus: {}
            agent.gpu_status = lambda: []
            for retain in (False, True):
                test = {"id": f"test-{retain}", "profile_id": "esc", "run_dir": str(run_dir),
                        "retain_results": retain, "assigned_gpu": 0}
                stale_stage = None
                if not retain:
                    agent._test_command(test)
                    stale_stage = Path(agent.test_stage)
                try:
                    agent.run_test(test)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1314:
                        self.skipTest("Windows symlink privilege is unavailable")
                    raise
                self.assertEqual(reports[-1]["status"], "completed", reports[-1])
                self.assertEqual(reports[-1]["metrics"]["metrics/mAP50-95(B)"], 0.42)
                if retain:
                    self.assertTrue((run_dir / "test" / "best" / "metrics.yaml").is_file())
                    self.assertTrue(reports[-1]["result_path"].endswith("metrics.yaml"))
                else:
                    self.assertFalse(stale_stage.exists())
                    self.assertFalse((run_dir / "test").exists())
                    self.assertFalse(any(dataset.glob(".monitor-test-test-False-*")))


if __name__ == "__main__":
    unittest.main()
