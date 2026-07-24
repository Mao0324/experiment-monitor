"""Importable DDP-safe Trainer used by the DroneVehicle training script.

Keep this module on the training project's Python path.  Ultralytics writes the
Trainer's module/class name into its generated DDP script, so the child process
will import this class and run the monitoring mixin again.
"""

from ultralytics.models.yolo.obb.target_saliency_train import TargetSaliencyOBBTrainer

from yolo_monitor import RemoteMonitorTrainerMixin


class MonitoredTargetSaliencyOBBTrainer(
    RemoteMonitorTrainerMixin,
    TargetSaliencyOBBTrainer,
):
    """TargetSaliencyOBBTrainer plus rank-0 remote monitoring callbacks."""

    pass
