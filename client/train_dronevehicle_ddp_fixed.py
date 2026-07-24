"""DDP-safe version of the supplied DroneVehicle PaperLAF training script."""

import inspect
import os

os.environ["WANDB_MODE"] = "disabled"
os.environ["COMET_MODE"] = "DISABLED"
# This project has a dead localhost proxy configured globally.  Monitoring uses
# a direct HTTPS connection and DDP children inherit both settings.
if not os.getenv("YOLO_MONITOR_URL"):
    os.environ["YOLO_MONITOR_URL"] = "https://monitor.maocong.me"
if not os.getenv("YOLO_MONITOR_USE_PROXY"):
    os.environ["YOLO_MONITOR_USE_PROXY"] = "false"

import torch
from ultralytics import YOLO

from monitored_target_saliency_trainer import MonitoredTargetSaliencyOBBTrainer
from tools.pretrained_rerun_tracker import tracked_train
from yolo_monitor import YoloExperimentMonitor


_torch_load = torch.load
_torch_load_supports_weights_only = "weights_only" in inspect.signature(_torch_load).parameters


def _torch_load_trusted_checkpoint(*args, **kwargs):
    if _torch_load_supports_weights_only:
        kwargs.setdefault("weights_only", False)
    else:
        kwargs.pop("weights_only", None)
    return _torch_load(*args, **kwargs)


torch.load = _torch_load_trusted_checkpoint

CHECKPOINT = (
    "/media/biiteam/新加卷/biiteam/MCONG/Yolov8_TwoStream/"
    "pre-pth/yolov8s-obb_twostream_darkact_target_saliency_paperlaf_p345_fp32safe_v2.pt"
)
EXPERIMENT_NAME = (
    "DarkAct_TargetSaliencyPaperLAFMergeFeedback2D_P345_HNA_"
    "FullC-DilK3-PoolK3-OBBMaskS-FP32Attn_v2"
)
EXPERIMENT_NAME = os.getenv("YOLO_EXPERIMENT_NAME") or EXPERIMENT_NAME
QUEUE_BATCH = int(os.getenv("YOLO_QUEUE_BATCH") or "64")
QUEUE_RESUME = os.getenv("YOLO_QUEUE_RESUME_CHECKPOINT", "").strip()
ACTIVE_CHECKPOINT = QUEUE_RESUME or CHECKPOINT
if os.getenv("YOLO_QUEUE_JOB_ID") and os.getenv("CUDA_VISIBLE_DEVICES"):
    # The Agent exposes only the selected physical GPUs, so Ultralytics receives
    # their process-local indices (0,1,...) instead of hard-coded machine IDs.
    QUEUE_DEVICE = ",".join(str(index) for index, _ in enumerate(os.environ["CUDA_VISIBLE_DEVICES"].split(",")))
else:
    QUEUE_DEVICE = "4,5"

if not os.getenv("YOLO_MONITOR_TOKEN"):
    raise RuntimeError("Set YOLO_MONITOR_TOKEN before training")

model = YOLO(ACTIVE_CHECKPOINT, task="obb")
monitor = YoloExperimentMonitor(experiment_name=EXPERIMENT_NAME)

# run() creates the remote experiment before tracked_train starts.  It exports
# the same run ID to the generated DDP children and reports parent-side errors.
results = monitor.run(
    tracked_train,
    model,
    "PT-R015",
    ACTIVE_CHECKPOINT,
    trainer=MonitoredTargetSaliencyOBBTrainer,
    data="/media/biiteam/新加卷/biiteam/MCONG/Yolov8_TwoStream/data/dronevehicle.yaml",
    batch=QUEUE_BATCH,
    epochs=100,
    imgsz=640,
    workers=8,
    device=QUEUE_DEVICE,
    project="DroneVehicle_OBB_FusionTransfer",
    name=EXPERIMENT_NAME,
    exist_ok=False,
    task="obb",
    resume=QUEUE_RESUME or False,
)
