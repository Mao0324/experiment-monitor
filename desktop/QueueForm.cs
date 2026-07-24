using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class QueueForm : Form
    {
        private readonly MonitorApi api;
        private readonly DataGridView jobsGrid;
        private readonly RichTextBox agentsText;
        private readonly Label statusLabel;
        private readonly Timer refreshTimer;
        private bool loading;

        public QueueForm(MonitorApi api)
        {
            this.api = api;
            Text = "实验队列 · YOLO LAB";
            Icon = Theme.CreateAppIcon();
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(1180, 720);
            MinimumSize = new Size(980, 620);
            BackColor = Theme.Back;
            ForeColor = Theme.Text;
            Font = Theme.Font(9, FontStyle.Regular);

            Panel header = new Panel { Dock = DockStyle.Top, Height = 74, BackColor = Theme.Panel2, Padding = new Padding(22, 14, 22, 10) };
            Label title = Theme.Label("实验队列与训练 Agent", 15, FontStyle.Bold, Theme.Text);
            title.Location = new Point(22, 12);
            statusLabel = Theme.Label("正在读取队列…", 8.5f, FontStyle.Regular, Theme.Muted);
            statusLabel.Location = new Point(24, 45);
            Button add = Theme.Button("＋ 新建任务", true);
            Button refresh = Theme.Button("刷新", false);
            Button requeue = Theme.Button("重新排队", false);
            Button cancel = Theme.Button("取消任务", false);
            foreach (Button button in new Button[] { add, refresh, requeue, cancel }) { button.AutoSize = false; button.Size = new Size(104, 36); button.Anchor = AnchorStyles.Top | AnchorStyles.Right; }
            header.Resize += delegate
            {
                cancel.Location = new Point(header.ClientSize.Width - 126, 17);
                requeue.Location = new Point(header.ClientSize.Width - 240, 17);
                refresh.Location = new Point(header.ClientSize.Width - 354, 17);
                add.Location = new Point(header.ClientSize.Width - 468, 17);
            };
            add.Click += OnAdd;
            refresh.Click += async delegate { await LoadQueueAsync(); };
            requeue.Click += async delegate { await PerformActionAsync("requeue", "重新排队"); };
            cancel.Click += async delegate { await PerformActionAsync("cancel", "取消"); };
            header.Controls.Add(title); header.Controls.Add(statusLabel); header.Controls.Add(add); header.Controls.Add(refresh); header.Controls.Add(requeue); header.Controls.Add(cancel);

            jobsGrid = new DataGridView { Dock = DockStyle.Fill };
            ConfigureGrid(jobsGrid);
            jobsGrid.Columns.Add("id", "ID"); jobsGrid.Columns["id"].Visible = false;
            jobsGrid.Columns.Add("name", "任务"); jobsGrid.Columns["name"].AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill; jobsGrid.Columns["name"].MinimumWidth = 220;
            jobsGrid.Columns.Add("status", "状态"); jobsGrid.Columns["status"].Width = 95;
            jobsGrid.Columns.Add("priority", "优先级"); jobsGrid.Columns["priority"].Width = 70;
            jobsGrid.Columns.Add("gpus", "GPU 条件"); jobsGrid.Columns["gpus"].Width = 150;
            jobsGrid.Columns.Add("idle", "空闲阈值"); jobsGrid.Columns["idle"].Width = 95;
            jobsGrid.Columns.Add("attempt", "尝试"); jobsGrid.Columns["attempt"].Width = 70;
            jobsGrid.Columns.Add("policy", "异常策略"); jobsGrid.Columns["policy"].Width = 135;
            jobsGrid.Columns.Add("agent", "Agent"); jobsGrid.Columns["agent"].Width = 130;

            DarkPanel jobsPanel = new DarkPanel { Dock = DockStyle.Fill, Padding = new Padding(14), Radius = 14 };
            Label jobsTitle = Theme.Label("任务队列", 11, FontStyle.Bold, Theme.Text); jobsTitle.Dock = DockStyle.Top; jobsTitle.Height = 34;
            jobsPanel.Controls.Add(jobsGrid); jobsPanel.Controls.Add(jobsTitle);

            agentsText = new RichTextBox { Dock = DockStyle.Fill, ReadOnly = true, BorderStyle = BorderStyle.None, BackColor = Theme.Panel, ForeColor = Theme.Muted, Font = Theme.Font(9, FontStyle.Regular) };
            DarkPanel agentsPanel = new DarkPanel { Dock = DockStyle.Bottom, Height = 185, Padding = new Padding(16), Radius = 14 };
            Label agentsTitle = Theme.Label("训练 Agent / GPU 空闲状态", 11, FontStyle.Bold, Theme.Text); agentsTitle.Dock = DockStyle.Top; agentsTitle.Height = 34;
            agentsPanel.Controls.Add(agentsText); agentsPanel.Controls.Add(agentsTitle);

            Panel body = new Panel { Dock = DockStyle.Fill, Padding = new Padding(18), BackColor = Theme.Back };
            body.Controls.Add(jobsPanel); body.Controls.Add(agentsPanel);
            Controls.Add(body); Controls.Add(header);

            refreshTimer = new Timer { Interval = 5000 };
            refreshTimer.Tick += async delegate { await LoadQueueAsync(); };
            Shown += async delegate { refreshTimer.Start(); await LoadQueueAsync(); };
            FormClosed += delegate { refreshTimer.Stop(); refreshTimer.Dispose(); };
        }

        private static void ConfigureGrid(DataGridView view)
        {
            view.AllowUserToAddRows = false; view.AllowUserToDeleteRows = false; view.AllowUserToResizeRows = false;
            view.MultiSelect = false; view.ReadOnly = true; view.RowHeadersVisible = false;
            view.SelectionMode = DataGridViewSelectionMode.FullRowSelect; view.BackgroundColor = Theme.Panel;
            view.BorderStyle = BorderStyle.None; view.GridColor = Theme.Line; view.EnableHeadersVisualStyles = false;
            view.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Theme.Panel2, ForeColor = Theme.Muted, SelectionBackColor = Theme.Panel2, Font = Theme.Font(8.5f, FontStyle.Bold) };
            view.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Theme.Panel, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(39, 59, 94), SelectionForeColor = Theme.Text, Padding = new Padding(6) };
            view.RowTemplate.Height = 42;
        }

        public async Task LoadQueueAsync()
        {
            if (loading || IsDisposed) return;
            loading = true;
            try
            {
                Dictionary<string, object> data = await api.GetQueueAsync();
                jobsGrid.Rows.Clear();
                foreach (object raw in Json.Children(data, "jobs"))
                {
                    Dictionary<string, object> job = Json.Dict(raw);
                    string candidates = string.Join(",", Json.Children(job, "gpu_candidates").Select(Convert.ToString).ToArray());
                    jobsGrid.Rows.Add(Json.Text(job, "id"), Json.Text(job, "name"), StatusText(Json.Text(job, "status")), Json.Int(job, "priority"),
                        Json.Int(job, "required_gpu_count") + " 张 / " + (candidates.Length > 0 ? candidates : "任意"), Duration(Json.Int(job, "idle_seconds")),
                        Json.Int(job, "attempt_count") + "/" + (Json.Int(job, "retry_limit") + 1), PolicyText(Json.Text(job, "anomaly_policy")), Json.Text(job, "agent_id", "—"));
                }
                List<string> agentLines = new List<string>();
                foreach (object raw in Json.Children(data, "agents"))
                {
                    Dictionary<string, object> agent = Json.Dict(raw);
                    agentLines.Add(Json.Text(agent, "hostname") + "  ·  " + Json.Text(agent, "state") + "  ·  " + Json.Text(agent, "updated_at"));
                    foreach (object rawGpu in Json.Children(agent, "gpus"))
                    {
                        Dictionary<string, object> gpu = Json.Dict(rawGpu);
                        agentLines.Add(string.Format(CultureInfo.InvariantCulture, "    GPU {0}  利用率 {1:0}%  可用显存 {2:0} MiB  已空闲 {3}", Json.Int(gpu, "index"), Json.Number(gpu, "utilization_percent", 0), Json.Number(gpu, "memory_free_mb", 0), Duration(Json.Int(gpu, "idle_for_seconds"))));
                    }
                }
                agentsText.Text = agentLines.Count > 0 ? string.Join(Environment.NewLine, agentLines.ToArray()) : "还没有训练 Agent 上线。请在训练 Linux 机器安装 queue_agent.py。";
                statusLabel.Text = string.Format("● 已连接 · {0} 个任务 · {1} 个 Agent", jobsGrid.Rows.Count, Json.Children(data, "agents").Length);
                statusLabel.ForeColor = Theme.Green;
            }
            catch (Exception ex)
            {
                statusLabel.Text = "● 队列读取失败 · " + ex.Message;
                statusLabel.ForeColor = Theme.Red;
            }
            finally { loading = false; }
        }

        private async void OnAdd(object sender, EventArgs e)
        {
            using (NewQueueJobDialog dialog = new NewQueueJobDialog())
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                string password = PasswordDialog.Ask(this, "加入实验队列", "请输入管理员密码以提交训练任务：");
                if (password == null) return;
                try { await api.CreateQueueJobAsync(dialog.BuildPayload(), password); await LoadQueueAsync(); }
                catch (AdminPasswordException) { MessageBox.Show(this, "管理员密码错误，任务未提交。", "提交失败", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
                catch (Exception ex) { MessageBox.Show(this, ex.Message, "提交失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            }
        }

        private async Task PerformActionAsync(string action, string actionText)
        {
            if (jobsGrid.SelectedRows.Count == 0) { MessageBox.Show(this, "请先选择一个队列任务。", actionText, MessageBoxButtons.OK, MessageBoxIcon.Information); return; }
            string id = Convert.ToString(jobsGrid.SelectedRows[0].Cells["id"].Value);
            string name = Convert.ToString(jobsGrid.SelectedRows[0].Cells["name"].Value);
            if (MessageBox.Show(this, "确定要" + actionText + "任务“" + name + "”吗？", actionText, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            string password = PasswordDialog.Ask(this, actionText + "任务", "请输入管理员密码：");
            if (password == null) return;
            try { await api.QueueActionAsync(id, action, password); await LoadQueueAsync(); }
            catch (AdminPasswordException) { MessageBox.Show(this, "管理员密码错误。", actionText + "失败", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, actionText + "失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private static string StatusText(string value)
        {
            Dictionary<string, string> values = new Dictionary<string, string> { { "queued", "排队中" }, { "leased", "已领取" }, { "running", "运行中" }, { "waiting_memory", "等待显存" }, { "completed", "已完成" }, { "failed", "失败" }, { "cancelled", "已取消" } };
            return values.ContainsKey(value) ? values[value] : value;
        }
        private static string PolicyText(string value)
        {
            Dictionary<string, string> values = new Dictionary<string, string> { { "notify", "仅通知" }, { "stop", "直接停止" }, { "retry_lower_batch", "降低 batch 重试" }, { "retry_when_memory", "等待显存重试" } };
            return values.ContainsKey(value) ? values[value] : value;
        }
        private static string Duration(int seconds) { TimeSpan value = TimeSpan.FromSeconds(Math.Max(0, seconds)); return value.TotalHours >= 1 ? string.Format("{0}h {1}m", (int)value.TotalHours, value.Minutes) : string.Format("{0}m {1}s", value.Minutes, value.Seconds); }
    }

    internal sealed class NewQueueJobDialog : Form
    {
        private readonly TextBox nameBox = Theme.TextBox("");
        private readonly TextBox cwdBox = Theme.TextBox("");
        private readonly TextBox commandBox = Theme.TextBox("");
        private readonly TextBox candidatesBox = Theme.TextBox("");
        private readonly TextBox checkpointBox = Theme.TextBox("");
        private readonly TextBox baselineBox = Theme.TextBox("");
        private readonly RichTextBox notesBox = new RichTextBox();
        private readonly NumericUpDown priority = Number(-1000, 1000, 0);
        private readonly NumericUpDown gpuCount = Number(1, 16, 1);
        private readonly NumericUpDown idleSeconds = Number(0, 86400, 60);
        private readonly NumericUpDown freeMemory = Number(0, 200000, 2048);
        private readonly NumericUpDown retries = Number(0, 20, 0);
        private readonly NumericUpDown retryDelay = Number(5, 86400, 60);
        private readonly NumericUpDown batch = Number(0, 100000, 64);
        private readonly NumericUpDown minBatch = Number(1, 100000, 1);
        private readonly ComboBox policy = new ComboBox();

        public NewQueueJobDialog()
        {
            Text = "新建实验队列任务"; Icon = Theme.CreateAppIcon(); ClientSize = new Size(760, 690); MinimumSize = new Size(700, 620); StartPosition = FormStartPosition.CenterParent;
            BackColor = Theme.Back; ForeColor = Theme.Text; Font = Theme.Font(9, FontStyle.Regular);
            TableLayoutPanel form = new TableLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(22), ColumnCount = 2, RowCount = 15, BackColor = Theme.Back };
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 190)); form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            Add(form, 0, "任务名称", nameBox); Add(form, 1, "工作目录", cwdBox); Add(form, 2, "启动命令", commandBox);
            Add(form, 3, "优先级", priority); Add(form, 4, "同时空闲 GPU 数量", gpuCount); Add(form, 5, "候选 GPU（逗号分隔）", candidatesBox);
            Add(form, 6, "GPU 空闲阈值（秒）", idleSeconds); Add(form, 7, "单卡最低空闲显存 MiB", freeMemory); Add(form, 8, "失败重试次数", retries); Add(form, 9, "重试等待（秒）", retryDelay);
            policy.DropDownStyle = ComboBoxStyle.DropDownList; policy.Items.AddRange(new object[] { "仅通知", "直接停止", "降低 batch 后重试", "等待显存后重试" }); policy.SelectedIndex = 0; Theme.StyleComboBox(policy); Add(form, 10, "异常策略", policy);
            Add(form, 11, "初始 batch（0=不替换）", batch); Add(form, 12, "最小 batch", minBatch); Add(form, 13, "断点 checkpoint", checkpointBox); Add(form, 14, "基线 Run ID", baselineBox);
            notesBox.BackColor = Theme.Panel2; notesBox.ForeColor = Theme.Text; notesBox.BorderStyle = BorderStyle.FixedSingle; notesBox.Height = 80; notesBox.Dock = DockStyle.Fill;
            form.RowCount = 16; Add(form, 15, "任务备注", notesBox);
            Panel buttons = new Panel { Dock = DockStyle.Bottom, Height = 64, BackColor = Theme.Panel2 };
            Button ok = Theme.Button("加入队列", true); ok.Location = new Point(548, 14); ok.DialogResult = DialogResult.OK;
            Button cancelButton = Theme.Button("取消", false); cancelButton.Location = new Point(650, 14); cancelButton.DialogResult = DialogResult.Cancel;
            buttons.Controls.Add(ok); buttons.Controls.Add(cancelButton); Controls.Add(form); Controls.Add(buttons); AcceptButton = ok; CancelButton = cancelButton;
            nameBox.Text = "新实验";
            commandBox.Text = "python train.py";
        }

        private static NumericUpDown Number(decimal min, decimal max, decimal value) { return new NumericUpDown { Minimum = min, Maximum = max, Value = value, BackColor = Theme.Panel2, ForeColor = Theme.Text, BorderStyle = BorderStyle.FixedSingle, Dock = DockStyle.Fill }; }
        private static void Add(TableLayoutPanel panel, int row, string label, Control control) { panel.RowStyles.Add(new RowStyle(SizeType.Absolute, row == 15 ? 90 : 44)); Label caption = Theme.Label(label, 9, FontStyle.Bold, Theme.Muted); caption.Dock = DockStyle.Fill; caption.TextAlign = ContentAlignment.MiddleLeft; control.Dock = DockStyle.Fill; panel.Controls.Add(caption, 0, row); panel.Controls.Add(control, 1, row); }

        public Dictionary<string, object> BuildPayload()
        {
            Dictionary<string, object> payload = new Dictionary<string, object>();
            payload["name"] = nameBox.Text.Trim(); payload["working_directory"] = cwdBox.Text.Trim(); payload["command"] = commandBox.Text.Trim();
            payload["priority"] = (int)priority.Value; payload["required_gpu_count"] = (int)gpuCount.Value; payload["gpu_candidates"] = candidatesBox.Text.Trim();
            payload["idle_seconds"] = (int)idleSeconds.Value; payload["min_free_memory_mb"] = (int)freeMemory.Value; payload["retry_limit"] = (int)retries.Value; payload["retry_delay_seconds"] = (int)retryDelay.Value;
            payload["anomaly_policy"] = new string[] { "notify", "stop", "retry_lower_batch", "retry_when_memory" }[Math.Max(0, policy.SelectedIndex)];
            if (batch.Value > 0) payload["batch_size"] = (int)batch.Value; payload["min_batch_size"] = (int)minBatch.Value;
            payload["resume_checkpoint"] = checkpointBox.Text.Trim(); payload["baseline_run_id"] = baselineBox.Text.Trim(); payload["notes"] = notesBox.Text.Trim();
            return payload;
        }
    }
}
