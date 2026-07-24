using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class DashboardForm : Form
    {
        private readonly MonitorState state;
        private readonly AppSettings settings;
        private readonly DataGridView grid;
        private readonly TextBox searchBox;
        private readonly Button statusFilter;
        private int statusFilterIndex;
        private readonly CheckBox favoriteOnly;
        private readonly Label connectionLabel;
        private readonly Label runningCard;
        private readonly Label completedCard;
        private readonly Label stalledCard;
        private readonly Label selectedTitle;
        private readonly Label selectedStatus;
        private readonly Label epochValue;
        private readonly Label batchValue;
        private readonly Label timeValue;
        private readonly ModernProgress progress;
        private readonly Label metricsText;
        private readonly RichTextBox hostText;
        private readonly RichTextBox logText;
        private readonly TextBox groupBox;
        private readonly TextBox tagsBox;
        private readonly CheckBox favoriteBox;
        private readonly ComboBox metricSelector;
        private readonly TrendChart chart;
        private readonly Label bestText;
        private readonly Button saveButton;
        private readonly Button deleteButton;
        private readonly Button compareButton;
        private readonly Button updateButton;
        private readonly Button notesButton;
        private readonly HashSet<string> comparisonIds = new HashSet<string>();
        private Dictionary<string, object> selectedRun;
        private Dictionary<string, object> selectedDetail;
        private bool loadingDetail;
        private bool populatingGrid;

        [DllImport("user32.dll")]
        private static extern bool ReleaseCapture();
        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr handle, int message, IntPtr wParam, IntPtr lParam);

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams parameters = base.CreateParams;
                parameters.ClassStyle |= 0x00020000;
                return parameters;
            }
        }

        public DashboardForm(MonitorState state, AppSettings settings)
        {
            this.state = state;
            this.settings = settings;
            Text = "YOLO 实验监控 · Epoch 精灵";
            Icon = Theme.CreateAppIcon();
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.None;
            MinimumSize = new Size(1080, 680);
            ClientSize = new Size(1280, 780);
            BackColor = Theme.Back;
            ForeColor = Theme.Text;
            Font = Theme.Font(9, FontStyle.Regular);

            Panel header = new Panel { Dock = DockStyle.Fill, Height = 82, Padding = new Padding(26, 14, 20, 10), BackColor = Theme.Panel2, Margin = new Padding(0) };
            Label title = Theme.Label("YOLO  LAB", 15.5f, FontStyle.Bold, Theme.Text);
            title.Location = new Point(28, 13);
            connectionLabel = Theme.Label("● 正在连接", 8.5f, FontStyle.Regular, Theme.Muted);
            connectionLabel.Location = new Point(30, 47);
            Button refresh = Theme.Button("立即刷新", false);
            Button queueCenter = Theme.Button("实验队列", false);
            updateButton = Theme.Button("检查更新", false);
            Button server = Theme.Button("服务器设置", false);
            Button minimize = Theme.Button("—", false);
            Button maximize = Theme.Button("□", false);
            Button close = Theme.Button("×", false);
            refresh.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            queueCenter.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            updateButton.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            server.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            refresh.Size = new Size(96, 36); refresh.AutoSize = false;
            queueCenter.Size = new Size(96, 36); queueCenter.AutoSize = false;
            updateButton.Size = new Size(96, 36); updateButton.AutoSize = false;
            server.Size = new Size(106, 36); server.AutoSize = false;
            foreach (Button windowButton in new Button[] { minimize, maximize, close })
            {
                windowButton.Size = new Size(38, 34); windowButton.AutoSize = false; windowButton.Font = Theme.Font(10, FontStyle.Bold); windowButton.BackColor = Theme.Panel2;
            }
            close.ForeColor = Theme.Red;
            header.Resize += delegate
            {
                close.Location = new Point(header.ClientSize.Width - 48, 12);
                maximize.Location = new Point(header.ClientSize.Width - 90, 12);
                minimize.Location = new Point(header.ClientSize.Width - 132, 12);
                server.Location = new Point(header.ClientSize.Width - 252, 12);
                updateButton.Location = new Point(header.ClientSize.Width - 362, 12);
                refresh.Location = new Point(header.ClientSize.Width - 472, 12);
                queueCenter.Location = new Point(header.ClientSize.Width - 582, 12);
            };
            refresh.Click += async delegate { if (state != null) await state.RefreshAsync(); };
            queueCenter.Click += delegate { if (state != null) { QueueForm form = new QueueForm(state.Api); form.Show(this); } };
            updateButton.Click += async delegate
            {
                if (state == null) return;
                updateButton.Enabled = false;
                await UpdateWorkflow.CheckAndPromptAsync(this, settings.ServerUrl, false, delegate(string text) { updateButton.Text = text; });
                updateButton.Enabled = true;
            };
            server.Click += OnServerSettings;
            minimize.Click += delegate { WindowState = FormWindowState.Minimized; };
            maximize.Click += delegate { WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized; };
            close.Click += delegate { Close(); };
            header.MouseDown += DragWindow;
            title.MouseDown += DragWindow;
            header.DoubleClick += delegate { maximize.PerformClick(); };
            header.Controls.Add(title);
            header.Controls.Add(connectionLabel);
            header.Controls.Add(refresh);
            header.Controls.Add(queueCenter);
            header.Controls.Add(updateButton);
            header.Controls.Add(server);
            header.Controls.Add(minimize);
            header.Controls.Add(maximize);
            header.Controls.Add(close);
            SplitContainer split = new SplitContainer();
            split.Dock = DockStyle.Fill;
            split.BackColor = Theme.Line;
            split.Panel1.BackColor = Theme.Back;
            split.Panel2.BackColor = Theme.Back;
            split.Panel1.Padding = new Padding(18, 8, 8, 18);
            split.Panel2.Padding = new Padding(8, 8, 18, 18);
            TableLayoutPanel rootLayout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, Margin = new Padding(0), Padding = new Padding(0), BackColor = Theme.Back };
            rootLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 82));
            rootLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            rootLayout.Controls.Add(header, 0, 0);
            rootLayout.Controls.Add(split, 0, 1);
            Controls.Add(rootLayout);
            Shown += delegate { split.SplitterDistance = Math.Max(330, Math.Min(470, (int)(split.ClientSize.Width * .34))); };
            Resize += delegate
            {
                if (WindowState == FormWindowState.Normal)
                    using (GraphicsPath path = Theme.RoundRect(new Rectangle(0, 0, Width, Height), 14)) Region = new Region(path);
                else Region = null;
            };

            DarkPanel listPanel = new DarkPanel { Dock = DockStyle.Fill, Padding = new Padding(14), Radius = 16 };
            Panel filters = new Panel { Dock = DockStyle.Top, Height = 112, BackColor = Theme.Panel };
            Label libraryTitle = Theme.Label("实验库", 11, FontStyle.Bold, Theme.Text);
            libraryTitle.Location = new Point(0, 0);
            Label libraryHint = Theme.Label("RUNS", 7.5f, FontStyle.Bold, Theme.Blue);
            libraryHint.Location = new Point(58, 5);
            searchBox = Theme.TextBox("搜索实验名称");
            searchBox.Location = new Point(10, 8);
            searchBox.Width = 360;
            searchBox.Height = 22;
            searchBox.AutoSize = false;
            searchBox.BorderStyle = BorderStyle.None;
            searchBox.BackColor = Theme.Panel2;
            searchBox.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Theme.SetCueBanner(searchBox, "搜索实验名称");
            DarkPanel searchShell = new DarkPanel { Location = new Point(0, 30), Width = 380, Height = 36, Radius = 9, BackColor = Theme.Panel2, Padding = new Padding(0), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            searchShell.Controls.Add(searchBox);
            statusFilter = Theme.Button("全部状态   ▾", false);
            statusFilter.Location = new Point(0, 75); statusFilter.Size = new Size(145, 34); statusFilter.AutoSize = false;
            ContextMenuStrip statusMenu = new ContextMenuStrip { BackColor = Theme.Panel, ForeColor = Theme.Text, ShowImageMargin = false, Font = Theme.Font(8.8f, FontStyle.Regular) };
            string[] statusNames = { "全部状态", "运行中", "已完成", "疑似卡住", "失败" };
            for (int statusIndex = 0; statusIndex < statusNames.Length; statusIndex++)
            {
                int selectedIndex = statusIndex;
                ToolStripMenuItem item = new ToolStripMenuItem(statusNames[statusIndex]);
                item.Padding = new Padding(12, 7, 24, 7);
                item.Click += delegate { statusFilterIndex = selectedIndex; statusFilter.Text = statusNames[selectedIndex] + "   ▾"; PopulateGrid(); };
                statusMenu.Items.Add(item);
            }
            statusFilter.Click += delegate { statusMenu.Show(statusFilter, new Point(0, statusFilter.Height + 2)); };
            favoriteOnly = new CheckBox { Text = "只看收藏", ForeColor = Theme.Text, AutoSize = true, Location = new Point(163, 77), BackColor = Theme.Panel };
            filters.Controls.Add(libraryTitle);
            filters.Controls.Add(libraryHint);
            filters.Controls.Add(searchShell);
            filters.Controls.Add(statusFilter);
            filters.Controls.Add(favoriteOnly);

            grid = new DataGridView();
            ConfigureGrid(grid);
            grid.Dock = DockStyle.Fill;
            DataGridViewCheckBoxColumn select = new DataGridViewCheckBoxColumn { Name = "Compare", HeaderText = "对比", Width = 46 };
            grid.Columns.Add(select);
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Favorite", HeaderText = "", Width = 28 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "实验", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, MinimumWidth = 155 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Status", HeaderText = "状态", Width = 72 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Epoch", HeaderText = "Epoch", Width = 74 });
            grid.CellValueChanged += GridCellValueChanged;
            grid.CurrentCellDirtyStateChanged += delegate { if (grid.IsCurrentCellDirty) grid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            grid.SelectionChanged += GridSelectionChanged;

            Panel listActions = new Panel { Dock = DockStyle.Bottom, Height = 60, BackColor = Theme.Panel };
            compareButton = Theme.Button("对比已选实验", true);
            compareButton.Location = new Point(0, 14);
            compareButton.Height = 38;
            compareButton.Click += OnCompare;
            Label compareHint = Theme.Label("选择 2–5 条", 8, FontStyle.Regular, Theme.Muted);
            compareHint.Location = new Point(154, 25);
            listActions.Controls.Add(compareButton);
            listActions.Controls.Add(compareHint);
            listPanel.Controls.Add(grid);
            listPanel.Controls.Add(filters);
            listPanel.Controls.Add(listActions);
            split.Panel1.Controls.Add(listPanel);

            TableLayoutPanel detailRoot = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Theme.Back, RowCount = 2, ColumnCount = 1, Margin = new Padding(0), Padding = new Padding(0) };
            detailRoot.RowStyles.Add(new RowStyle(SizeType.Absolute, 88));
            detailRoot.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            TableLayoutPanel cards = new TableLayoutPanel { Dock = DockStyle.Top, Height = 88, ColumnCount = 3, BackColor = Theme.Back, Padding = new Padding(0, 0, 0, 10) };
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
            runningCard = AddCard(cards, 0, "运行中", Theme.Cyan);
            completedCard = AddCard(cards, 1, "已完成", Theme.Green);
            stalledCard = AddCard(cards, 2, "需要关注", Theme.Amber);

            TableLayoutPanel tabContainer = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Theme.Back, RowCount = 2, ColumnCount = 1, Margin = new Padding(0), Padding = new Padding(0) };
            tabContainer.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
            tabContainer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            FlowLayoutPanel tabBar = new FlowLayoutPanel { Dock = DockStyle.Fill, BackColor = Theme.Panel2, Padding = new Padding(4, 4, 0, 4), WrapContents = false };
            Panel tabHost = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Back };
            Panel overview = NewTab();
            Panel metrics = NewTab();
            Panel logs = NewTab();
            Panel manage = NewTab();
            Panel[] pages = { overview, metrics, logs, manage };
            string[] pageNames = { "概览", "指标趋势", "实时日志", "管理" };
            Button[] pageButtons = new Button[pages.Length];
            Action<int> selectPage = delegate(int selected)
            {
                for (int i = 0; i < pages.Length; i++)
                {
                    pages[i].Visible = i == selected;
                    pageButtons[i].BackColor = i == selected ? Color.FromArgb(40, 73, 116) : Theme.Panel2;
                    pageButtons[i].FlatAppearance.BorderColor = i == selected ? Theme.Blue : Theme.Line;
                    RoundButton round = pageButtons[i] as RoundButton;
                    if (round != null) round.BorderColor = i == selected ? Theme.Blue : Theme.Line;
                }
            };
            for (int i = 0; i < pages.Length; i++)
            {
                int pageIndex = i;
                pages[i].Dock = DockStyle.Fill;
                tabHost.Controls.Add(pages[i]);
                pageButtons[i] = Theme.Button(pageNames[i], false);
                pageButtons[i].Width = 112;
                pageButtons[i].Height = 32;
                pageButtons[i].Margin = new Padding(0, 0, 4, 0);
                pageButtons[i].Click += delegate { selectPage(pageIndex); };
                tabBar.Controls.Add(pageButtons[i]);
            }
            tabContainer.Controls.Add(tabBar, 0, 0);
            tabContainer.Controls.Add(tabHost, 0, 1);
            selectPage(0);
            detailRoot.Controls.Add(cards, 0, 0);
            detailRoot.Controls.Add(tabContainer, 0, 1);
            split.Panel2.Controls.Add(detailRoot);

            DarkPanel hero = new DarkPanel { Dock = DockStyle.Top, Height = 174, Padding = new Padding(18), Radius = 17, ShowAccent = true, AccentColor = Theme.Blue };
            selectedTitle = Theme.Label("选择一条实验查看详情", 14, FontStyle.Bold, Theme.Text);
            selectedTitle.Location = new Point(18, 16);
            selectedTitle.AutoSize = false;
            selectedTitle.Size = new Size(680, 34);
            selectedTitle.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            selectedTitle.AutoEllipsis = true;
            selectedStatus = Theme.Label("—", 9, FontStyle.Bold, Theme.Muted);
            selectedStatus.Location = new Point(20, 60);
            progress = new ModernProgress { Location = new Point(20, 91), Height = 11, Width = 690, Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top, Maximum = 1000, AccentColor = Theme.Cyan };
            TableLayoutPanel heroStats = new TableLayoutPanel { Location = new Point(16, 112), Height = 46, Width = 700, Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top, ColumnCount = 3 };
            heroStats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33));
            heroStats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33));
            heroStats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 34));
            epochValue = AddHeroStat(heroStats, 0, "Epoch");
            batchValue = AddHeroStat(heroStats, 1, "Batch");
            timeValue = AddHeroStat(heroStats, 2, "耗时 / ETA");
            hero.Controls.Add(selectedTitle);
            hero.Controls.Add(selectedStatus);
            hero.Controls.Add(progress);
            hero.Controls.Add(heroStats);
            SplitContainer overviewSplit = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Vertical, BackColor = Theme.Line, Padding = new Padding(0, 10, 0, 0), SplitterWidth = 10 };
            DarkPanel metricPanel = new DarkPanel { Dock = DockStyle.Fill, Radius = 16, ShowAccent = true, AccentColor = Theme.Cyan };
            DarkPanel hostPanel = new DarkPanel { Dock = DockStyle.Fill, Radius = 16, ShowAccent = true, AccentColor = Theme.Purple };
            metricPanel.Controls.Add(Theme.Label("最新指标", 10, FontStyle.Bold, Theme.Text));
            metricsText = Theme.Label("尚无指标", 9, FontStyle.Regular, Theme.Muted);
            metricsText.Location = new Point(14, 48);
            metricsText.MaximumSize = new Size(355, 1000);
            metricPanel.Controls.Add(metricsText);
            hostPanel.Controls.Add(Theme.Label("GPU / 主机状态", 10, FontStyle.Bold, Theme.Text));
            hostText = new RichTextBox { Text = "训练端尚未上报硬件状态", Location = new Point(16, 46), ReadOnly = true, BorderStyle = BorderStyle.None, BackColor = Theme.Panel, ForeColor = Theme.Muted, Font = Theme.Font(8.8f, FontStyle.Regular), WordWrap = true, DetectUrls = false, ScrollBars = RichTextBoxScrollBars.Vertical, Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right };
            hostPanel.Resize += delegate { hostText.Size = new Size(Math.Max(40, hostPanel.ClientSize.Width - 31), Math.Max(40, hostPanel.ClientSize.Height - 60)); };
            hostPanel.Controls.Add(hostText);
            overviewSplit.Panel1.Controls.Add(metricPanel);
            overviewSplit.Panel2.Controls.Add(hostPanel);
            overviewSplit.Resize += delegate
            {
                int width = overviewSplit.ClientSize.Width;
                if (width < 620) return;
                int desired = (int)(width * .66);
                desired = Math.Max(320, Math.Min(desired, width - 290));
                if (desired > 0 && desired < width - overviewSplit.SplitterWidth)
                    overviewSplit.SplitterDistance = desired;
            };
            TableLayoutPanel overviewLayout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, Margin = new Padding(0), Padding = new Padding(0), BackColor = Theme.Back };
            overviewLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 174));
            overviewLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            overviewLayout.Controls.Add(hero, 0, 0);
            overviewLayout.Controls.Add(overviewSplit, 0, 1);
            overview.Controls.Add(overviewLayout);

            Panel chartToolbar = new Panel { Dock = DockStyle.Top, Height = 55, BackColor = Theme.Panel };
            chartToolbar.Controls.Add(Theme.Label("指标", 9, FontStyle.Bold, Theme.Muted));
            metricSelector = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Theme.Panel2, ForeColor = Theme.Text, FlatStyle = FlatStyle.Flat, Location = new Point(52, 8), Width = 310 };
            Theme.StyleComboBox(metricSelector);
            metricSelector.SelectedIndexChanged += delegate { RenderMetricChart(); };
            bestText = Theme.Label("选择实验后显示最佳 Epoch", 8.5f, FontStyle.Regular, Theme.Muted);
            bestText.Location = new Point(380, 12);
            chartToolbar.Controls.Add(metricSelector);
            chartToolbar.Controls.Add(bestText);
            chart = new TrendChart { Dock = DockStyle.Fill, BackColor = Theme.Panel2 };
            metrics.Controls.Add(chart);
            metrics.Controls.Add(chartToolbar);

            logText = new RichTextBox { Dock = DockStyle.Fill, ReadOnly = true, BackColor = Color.FromArgb(8, 14, 27), ForeColor = Color.FromArgb(195, 215, 238), BorderStyle = BorderStyle.None, Font = new Font("Consolas", 9.5f), WordWrap = false };
            logs.Padding = new Padding(12);
            logs.Controls.Add(logText);

            TableLayoutPanel manageLayout = new TableLayoutPanel { Dock = DockStyle.Top, Height = 335, Padding = new Padding(18), ColumnCount = 2, RowCount = 6 };
            manageLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 130));
            manageLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 45));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
            manageLayout.RowStyles.Add(new RowStyle(SizeType.Absolute, 55));
            groupBox = Theme.TextBox("实验分组"); groupBox.Dock = DockStyle.Fill;
            tagsBox = Theme.TextBox("标签，逗号分隔"); tagsBox.Dock = DockStyle.Fill;
            favoriteBox = new CheckBox { Text = "收藏这条实验", ForeColor = Theme.Text, AutoSize = true, BackColor = Theme.Back };
            saveButton = Theme.Button("保存分组 / 标签 / 收藏", true);
            notesButton = Theme.Button("编辑实验假设 / 结论 / 下一步", false);
            deleteButton = Theme.Button("永久删除实验", false);
            deleteButton.BackColor = Color.FromArgb(129, 39, 57);
            deleteButton.FlatAppearance.BorderColor = Theme.Red;
            saveButton.Click += OnSaveMetadata;
            notesButton.Click += OnEditNotes;
            deleteButton.Click += OnDelete;
            manageLayout.Controls.Add(Theme.Label("实验分组", 9, FontStyle.Bold, Theme.Muted), 0, 0); manageLayout.Controls.Add(groupBox, 1, 0);
            manageLayout.Controls.Add(Theme.Label("实验标签", 9, FontStyle.Bold, Theme.Muted), 0, 1); manageLayout.Controls.Add(tagsBox, 1, 1);
            manageLayout.Controls.Add(Theme.Label("重要实验", 9, FontStyle.Bold, Theme.Muted), 0, 2); manageLayout.Controls.Add(favoriteBox, 1, 2);
            manageLayout.Controls.Add(Theme.Label("实验笔记", 9, FontStyle.Bold, Theme.Muted), 0, 3); manageLayout.Controls.Add(notesButton, 1, 3);
            manageLayout.Controls.Add(Theme.Label("管理操作", 9, FontStyle.Bold, Theme.Muted), 0, 4); manageLayout.Controls.Add(saveButton, 1, 4);
            manageLayout.Controls.Add(Theme.Label("危险区域", 9, FontStyle.Bold, Theme.Red), 0, 5); manageLayout.Controls.Add(deleteButton, 1, 5);
            manage.Controls.Add(manageLayout);
            Label securityHint = Theme.Label("浏览无需密码；保存、收藏和删除会在操作时单独要求管理员密码，密码不会保存在本机。", 8.5f, FontStyle.Regular, Theme.Muted);
            securityHint.Location = new Point(22, 355);
            manage.Controls.Add(securityHint);

            searchBox.TextChanged += delegate { PopulateGrid(); };
            favoriteOnly.CheckedChanged += delegate { PopulateGrid(); };
            if (state != null)
            {
                state.Updated += StateUpdated;
                Shown += async delegate { await state.RefreshAsync(); };
            }
            FormClosed += delegate { if (state != null) state.Updated -= StateUpdated; };
            SetManagementEnabled(false);
        }

        private static void ConfigureGrid(DataGridView view)
        {
            view.AllowUserToAddRows = false;
            view.AllowUserToDeleteRows = false;
            view.AllowUserToResizeRows = false;
            view.MultiSelect = false;
            view.RowHeadersVisible = false;
            view.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
            view.BackgroundColor = Theme.Panel;
            view.BorderStyle = BorderStyle.None;
            view.GridColor = Theme.Line;
            view.CellBorderStyle = DataGridViewCellBorderStyle.None;
            view.ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None;
            view.ColumnHeadersHeight = 38;
            view.EnableHeadersVisualStyles = false;
            view.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle { BackColor = Theme.Panel2, ForeColor = Theme.Muted, Font = Theme.Font(8.2f, FontStyle.Bold), SelectionBackColor = Theme.Panel2, Padding = new Padding(4, 0, 4, 0) };
            view.DefaultCellStyle = new DataGridViewCellStyle { BackColor = Theme.Panel, ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(29, 43, 70), SelectionForeColor = Color.White, Padding = new Padding(5, 4, 5, 4) };
            view.AlternatingRowsDefaultCellStyle = new DataGridViewCellStyle { BackColor = Color.FromArgb(13, 21, 38), ForeColor = Theme.Text, SelectionBackColor = Color.FromArgb(29, 43, 70), SelectionForeColor = Color.White };
            view.RowTemplate.Height = 52;
            view.RowTemplate.DividerHeight = 1;
        }

        private void DragWindow(object sender, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left) return;
            ReleaseCapture();
            SendMessage(Handle, 0xA1, (IntPtr)2, IntPtr.Zero);
        }

        protected override void WndProc(ref Message message)
        {
            const int WM_NCHITTEST = 0x84;
            if (message.Msg == WM_NCHITTEST && WindowState == FormWindowState.Normal)
            {
                Point screen = new Point((short)(message.LParam.ToInt64() & 0xffff), (short)((message.LParam.ToInt64() >> 16) & 0xffff));
                Point point = PointToClient(screen);
                int grip = 8;
                if (point.X <= grip && point.Y <= grip) { message.Result = (IntPtr)13; return; }
                if (point.X >= ClientSize.Width - grip && point.Y <= grip) { message.Result = (IntPtr)14; return; }
                if (point.X <= grip && point.Y >= ClientSize.Height - grip) { message.Result = (IntPtr)16; return; }
                if (point.X >= ClientSize.Width - grip && point.Y >= ClientSize.Height - grip) { message.Result = (IntPtr)17; return; }
                if (point.X <= grip) { message.Result = (IntPtr)10; return; }
                if (point.X >= ClientSize.Width - grip) { message.Result = (IntPtr)11; return; }
                if (point.Y <= grip) { message.Result = (IntPtr)12; return; }
                if (point.Y >= ClientSize.Height - grip) { message.Result = (IntPtr)15; return; }
            }
            base.WndProc(ref message);
        }

        private static Panel NewTab() { return new Panel { BackColor = Theme.Back, ForeColor = Theme.Text, Padding = new Padding(10), Visible = false }; }

        private static Label AddCard(TableLayoutPanel cards, int column, string caption, Color color)
        {
            DarkPanel panel = new DarkPanel { Dock = DockStyle.Fill, Margin = new Padding(column == 0 ? 0 : 6, 0, column == 2 ? 0 : 6, 0), Padding = new Padding(16, 10, 16, 8), Radius = 15, ShowAccent = true, AccentColor = color };
            Label label = Theme.Label("0", 18, FontStyle.Bold, color);
            label.Location = new Point(21, 9);
            Label name = Theme.Label(caption, 8.5f, FontStyle.Regular, Theme.Muted);
            name.Location = new Point(23, 46);
            panel.Controls.Add(label); panel.Controls.Add(name); cards.Controls.Add(panel, column, 0);
            return label;
        }

        private static Label AddHeroStat(TableLayoutPanel layout, int column, string caption)
        {
            Panel panel = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Panel };
            Label value = Theme.Label("—", 10, FontStyle.Bold, Theme.Text); value.Location = new Point(4, 0);
            Label label = Theme.Label(caption, 7.5f, FontStyle.Regular, Theme.Muted); label.Location = new Point(4, 23);
            panel.Controls.Add(value); panel.Controls.Add(label); layout.Controls.Add(panel, column, 0); return value;
        }

        private void StateUpdated(object sender, EventArgs e)
        {
            if (IsDisposed) return;
            if (InvokeRequired) { BeginInvoke(new Action(delegate { StateUpdated(sender, e); })); return; }
            UpdateConnectionAndCards();
            PopulateGrid();
            if (selectedRun != null) LoadSelectedDetailAsync(Json.Text(selectedRun, "id"));
        }

        private void UpdateConnectionAndCards()
        {
            if (state == null) return;
            connectionLabel.Text = state.Error == null ? "● 实时连接正常 · " + state.LastSuccess.ToString("HH:mm:ss") : "● 连接失败 · 正在重试";
            connectionLabel.ForeColor = state.Error == null ? Theme.Green : Theme.Red;
            runningCard.Text = state.Runs.Count(delegate(Dictionary<string, object> r) { return Json.Text(r, "status") == "running"; }).ToString();
            completedCard.Text = state.Runs.Count(delegate(Dictionary<string, object> r) { return Json.Text(r, "status") == "completed"; }).ToString();
            stalledCard.Text = state.Runs.Count(delegate(Dictionary<string, object> r) { string s = Json.Text(r, "status"); return s == "stalled" || s == "failed"; }).ToString();
        }

        private string SelectedStatusFilter()
        {
            if (statusFilterIndex == 1) return "running";
            if (statusFilterIndex == 2) return "completed";
            if (statusFilterIndex == 3) return "stalled";
            if (statusFilterIndex == 4) return "failed";
            return "";
        }

        private void PopulateGrid()
        {
            if (state == null) return;
            string selectedId = selectedRun == null ? "" : Json.Text(selectedRun, "id");
            string query = searchBox.Text.Trim();
            string status = SelectedStatusFilter();
            IEnumerable<Dictionary<string, object>> runs = state.Runs.Where(delegate(Dictionary<string, object> run)
            {
                return (query.Length == 0 || Json.Text(run, "name").IndexOf(query, StringComparison.CurrentCultureIgnoreCase) >= 0)
                    && (status.Length == 0 || Json.Text(run, "status") == status)
                    && (!favoriteOnly.Checked || Json.Bool(run, "favorite"));
            });
            populatingGrid = true;
            grid.Rows.Clear();
            int selectRow = -1;
            foreach (Dictionary<string, object> run in runs)
            {
                string id = Json.Text(run, "id");
                int index = grid.Rows.Add(comparisonIds.Contains(id), Json.Bool(run, "favorite") ? "★" : "☆", Json.Text(run, "name", "未命名实验"), Theme.StatusText(Json.Text(run, "status")), string.Format("{0}/{1}", Json.Int(run, "current_epoch"), Json.Int(run, "total_epochs")));
                grid.Rows[index].Tag = run;
                grid.Rows[index].Cells[3].Style.ForeColor = Theme.StatusColor(Json.Text(run, "status"));
                if (id == selectedId) selectRow = index;
            }
            if (selectRow >= 0) grid.Rows[selectRow].Selected = true;
            populatingGrid = false;
        }

        private void GridCellValueChanged(object sender, DataGridViewCellEventArgs e)
        {
            if (populatingGrid || e.RowIndex < 0 || e.ColumnIndex != 0) return;
            Dictionary<string, object> run = grid.Rows[e.RowIndex].Tag as Dictionary<string, object>;
            if (run == null) return;
            bool selected = Convert.ToBoolean(grid.Rows[e.RowIndex].Cells[0].Value ?? false);
            string id = Json.Text(run, "id");
            if (selected) comparisonIds.Add(id); else comparisonIds.Remove(id);
            compareButton.Text = comparisonIds.Count == 0 ? "对比已选实验" : "对比已选实验（" + comparisonIds.Count + "）";
        }

        private void GridSelectionChanged(object sender, EventArgs e)
        {
            if (populatingGrid || grid.SelectedRows.Count == 0) return;
            Dictionary<string, object> run = grid.SelectedRows[0].Tag as Dictionary<string, object>;
            if (run == null) return;
            string oldId = selectedRun == null ? "" : Json.Text(selectedRun, "id");
            selectedRun = run;
            RenderRunSummary(run);
            if (oldId != Json.Text(run, "id")) LoadSelectedDetailAsync(Json.Text(run, "id"));
        }

        private void RenderRunSummary(Dictionary<string, object> run)
        {
            selectedTitle.Text = Json.Text(run, "name", "未命名实验");
            string status = Json.Text(run, "status");
            selectedStatus.Text = "● " + Theme.StatusText(status) + (Json.Text(run, "group_name").Length > 0 ? "   ·   " + Json.Text(run, "group_name") : "");
            selectedStatus.ForeColor = Theme.StatusColor(status);
            int epoch = Json.Int(run, "current_epoch"), total = Json.Int(run, "total_epochs");
            int batch = Json.Int(run, "current_batch"), totalBatch = Json.Int(run, "total_batches");
            progress.Value = total > 0 ? Math.Min(1000, Math.Max(0, epoch * 1000 / total)) : 0;
            epochValue.Text = string.Format("{0} / {1}", epoch, total);
            batchValue.Text = string.Format("{0} / {1}", batch, totalBatch);
            timeValue.Text = Duration(Json.Number(run, "elapsed_seconds", 0)) + " / " + Duration(Json.Number(run, "eta_seconds", 0));
            metricsText.Text = FormatDictionary(Json.Child(run, "latest_metrics"), 16, "尚无指标");
            hostText.Text = FormatHost(Json.Child(run, "host_status"));
            logText.Text = Json.Text(run, "log_tail", "训练端尚未上报日志。");
            groupBox.Text = Json.Text(run, "group_name");
            tagsBox.Text = string.Join(", ", Json.Children(run, "tags").Select(Convert.ToString).ToArray());
            favoriteBox.Checked = Json.Bool(run, "favorite");
            SetManagementEnabled(true);
        }

        private async void LoadSelectedDetailAsync(string runId)
        {
            if (state == null || loadingDetail) return;
            loadingDetail = true;
            try
            {
                Dictionary<string, object> detail = await state.Api.GetRunAsync(runId);
                if (selectedRun != null && Json.Text(selectedRun, "id") == runId)
                {
                    selectedDetail = detail;
                    PopulateMetricSelector();
                    RenderBest();
                }
            }
            catch (Exception ex) { bestText.Text = "详情读取失败：" + ex.Message; }
            finally { loadingDetail = false; }
        }

        private void PopulateMetricSelector()
        {
            if (selectedDetail == null) return;
            string selected = metricSelector.SelectedItem == null ? "" : metricSelector.SelectedItem.ToString();
            List<string> keys = new List<string>();
            foreach (object item in Json.Children(selectedDetail, "events"))
            {
                Dictionary<string, object> metrics = Json.Child(Json.Dict(item), "metrics");
                foreach (string key in metrics.Keys) if (!keys.Contains(key) && IsNumber(metrics[key])) keys.Add(key);
            }
            metricSelector.Items.Clear();
            foreach (string key in keys) metricSelector.Items.Add(key);
            int index = keys.IndexOf(selected);
            if (metricSelector.Items.Count > 0) metricSelector.SelectedIndex = index >= 0 ? index : PreferredMetricIndex(keys);
            else chart.SetSeries(new List<ChartSeries>());
        }

        private static int PreferredMetricIndex(List<string> keys)
        {
            string[] priorities = { "map50-95", "map50", "precision", "recall", "loss" };
            foreach (string priority in priorities)
                for (int i = 0; i < keys.Count; i++) if (keys[i].ToLowerInvariant().Replace("_", "-").Contains(priority)) return i;
            return 0;
        }

        private void RenderMetricChart()
        {
            if (selectedDetail == null || metricSelector.SelectedItem == null) return;
            string key = metricSelector.SelectedItem.ToString();
            ChartSeries series = new ChartSeries { Name = Json.Text(Json.Child(selectedDetail, "run"), "name", "实验"), Color = Theme.Cyan, Points = new List<ChartPoint>() };
            foreach (object item in Json.Children(selectedDetail, "events"))
            {
                Dictionary<string, object> evt = Json.Dict(item);
                Dictionary<string, object> values = Json.Child(evt, "metrics");
                object value;
                if (values.TryGetValue(key, out value) && IsNumber(value)) series.Points.Add(new ChartPoint(Json.Int(evt, "epoch"), Convert.ToDouble(value, CultureInfo.InvariantCulture)));
            }
            chart.SetSeries(new List<ChartSeries> { series });
        }

        private void RenderBest()
        {
            Dictionary<string, object> best = Json.Child(selectedDetail, "best");
            if (best.Count == 0) { bestText.Text = "当前指标暂无可用的 Best Epoch"; return; }
            bestText.Text = string.Format("Best Epoch {0} · {1} = {2:0.######}", Json.Int(best, "epoch"), Json.Text(best, "metric_key"), Json.Number(best, "value", 0));
        }

        private async void OnSaveMetadata(object sender, EventArgs e)
        {
            if (selectedRun == null || state == null) return;
            string password = PasswordDialog.Ask(this, "保存管理信息", "请输入管理员密码以保存分组、标签和收藏：");
            if (password == null) return;
            try
            {
                saveButton.Enabled = false;
                await state.Api.SaveMetadataAsync(Json.Text(selectedRun, "id"), groupBox.Text, tagsBox.Text, favoriteBox.Checked, password);
                await state.RefreshAsync();
                MessageBox.Show(this, "实验管理信息已保存。", "保存成功", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            catch (AdminPasswordException) { MessageBox.Show(this, "管理员密码错误，未保存任何修改。", "保存失败", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "保存失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            finally { saveButton.Enabled = true; }
        }

        private async void OnEditNotes(object sender, EventArgs e)
        {
            if (selectedRun == null || selectedDetail == null || state == null) return;
            Dictionary<string, object> run = Json.Child(selectedDetail, "run");
            using (ExperimentNotesDialog dialog = new ExperimentNotesDialog(run, Json.Text(Json.Child(selectedDetail, "report"), "config_diff")))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                string password = PasswordDialog.Ask(this, "保存实验笔记", "请输入管理员密码以保存实验假设、结论和下一步：");
                if (password == null) return;
                try
                {
                    notesButton.Enabled = false;
                    await state.Api.SaveExperimentNotesAsync(Json.Text(selectedRun, "id"), dialog.BuildPayload(), password);
                    selectedDetail = await state.Api.GetRunAsync(Json.Text(selectedRun, "id"));
                    PopulateMetricSelector();
                    RenderBest();
                    MessageBox.Show(this, "实验笔记已保存。", "保存成功", MessageBoxButtons.OK, MessageBoxIcon.Information);
                }
                catch (AdminPasswordException) { MessageBox.Show(this, "管理员密码错误，笔记未保存。", "保存失败", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
                catch (Exception ex) { MessageBox.Show(this, ex.Message, "保存失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
                finally { notesButton.Enabled = true; }
            }
        }

        private async void OnDelete(object sender, EventArgs e)
        {
            if (selectedRun == null || state == null) return;
            string name = Json.Text(selectedRun, "name");
            if (MessageBox.Show(this, "将永久删除实验及其全部指标：\n\n" + name + "\n\n此操作无法恢复。", "二次确认删除", MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return;
            string password = PasswordDialog.Ask(this, "确认永久删除", "请输入管理员密码：");
            if (password == null) return;
            try
            {
                deleteButton.Enabled = false;
                await state.Api.DeleteRunAsync(Json.Text(selectedRun, "id"), password);
                selectedRun = null; selectedDetail = null; ClearDetails();
                await state.RefreshAsync();
            }
            catch (AdminPasswordException) { MessageBox.Show(this, "管理员密码错误，实验记录未删除。", "删除失败", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "删除失败", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            finally { deleteButton.Enabled = true; }
        }

        private async void OnCompare(object sender, EventArgs e)
        {
            if (state == null) return;
            if (comparisonIds.Count < 2 || comparisonIds.Count > 5)
            {
                MessageBox.Show(this, "请选择 2–5 条实验进行对比。", "实验对比", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            ComparisonForm form = new ComparisonForm(state.Api, comparisonIds.ToList());
            form.Show(this);
            await form.LoadDataAsync();
        }

        private async void OnServerSettings(object sender, EventArgs e)
        {
            if (state == null) return;
            string value = TextPromptDialog.Ask(this, "服务器设置", "监控服务器地址（HTTPS）：", settings.ServerUrl);
            if (value == null) return;
            value = value.Trim().TrimEnd('/');
            if (!Uri.IsWellFormedUriString(value, UriKind.Absolute) || !value.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                MessageBox.Show(this, "请输入完整的 HTTPS 地址。", "地址无效", MessageBoxButtons.OK, MessageBoxIcon.Warning); return;
            }
            settings.ServerUrl = value; settings.Save(); state.ChangeServer(value); await state.RefreshAsync();
        }

        private void SetManagementEnabled(bool enabled) { groupBox.Enabled = enabled; tagsBox.Enabled = enabled; favoriteBox.Enabled = enabled; notesButton.Enabled = enabled; saveButton.Enabled = enabled; deleteButton.Enabled = enabled; }

        private void ClearDetails()
        {
            selectedTitle.Text = "选择一条实验查看详情"; selectedStatus.Text = "—"; epochValue.Text = batchValue.Text = timeValue.Text = "—"; progress.Value = 0;
            metricsText.Text = "尚无指标"; hostText.Text = "尚无硬件状态"; logText.Text = ""; groupBox.Text = tagsBox.Text = ""; favoriteBox.Checked = false;
            metricSelector.Items.Clear(); chart.SetSeries(new List<ChartSeries>()); bestText.Text = "选择实验后显示最佳 Epoch"; SetManagementEnabled(false);
        }

        private static bool IsNumber(object value)
        {
            return value is byte || value is short || value is int || value is long || value is float || value is double || value is decimal;
        }

        private static string Duration(double seconds)
        {
            if (seconds <= 0) return "—";
            TimeSpan span = TimeSpan.FromSeconds(seconds);
            return span.TotalHours >= 1 ? string.Format("{0}h {1}m", (int)span.TotalHours, span.Minutes) : string.Format("{0}m {1}s", span.Minutes, span.Seconds);
        }

        private static string FormatDictionary(Dictionary<string, object> values, int limit, string empty)
        {
            if (values.Count == 0) return empty;
            return string.Join(Environment.NewLine, values.Take(limit).Select(delegate(KeyValuePair<string, object> pair)
            {
                double number; string text = pair.Value == null ? "—" : Convert.ToString(pair.Value, CultureInfo.InvariantCulture);
                if (double.TryParse(text, NumberStyles.Any, CultureInfo.InvariantCulture, out number)) text = number.ToString("0.######", CultureInfo.InvariantCulture);
                return pair.Key + "   " + text;
            }).ToArray());
        }

        private static string FormatHost(Dictionary<string, object> host)
        {
            if (host.Count == 0) return "训练端尚未上报硬件状态";
            List<string> lines = new List<string>();
            lines.Add("主机   " + Json.Text(host, "hostname", "—"));
            Dictionary<string, object> load = Json.Child(host, "load");
            if (load.Count > 0) lines.Add("CPU   " + Json.Number(load, "per_cpu_percent", 0).ToString("0.0") + "%（1 分钟负载 / 核）");
            Dictionary<string, object> memory = Json.Child(host, "memory");
            if (memory.Count > 0)
            {
                lines.Add(string.Format("内存   {0:0} / {1:0} MiB   {2:0.0}%", Json.Number(memory, "used_mb", 0), Json.Number(memory, "total_mb", 0), Json.Number(memory, "used_percent", 0)));
            }
            object rawGpus;
            object[] gpus = host.TryGetValue("gpus", out rawGpus) ? Json.Array(rawGpus) : new object[0];
            if (gpus.Length == 0) lines.Add("GPU   未读取到 NVIDIA GPU 状态");
            foreach (object raw in gpus)
            {
                Dictionary<string, object> gpu = Json.Dict(raw);
                lines.Add(string.Format("GPU {0}   {1}", Json.Text(gpu, "index", "—"), Json.Text(gpu, "name", "—")));
                lines.Add(string.Format("  利用率 {0:0}%   显存 {1:0}/{2:0} MiB   {3:0}°C", Json.Number(gpu, "utilization_percent", 0), Json.Number(gpu, "memory_used_mb", 0), Json.Number(gpu, "memory_total_mb", 0), Json.Number(gpu, "temperature_c", 0)));
            }
            if (Json.Text(host, "sampled_at").Length > 0) lines.Add("采样   " + Json.Text(host, "sampled_at"));
            return string.Join(Environment.NewLine, lines.ToArray());
        }
    }

    internal sealed class PasswordDialog : Form
    {
        private readonly TextBox password;
        private PasswordDialog(string title, string message)
        {
            Text = title; Icon = Theme.CreateAppIcon(); ClientSize = new Size(430, 170); FormBorderStyle = FormBorderStyle.FixedDialog; StartPosition = FormStartPosition.CenterParent; MaximizeBox = false; MinimizeBox = false; BackColor = Theme.Back; ForeColor = Theme.Text;
            Label label = Theme.Label(message, 9, FontStyle.Regular, Theme.Text); label.Location = new Point(20, 18); label.MaximumSize = new Size(390, 45);
            password = Theme.TextBox(""); password.UseSystemPasswordChar = true; password.Location = new Point(20, 73); password.Width = 390;
            Button ok = Theme.Button("确认", true); ok.Location = new Point(236, 116); ok.DialogResult = DialogResult.OK;
            Button cancel = Theme.Button("取消", false); cancel.Location = new Point(330, 116); cancel.DialogResult = DialogResult.Cancel;
            Controls.Add(label); Controls.Add(password); Controls.Add(ok); Controls.Add(cancel); AcceptButton = ok; CancelButton = cancel;
        }
        public static string Ask(IWin32Window owner, string title, string message) { using (PasswordDialog form = new PasswordDialog(title, message)) return form.ShowDialog(owner) == DialogResult.OK ? form.password.Text : null; }
    }

    internal sealed class TextPromptDialog : Form
    {
        private readonly TextBox valueBox;
        private TextPromptDialog(string title, string message, string value)
        {
            Text = title; Icon = Theme.CreateAppIcon(); ClientSize = new Size(540, 170); FormBorderStyle = FormBorderStyle.FixedDialog; StartPosition = FormStartPosition.CenterParent; MaximizeBox = false; MinimizeBox = false; BackColor = Theme.Back; ForeColor = Theme.Text;
            Label label = Theme.Label(message, 9, FontStyle.Regular, Theme.Text); label.Location = new Point(20, 20);
            valueBox = Theme.TextBox(""); valueBox.Location = new Point(20, 60); valueBox.Width = 500; valueBox.Text = value;
            Button ok = Theme.Button("保存并连接", true); ok.Location = new Point(336, 112); ok.DialogResult = DialogResult.OK;
            Button cancel = Theme.Button("取消", false); cancel.Location = new Point(440, 112); cancel.DialogResult = DialogResult.Cancel;
            Controls.Add(label); Controls.Add(valueBox); Controls.Add(ok); Controls.Add(cancel); AcceptButton = ok; CancelButton = cancel;
        }
        public static string Ask(IWin32Window owner, string title, string message, string value) { using (TextPromptDialog form = new TextPromptDialog(title, message, value)) return form.ShowDialog(owner) == DialogResult.OK ? form.valueBox.Text : null; }
    }
}
