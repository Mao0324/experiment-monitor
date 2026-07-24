# Epoch 精灵 Windows 客户端

这是 YOLO Experiment Monitor 的 Windows 原生客户端，不包含 WebView、浏览器内核或网页嵌入。

## 交互

- 双击 `YoloMonitorPet.exe` 后，桌面右下角出现原创的 Epoch 精灵；
- 鼠标悬停显示当前实验名称、Epoch、Batch、百分比和 ETA；
- 拖动精灵可改变位置，位置会保存在 `%APPDATA%\YoloMonitorPet\settings.json`；
- 单击精灵打开原生实验控制台；
- 控制台提供实验搜索、状态/收藏筛选、进度、指标曲线、实时日志、GPU/主机状态和 2–5 实验对比；
- 收藏、分组、标签和删除使用本地密码对话框，每次操作单独验证，密码不落盘；
- 右键桌面精灵或托盘图标可打开面板、立即刷新或退出。

默认连接 `https://monitor.maocong.me`，可在“服务器设置”中修改。客户端每 3 秒通过公开只读 JSON 接口更新，不执行训练命令，也不下载数据集或模型权重。

## 系统要求

- Windows 10/11 64 位；
- 系统自带的 .NET Framework 4.8；
- 能通过 HTTPS 访问监控域名。

如果 Windows SmartScreen 首次提示未知发布者，这是因为 EXE 未购买代码签名证书；确认文件来自本项目后，可选择“更多信息 → 仍要运行”。

## 从源码重新编译

在 Windows PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```
