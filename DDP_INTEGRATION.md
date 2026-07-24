# Ultralytics 自动 DDP 接入

## 问题

`model.add_callback()` 添加到父进程的回调不会作为 `_callbacks` 参数写入 Ultralytics 自动生成的 DDP 临时脚本。临时脚本只重新导入 Trainer 类，并使用 `overrides` 创建新 Trainer。因此双卡 `device="4,5"` 时，需要让可导入的 Trainer 类自己在子进程中注册监控回调。

## 文件放置

将以下三个文件放在训练项目的 Python 可导入路径中，最简单是训练脚本所在的项目根目录：

```text
yolo_monitor.py
monitored_target_saliency_trainer.py
train_dronevehicle_ddp_fixed.py
```

如果项目不是 editable install，请在启动前显式加入项目根目录：

```bash
export PYTHONPATH="/你的项目根目录:${PYTHONPATH:-}"
```

验证子进程能导入监控 Trainer：

```bash
python -c "from monitored_target_saliency_trainer import MonitoredTargetSaliencyOBBTrainer; print(MonitoredTargetSaliencyOBBTrainer.__module__)"
```

输出应为：

```text
monitored_target_saliency_trainer
```

## 环境变量

```bash
export YOLO_MONITOR_URL='https://monitor.maocong.me'
export YOLO_MONITOR_TOKEN='服务器上的 MONITOR_API_TOKEN'
export YOLO_MONITOR_USE_PROXY='false'
```

监控客户端默认使用直连，不读取训练机的 `HTTP_PROXY`/`HTTPS_PROXY`。这可避免代理指向已关闭的 `127.0.0.1` 端口时出现 `Connection refused`。只有网络明确要求通过代理才能访问监控域名时，才设置 `YOLO_MONITOR_USE_PROXY=true`。

## 工作过程

1. 父进程的 `monitor.run()` 在调用 `tracked_train()` 前创建远程实验。
2. 父进程生成唯一 `run_id`，通过 `YOLO_MONITOR_RUN_ID` 传给 DDP 子进程。
3. DDP 临时脚本重新导入 `MonitoredTargetSaliencyOBBTrainer`。
4. `RemoteMonitorTrainerMixin` 在 Trainer 初始化后注册回调。
5. 只有全局 `RANK=0`（缺失时检查 `LOCAL_RANK=0`）执行网络上报。
6. Rank 1 不创建实验、不发送进度。
7. 子进程 Rank 0 在训练结束时发送最终指标；父进程捕获 DDP/`tracked_train` 异常并发送失败状态。
8. Rank 0 同时保留终端原始输出，并约每 15 秒上报最近日志以及所选 GPU/主机状态。
9. Trainer 启动时采集 Git commit、代码 dirty 状态、Python/PyTorch/CUDA/Ultralytics 版本，并为可读取的小型 YAML/JSON 配置计算 SHA-256。

卡住/掉线判定由服务端 `MONITOR_STALE_SECONDS` 控制，默认 600 秒。任何新的训练进度都会把状态从“疑似卡住/掉线”恢复为“运行中”。

不要再调用 `monitor.attach(model)`；自动 DDP 使用的是 `monitor.run()` 与监控 Trainer 子类。
