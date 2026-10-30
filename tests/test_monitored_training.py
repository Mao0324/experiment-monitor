import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "client" / "monitored_training.py"
spec = importlib.util.spec_from_file_location("monitored_training_test", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Model:
    def __init__(self):
        self.calls = []

    def train(self, **kwargs):
        self.calls.append(kwargs)
        return "trained"


class MonitoredTrainingTest(unittest.TestCase):
    def test_manual_training_preserves_trainer_and_arguments(self):
        model = Model()
        trainer = object()
        with patch.dict(os.environ, {}, clear=True):
            result = module.train_with_monitor(model, trainer=trainer, device="2", batch=4)
            self.assertEqual(module.agent_device("2", single_gpu=True), "2")
        self.assertEqual(result, "trained")
        self.assertEqual(model.calls, [{"trainer": trainer, "device": "2", "batch": 4}])

    def test_queue_uses_assigned_physical_devices(self):
        with patch.dict(os.environ, {"YOLO_QUEUE_JOB_ID": "job", "CUDA_VISIBLE_DEVICES": "5,7"}):
            self.assertEqual(module.agent_device("0", single_gpu=True), "5")
            self.assertEqual(module.agent_device(None), "5,7")
            self.assertEqual(module.agent_device("cpu", single_gpu=True), "cpu")

    def test_queue_rejects_missing_gpu_assignment(self):
        with patch.dict(os.environ, {"YOLO_QUEUE_JOB_ID": "job", "CUDA_VISIBLE_DEVICES": ""}):
            with self.assertRaises(RuntimeError):
                module.agent_device("0")

    def test_queue_requires_monitor_credentials(self):
        with patch.dict(os.environ, {"YOLO_QUEUE_JOB_ID": "job"}, clear=True):
            with self.assertRaises(RuntimeError):
                module.train_with_monitor(Model(), device="5")

    def test_monitor_receives_original_trainer(self):
        calls = []

        class Monitor:
            def __init__(self, experiment_name=None):
                calls.append(("name", experiment_name))

            def train(self, model, **kwargs):
                calls.append(("train", model, kwargs))
                return "monitored"

        fake = types.SimpleNamespace(YoloExperimentMonitor=Monitor)
        model = Model()
        trainer = object()
        env = {"YOLO_MONITOR_URL": "https://example.test", "YOLO_MONITOR_TOKEN": "secret",
               "YOLO_EXPERIMENT_NAME": "queue-run"}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"yolo_monitor": fake}):
            result = module.train_with_monitor(model, trainer=trainer, device="5", batch=8)
        self.assertEqual(result, "monitored")
        self.assertEqual(calls, [("name", "queue-run"),
                                 ("train", model, {"trainer": trainer, "device": "5", "batch": 8})])
        self.assertFalse(model.calls)


if __name__ == "__main__":
    unittest.main()
