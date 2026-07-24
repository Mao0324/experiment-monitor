# YOLO Experiment Monitor

面向 1 核 1G Ubuntu 22.04 公网服务器的轻量 Ultralytics YOLO 实验监控系统。

已实现的范围：

- 网页查看当前 epoch、批次、耗时和预计剩余时间；
- 查看每个 epoch 的训练/验证指标；趋势图一次显示一个指标，带真实数值坐标、悬停值和统计值；
- 检测长时间无上报的卡住/掉线实验，并在恢复上报后自动回到运行状态；
- 查看训练日志尾部、GPU 利用率/显存/温度以及主机负载/内存；
- 勾选 2–5 条实验，按同一个指标和同一纵轴刻度对比；
- SSE 实时推送进度、日志、硬件状态和曲线，不再整页定时刷新；
- 自动识别 Best Epoch，并显示最佳值、最新值和差值；
- 自动记录 Git commit/dirty 状态、Python/PyTorch/CUDA/Ultralytics 版本及配置文件指纹；
- 无需登录即可只读查看实验列表、详情、对比和 SSE 实时数据；
- 提供不含 WebView 的 Windows 原生“Epoch 精灵”EXE：悬停查看进度，点击打开本地实验控制台；
- 实验分组、标签、收藏，以及名称/状态/分组/标签组合筛选；修改分组、标签和收藏时单独校验管理员密码；
- systemd timer 每日创建压缩 SQLite 备份，默认保留 14 天且不自动删除实验；
- SQLite 保存历史实验与最终结果；
- 在实验详情页输入管理员密码并确认后，永久删除单条实验及其指标；
- 正常结束或异常退出时，通过 QQ SMTP 发送邮件；
- 独立的训练上报 token 和管理操作密码；
- Nginx + HTTPS；
- systemd 开机启动、故障重启和安全限制；
- 上报失败不会中断 YOLO 训练。
- 自动 DDP 使用共享 run ID，并且只有 Rank 0 上报，避免双卡产生重复实验。
- Agent 自动扫描允许目录中的 `train_dronevehicle*.py` 并在网页选择脚本后自动填表；
- 新任务先做不占 GPU 的 Python、路径、checkpoint、数据配置、磁盘和依赖预检；
- 队列逐项解释未启动原因，包括缺少 GPU 数、显存不足、利用率超限和剩余空闲时间；
- 可复制历史队列任务或基于由队列启动的实验新建，并可按“脚本 × batch”批量生成消融任务；
- 支持完成当前 Epoch 后安全暂停、从最新 `last.pt` 一键恢复；Agent 重启后可重新接管遗留训练进程；
- OOM、NaN、loss 爆炸、磁盘不足和进程消失会显示具体建议，但不会未经允许修改代码。

服务端没有 PyPI 依赖，只使用 Python 标准库，适合低配置服务器。训练端同样不增加额外依赖，直接使用 Ultralytics 已提供的回调机制。

完整步骤见 [DEPLOY.md](DEPLOY.md)。接入示例见 [client/train_example.py](client/train_example.py)。
使用 `device="4,5"` 等自动 DDP 时，请改用 [DDP_INTEGRATION.md](DDP_INTEGRATION.md) 和
[client/train_dronevehicle_ddp_fixed.py](client/train_dronevehicle_ddp_fixed.py)。

## 目录

```text
server/
  monitor_server.py                 服务端应用
  monitor_maintenance.py            SQLite 备份与可选保留策略
  experiment-monitor.env.example   配置模板
  experiment-monitor.service       systemd 单元
  experiment-monitor-backup.*      每日备份 service/timer
  nginx-site.conf.template          Nginx 站点模板
  install.sh                        Ubuntu 安装/更新脚本
desktop/
  Program.cs                        Windows 原生客户端入口
  PetForms.cs                       悬浮宠物、悬停进度卡与托盘
  DashboardForm.cs                  本地实验管理控制台
  Charts.cs                         自绘单实验/多实验指标曲线
  build.ps1                         使用系统 C# 编译器生成 EXE
client/
  yolo_monitor.py                   Ultralytics 回调及异常包装器
  train_example.py                  最小接入示例
  monitored_target_saliency_trainer.py  可被 DDP 子进程重新导入的监控 Trainer
  train_dronevehicle_ddp_fixed.py   DroneVehicle 双卡修复示例
tests/
  test_smoke.py                     本地端到端冒烟测试
```

## 安全边界

- 训练机器只进行出站 HTTPS 请求，无需公网 IP 或开放端口。
- `8765` 仅绑定服务器回环地址，由 Nginx 对外提供 HTTPS。
- API token、QQ 授权码、管理密码只保存在服务器 `/etc/experiment-monitor.env`。
- 实验列表、详情、对比和实时数据是公开只读页面；知道监控域名的人可以看到实验名称、日志尾部、结果路径、硬件状态和可复现信息。
- 删除、收藏、分组和标签修改均需要在当次操作中提供管理员密码；密码只通过 HTTPS 提交，不保存在浏览器会话中。
- 除经管理员密码确认的记录管理操作外，系统不会远程执行训练命令。
- 不上传模型权重或数据集，只上报进度、指标、结果路径、日志尾部和硬件状态。
