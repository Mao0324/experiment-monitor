"""Minimal example: adapt the model path, dataset and training arguments."""

from ultralytics import YOLO

from yolo_monitor import YoloExperimentMonitor


model = YOLO("yolo11n.pt")
monitor = YoloExperimentMonitor(experiment_name="drone-detection-baseline")

# The wrapper reports a normal completion or a Python exception, then preserves
# Ultralytics' original return value/exception behavior.
results = monitor.train(
    model,
    data="path/to/data.yaml",
    epochs=100,
    imgsz=640,
)
