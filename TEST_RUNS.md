# 网页测试集评估

实验详情的「总览」页有独立的 Test 测试集摘要和「运行测试集」按钮；「产物」页保留历次测试的完整指标、日志和文件路径。页面原有的 Best Val mAP、Epoch 曲线和验证集指标不被测试结果覆盖。网页只选择训练机公布的测试方案；测试命令由该训练机的 Agent 配置提供，不能从浏览器提交任意命令。每次提交需要管理员密码。

在训练机 `/etc/yolo-monitor-agent.json` 中加入 `test_profiles`。`ubuntu` 训练机的三个已核实方案见 [client/test_profiles_ubuntu.example.json](client/test_profiles_ubuntu.example.json)。它们都使用 Agent 的 `training_python`（`/home/ubuntu/miniconda3/envs/spikeyolo/bin/python`），不使用脚本注释里的 `yolotwo`。别的训练机按其测试脚本和运行目录分别配置；未配置的主机不会显示测试按钮选项。

每个方案指定唯一 `id`、显示用 `label`、允许的实验目录 `run_prefixes`、本机 `script`，以及参数数组 `args_complete` 和 `args_incomplete`。占位符 `{run_dir}` 指向当前实验目录，`{checkpoint}` 指向该实验的 `weights/best.pt`。参数以 argv 数组执行，不经 shell；Agent 使用 `CUDA_VISIBLE_DEVICES` 把测试限制在领取到的一张空闲 GPU。不要在测试脚本里另行覆盖这个环境变量或写死物理 GPU 编号。仅配置 `args_complete` 时，未完成实验不可测试。

完整训练结束且 Epoch 达到总数时，运行 `args_complete`，测试脚本需将 `metrics.yaml` 写到原实验目录的 `test/` 下，并输出 `Metrics saved to <路径>`；测试结果和文件路径都会显示在网页。未完成时必须经过两次确认；Agent 会把 checkpoint 复制到临时实验副本，再运行 `args_incomplete`，原目录不写测试文件。网页保留测试指标和日志摘要，临时目录在测试进程结束后清理。测试中 Agent 重启时会尝试接管遗留进程；若退出码已无法核实，仍会核对完整指标或结果文件，证据不足才标记失败。不要在未完成测试方案中使用会写入原始绝对路径的脚本。

测试只使用绑定到该 Run 的精确 `save_dir` 和 `best.pt`，不会按实验名寻找其他权重。训练任务和测试任务共享服务器端 GPU 预留；正在训练的显卡不会被测试领取。配置更新后需要更新 Agent 代码并在合适窗口重启 Agent；现有训练进程不应为此中断。
