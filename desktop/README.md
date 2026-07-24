# Epoch 精灵 Windows 客户端

这是 YOLO Experiment Monitor 的 Windows 原生客户端，不包含 WebView、浏览器内核或网页嵌入。

## 交互

- 双击 `YoloMonitorPet.exe` 后，桌面右下角出现原创的 Epoch 精灵；
- 鼠标悬停显示当前实验名称、Epoch、Batch、百分比和 ETA；
- 拖动精灵可改变位置，位置会保存在 `%APPDATA%\YoloMonitorPet\settings.json`；
- 单击精灵打开原生实验控制台；
- 控制台内置 Material Design、Claymorphism 和 Elegant 三套完整视觉风格，可在顶部操作栏或托盘菜单即时切换；
- 风格选择会保存到 `%APPDATA%\YoloMonitorPet\settings.json`，下次启动自动沿用；
- 控制台提供实验搜索、状态/收藏筛选、进度、指标曲线、实时日志、GPU/主机状态和 2–5 实验对比；
- 收藏、分组、标签和删除使用本地密码对话框，每次操作单独验证，密码不落盘；
- 右键桌面精灵或托盘图标可打开面板、切换风格、立即刷新或退出。

## 视觉风格

- **Material Design**：深色高对比工作台，适合长时间查看训练状态；
- **Claymorphism**：浅色软浮雕卡片、柔和阴影和更大的圆角；
- **Elegant**：黑金低饱和界面、细边框和克制的小圆角。

三套风格不仅切换色板，还分别定义卡片形态、按钮层级、阴影、高光、边框、进度条和 Epoch 精灵外观。

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
