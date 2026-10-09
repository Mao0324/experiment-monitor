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

- Agent 按配置文件的 `script_scan_patterns` 扫描每个 `allowed_roots`，不会执行脚本；当前默认匹配 `train/*/train_dronevehicle*.py` 和 `train/*/train_flir*.py`。它使用 Python AST 提取实验名、默认 batch、默认 GPU、checkpoint 和 data/model 配置路径。
- `script_scan_patterns` 接受字符串数组，例如 `["train_dronevehicle*.py", "train_flir*.py"]`。模式相对 `allowed_roots` 解析；如需扫描子目录，可显式使用 `**/train_flir*.py`。重叠模式会自动去重，绝对路径和包含 `..` 的越界模式会被忽略。
- 网页选择脚本后会自动填写任务名、Python、工作目录和启动命令。脚本内置预训练权重只作只读提示；“断点 checkpoint”仅在续训时填写 `last.pt`。
- 新任务必须先通过无 GPU 预检：路径白名单、Python/脚本/checkpoint/data/model 文件、磁盘空间，以及在 `CUDA_VISIBLE_DEVICES=""` 下导入 PyTorch 和 Ultralytics。
- 批量消融按“脚本 × batch”生成队列任务，单次最多 50 条。

## 安全暂停与 Agent 重启恢复

把本包的 `client/yolo_monitor.py` 更新到训练项目实际导入的监控模块。Agent 会设置 `YOLO_QUEUE_CONTROL_FILE`；点击“完成当前 Epoch 后暂停”后，每个 DDP rank 会在同一个 Epoch 边界设置 `trainer.stop`，随后使用最新 `last.pt` 恢复。

Agent 将 PID、任务、日志位置和读取偏移保存在 `state_file`。systemd 使用 `KillMode=process`，因此只重启 Agent 不会杀死训练子进程；新 Agent 会校验 `/proc/<pid>/environ` 中的任务 ID 后重新接管。若进程已经消失，会先读取服务器上的 Run 完成状态；服务器尚未确认完成且已绑定输出目录时，再核验对应的 `results.csv`、日志和 checkpoint。确认训练已完成时直接清理旧状态，不发送“进程消失”异常；未完成时，有 `last.pt` 的任务转为“已暂停”等待人工恢复，没有 checkpoint 则标记失败。服务器还会阻止旧版 Agent 将已完成 Run 错误回退为暂停，并在发送恢复异常邮件前再次核对完成状态。

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

## CUDA 健康检查与结果二次核验

`nvidia-smi` 能列出显卡不代表 CUDA 能初始化它。Agent 会在单卡独立进程中做 `libcuda` 初始化检查；检查失败的 GPU 显示“CUDA 不可用”，不计入空闲提醒，也不会被队列分配。结果按 `gpu_cuda_probe_seconds` 缓存（默认 300 秒）。训练子进程统一设置 `CUDA_DEVICE_ORDER=PCI_BUS_ID`，保证可见 GPU 的编号顺序稳定。排除故障卡只避免新任务选中它；显卡本身仍需在空闲维护窗口由管理员检查驱动、硬件与内核日志。

若 `nvidia-smi` 对某卡返回 `[N/A]`，该卡仍会显示在页面，但标为“遥测不可用”并暂不参与队列分配；不能仅凭可用显存推断它空闲。

服务或 Agent 重启后，服务器会通过 Agent 心跳发送核验任务。Agent 只读取任务**已绑定**的 `results.csv`、对应日志和 checkpoint，按 Epoch 幂等补录遗漏指标。100 行 CSV 本身不等于成功：只有达到计划 Epoch、训练完成日志在最后一次致命异常之后仍有成功收尾证据、存在 checkpoint 且原进程已经退出，才会把队列与实验状态改为完成。若训练进程仍在运行且日志持续更新，服务器不会仅凭监控上报中断标记“疑似卡住”；日志与指标均长时间不动时才触发疑似状态。
