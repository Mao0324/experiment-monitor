# 实验队列 Agent

训练任务由训练机主动通过 HTTPS 领取，公网服务器不会 SSH 登录训练机。Agent 只执行 `allowed_roots` 内的脚本，并且不使用 Shell 解析命令。

## 安装

把 `client` 目录复制到训练机，然后执行：

```bash
cd client
sudo bash install_queue_agent.sh
sudo nano /etc/yolo-monitor-agent.json
sudo systemctl restart yolo-monitor-agent
journalctl -u yolo-monitor-agent -f
```

Agent 服务固定从 `/opt/yolo-monitor-agent` 启动；每个实验仍会切换到队列任务单独填写的工作目录，因此项目磁盘暂时未挂载时也不会导致 Agent 自身反复启动失败。

配置文件中的 `api_token` 使用训练监控现有的 `YOLO_MONITOR_TOKEN`。不要填写网页管理员密码。

如果训练依赖 Conda，请在配置文件中把 `training_python` 设置为该环境的 Python 绝对路径。Agent 会用它扫描脚本并自动填表，例如：

```text
/home/biiteam/anaconda3/bin/python train_dronevehicle_ddp_fixed.py
```

## 脚本目录、预检与批量消融

- Agent 只扫描每个 `allowed_roots` 顶层的 `train_dronevehicle*.py`，不会执行脚本；它使用 Python AST 提取实验名、默认 batch、默认 GPU、checkpoint 和 data/model 配置路径。
- 网页选择脚本后会自动填写任务名、Python、工作目录和启动命令。脚本内置预训练权重只作只读提示；“断点 checkpoint”仅在续训时填写 `last.pt`。
- 新任务必须先通过无 GPU 预检：路径白名单、Python/脚本/checkpoint/data/model 文件、磁盘空间，以及在 `CUDA_VISIBLE_DEVICES=""` 下导入 PyTorch 和 Ultralytics。
- 批量消融按“脚本 × batch”生成队列任务，单次最多 50 条。

## 安全暂停与 Agent 重启恢复

把本包的 `client/yolo_monitor.py` 更新到训练项目实际导入的监控模块。Agent 会设置 `YOLO_QUEUE_CONTROL_FILE`；点击“完成当前 Epoch 后暂停”后，每个 DDP rank 会在同一个 Epoch 边界设置 `trainer.stop`，随后使用最新 `last.pt` 恢复。

Agent 将 PID、任务、日志位置和读取偏移保存在 `state_file`。systemd 使用 `KillMode=process`，因此只重启 Agent 不会杀死训练子进程；新 Agent 会校验 `/proc/<pid>/environ` 中的任务 ID 后重新接管。若进程已经消失，有 `last.pt` 时任务转为“已暂停”等待人工恢复，没有 checkpoint 时标记失败，避免永久卡在“运行中”。

`worker_slots` 控制同一训练机可同时管理多少个独立任务。第一个槽位沿用 `agent_id` 和 `state_file`，其余槽位自动使用 `agent_id-slot-N` 与 `agent-state.slot-N.json`，所以每个任务的恢复状态互不覆盖。

## 精确输出目录绑定

训练启动后，监控回调会直接上报 Ultralytics 的 `trainer.save_dir`；Agent 也会从 `Logging results to ...` 日志行补充绑定。服务器把该任务唯一对应的 `save_dir`、`weights/last.pt`、`weights/best.pt` 和 `results.csv` 保存到队列记录和实验详情中。

一旦任务已绑定输出目录，暂停、取消、失败重试和断点续训只会检查这个目录内的 `last.pt`，不会再扫描整个项目并误用其他实验较新的 checkpoint。只有绑定功能上线前创建的旧任务会保留兼容性扫描。

## 自动 batch 和断点续训

Agent 会设置以下环境变量：

- `YOLO_QUEUE_BATCH`
- `YOLO_QUEUE_RESUME_CHECKPOINT`
- `YOLO_QUEUE_JOB_ID`
- `YOLO_MONITOR_RUN_ID`
- `CUDA_VISIBLE_DEVICES`

训练脚本需要读取这些变量。`train_dronevehicle_ddp_fixed.py` 已提供完整示例。普通命令也可以使用 `{batch}` 和 `{resume}` 占位符。

## 异常策略

- `notify`：发送提醒，训练继续运行。
- `stop`：终止当前进程组。
- `retry_lower_batch`：发生异常时将 batch 减半，不低于任务的 `min_batch_size`，然后重新排队。
- `retry_when_memory`：停止当前进程，等待满足 GPU 空闲显存和空闲时长条件后再次领取。

Agent 会识别 CUDA OOM、NaN/Inf、loss 爆炸、磁盘空间不足、进程异常退出和服务器取消命令。网页和异常邮件会给出具体处置建议，但不会未经允许修改训练代码。
