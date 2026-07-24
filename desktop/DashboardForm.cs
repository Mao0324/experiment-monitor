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
        private readonly HashSet<string> comparisonIds = new HashSet<string>();
        private DataGridView grid;
        private TextBox searchBox;
        private Button statusFilter;
        private int statusFilterIndex;
        private CheckBox favoriteOnly;
        private Label connectionLabel;
        private Label runningCard;
        private Label completedCard;
        private Label stalledCard;
        private Label selectedTitle;
        private Label selectedStatus;
        private StatusPill selectedStatusPill;
        private Label epochValue;
        private Label batchValue;
        private Label timeValue;
        private ModernProgress progress;
        private Label metricsText;
        private RichTextBox hostText;
        private RichTextBox logText;
        private TextBox groupBox;
        private TextBox tagsBox;
        private CheckBox favoriteBox;
        private ComboBox metricSelector;
        private TrendChart chart;
        private Label bestText;
        private Button saveButton;
        private Button deleteButton;
        private Button compareButton;
        private Button updateButton;
        private Button notesButton;
        private Dictionary<string, object> selectedRun;
        private Dictionary<string, object> selectedDetail;
        private bool loadingDetail;
        private bool populatingGrid;

        public event EventHandler VisualStyleChanged;

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
            Text = "Epoch Studio · YOLO 实验监控";
            Icon = Theme.CreateAppIcon();
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.None;
            MinimumSize = new Size(1120, 700);
            ClientSize = new Size(1380, 840);
            BackColor = Theme.Back;
            ForeColor = Theme.Text;
            Font = Theme.Font(9, FontStyle.Regular);

            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.RowCount = 2;
            root.ColumnCount = 1;
            root.Margin = new Padding(0);
            root.Padding = new Padding(0);
            root.BackColor = Theme.Back;
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 78));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            root.Controls.Add(BuildHeader(), 0, 0);
            root.Controls.Add(BuildWorkspace(), 0, 1);
            Controls.Add(root);

            Resize += delegate
            {
                if (WindowState == FormWindowState.Normal)
                {
                    using (GraphicsPath path = Theme.RoundRect(new Rectangle(0, 0, Width, Height), Theme.Mode == VisualStyleMode.Clay ? 18 : 10))
                        Region = new Region(path);
                }
                else Region = null;
            };

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

        private Control BuildHeader()
        {
            Panel header = new Panel();
            header.Dock = DockStyle.Fill;
            header.BackColor = Theme.Panel2;
            header.Padding = new Padding(24, 0, 12, 0);
            header.MouseDown += DragWindow;
            header.DoubleClick += delegate { ToggleMaximize(); };

            Label mark = Theme.Label("Y", 13, FontStyle.Bold, Color.White);
            mark.TextAlign = ContentAlignment.MiddleCenter;
            mark.AutoSize = false;
            mark.Size = new Size(42, 42);
            mark.Location = new Point(24, 18);
            mark.BackColor = Theme.Blue;
            mark.MouseDown += DragWindow;

            Label title = Theme.Label("EPOCH STUDIO", 14.5f, FontStyle.Bold, Theme.Text);
            title.Location = new Point(79, 15);
            title.MouseDown += DragWindow;
            Label subtitle = Theme.Label("YOLO EXPERIMENT OPERATIONS", 7.2f, FontStyle.Bold, Theme.Muted);
            subtitle.Location = new Point(81, 42);
            subtitle.MouseDown += DragWindow;
            connectionLabel = Theme.Label("● 正在连接", 8.2f, FontStyle.Regular, Theme.Muted);
            connectionLabel.Location = new Point(287, 29);

            FlowLayoutPanel actions = new FlowLayoutPanel();
            actions.Dock = DockStyle.Right;
            actions.Width = 770;
            actions.Height = 78;
            actions.Padding = new Padding(0, 18, 0, 0);
            actions.FlowDirection = FlowDirection.RightToLeft;
            actions.WrapContents = false;
            actions.BackColor = Theme.Panel2;

            Button close = HeaderButton("×", 40);
            Button maximize = HeaderButton("□", 40);
            Button minimize = HeaderButton("—", 40);
            close.ForeColor = Theme.Red;
            close.Click += delegate { Close(); };
            maximize.Click += delegate { ToggleMaximize(); };
            minimize.Click += delegate { WindowState = FormWindowState.Minimized; };

            Button style = HeaderButton("风格 · " + Theme.ModeDisplayName + "  ▾", 138);
            ContextMenuStrip styleMenu = BuildStyleMenu();
            style.Click += delegate { styleMenu.Show(style, new Point(0, style.Height + 5)); };

            Button server = HeaderButton("服务器", 82);
            server.Click += OnServerSettings;
            updateButton = HeaderButton("检查更新", 92);
            updateButton.Click += async delegate
            {
                if (state == null) return;
                updateButton.Enabled = false;
                await UpdateWorkflow.CheckAndPromptAsync(this, settings.ServerUrl, false, delegate(string text) { updateButton.Text = text; });
                updateButton.Enabled = true;
            };
            Button refresh = HeaderButton("刷新", 72);
            refresh.Click += async delegate { if (state != null) await state.RefreshAsync(); };
            Button queue = Theme.Button("实验队列", true);
            queue.AutoSize = false;
            queue.Size = new Size(96, 40);
            queue.Margin = new Padding(6, 0, 6, 0);
            queue.Click += delegate { if (state != null) new QueueForm(state.Api).Show(this); };

            actions.Controls.Add(close);
            actions.Controls.Add(maximize);
            actions.Controls.Add(minimize);
            actions.Controls.Add(style);
            actions.Controls.Add(server);
            actions.Controls.Add(updateButton);
            actions.Controls.Add(refresh);
            actions.Controls.Add(queue);

            header.Controls.Add(actions);
            header.Controls.Add(mark);
            header.Controls.Add(title);
            header.Controls.Add(subtitle);
            header.Controls.Add(connectionLabel);
            return header;
        }

        private Button HeaderButton(string text, int width)
        {
            Button button = Theme.Button(text, false);
            button.AutoSize = false;
            button.Size = new Size(width, 40);
            button.Margin = new Padding(3, 0, 3, 0);
            return button;
        }

        private ContextMenuStrip BuildStyleMenu()
        {
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.BackColor = Theme.Panel;
            menu.ForeColor = Theme.Text;
            menu.ShowImageMargin = false;
            menu.Font = Theme.Font(9, FontStyle.Regular);
            AddStyleItem(menu, "Material Design", "material");
            AddStyleItem(menu, "Claymorphism", "clay");
            AddStyleItem(menu, "Elegant", "elegant");
            return menu;
        }

        private void AddStyleItem(ContextMenuStrip menu, string text, string key)
        {
            ToolStripMenuItem item = new ToolStripMenuItem(text);
            item.Checked = Theme.ModeKey == key;
            item.Padding = new Padding(14, 8, 24, 8);
            item.Click += delegate
            {
                settings.VisualStyle = key;
                settings.Save();
                Theme.Use(key);
                EventHandler handler = VisualStyleChanged;
                if (handler != null) handler(this, EventArgs.Empty);
            };
            menu.Items.Add(item);
        }

        private Control BuildWorkspace()
        {
            SplitContainer split = new SplitContainer();
            split.Dock = DockStyle.Fill;
            split.BackColor = Theme.Back;
            split.SplitterWidth = 10;
            split.Panel1.BackColor = Theme.Back;
            split.Panel2.BackColor = Theme.Back;
            split.Panel1.Padding = new Padding(18, 14, 5, 18);
            split.Panel2.Padding = new Padding(5, 14, 18, 18);
            split.Panel1.Controls.Add(BuildLibrary());
            split.Panel2.Controls.Add(BuildDetails());
            Shown += delegate
            {
                int desired = Math.Max(330, Math.Min(410, (int)(split.ClientSize.Width * .29)));
                if (desired < split.ClientSize.Width - 640) split.SplitterDistance = desired;
            };
            return split;
        }

        private Control BuildLibrary()
        {
            DarkPanel panel = new DarkPanel();
            panel.Dock = DockStyle.Fill;
            panel.Padding = new Padding(18);
            panel.Radius = Theme.CardRadius;

            Panel filters = new Panel();
            filters.Dock = DockStyle.Top;
            filters.Height = 148;
            filters.BackColor = Theme.Panel;

            Label eyebrow = Theme.Label("EXPERIMENT LIBRARY", 7.2f, FontStyle.Bold, Theme.Blue);
            eyebrow.Location = new Point(1, 0);
            Label title = Theme.Label("实验库", 13, FontStyle.Bold, Theme.Text);
            title.Location = new Point(0, 18);

            DarkPanel searchShell = new DarkPanel();
            searchShell.Location = new Point(0, 52);
            searchShell.Height = 42;
            searchShell.Width = 340;
            searchShell.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            searchShell.Radius = Theme.ButtonRadius;
            searchShell.BackColor = Theme.Input;
            searchShell.Padding = new Padding(12, 9, 12, 8);
            searchBox = Theme.TextBox("");
            searchBox.BorderStyle = BorderStyle.None;
            searchBox.Dock = DockStyle.Fill;
            searchBox.BackColor = Theme.Input;
            Theme.SetCueBanner(searchBox, "搜索实验名称");
            searchShell.Controls.Add(searchBox);

            statusFilter = Theme.Button("全部状态  ▾", false);
            statusFilter.AutoSize = false;
            statusFilter.Size = new Size(138, 38);
            statusFilter.Location = new Point(0, 105);
            ContextMenuStrip statusMenu = new ContextMenuStrip();
            statusMenu.BackColor = Theme.Panel;
            statusMenu.ForeColor = Theme.Text;
            statusMenu.ShowImageMargin = false;
            statusMenu.Font = Theme.Font(8.8f, FontStyle.Regular);
            string[] statusNames = { "全部状态", "运行中", "已完成", "疑似卡住", "失败" };
            for (int i = 0; i < statusNames.Length; i++)
            {
                int index = i;
                ToolStripMenuItem item = new ToolStripMenuItem(statusNames[i]);
                item.Padding = new Padding(12, 7, 24, 7);
                item.Click += delegate
                {
                    statusFilterIndex = index;
                    statusFilter.Text = statusNames[index] + "  ▾";
                    PopulateGrid();
                };
                statusMenu.Items.Add(item);
            }
            statusFilter.Click += delegate { statusMenu.Show(statusFilter, new Point(0, statusFilter.Height + 4)); };
            favoriteOnly = new CheckBox();
            favoriteOnly.Text = "只看收藏";
            favoriteOnly.ForeColor = Theme.Text;
            favoriteOnly.BackColor = Theme.Panel;
            favoriteOnly.AutoSize = true;
            favoriteOnly.Location = new Point(158, 115);

            filters.Controls.Add(eyebrow);
            filters.Controls.Add(title);
            filters.Controls.Add(searchShell);
            filters.Controls.Add(statusFilter);
            filters.Controls.Add(favoriteOnly);

            grid = new DataGridView();
            ConfigureGrid(grid);
            grid.Dock = DockStyle.Fill;
            grid.ScrollBars = ScrollBars.Vertical;
            grid.Columns.Add(new DataGridViewCheckBoxColumn { Name = "Compare", HeaderText = "选", Width = 32 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Favorite", HeaderText = "", Width = 22 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "实验", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, MinimumWidth = 92 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Status", HeaderText = "状态", Width = 64 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Epoch", HeaderText = "Epoch", Width = 61 });
            grid.CellValueChanged += GridCellValueChanged;
            grid.CurrentCellDirtyStateChanged += delegate { if (grid.IsCurrentCellDirty) grid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
            grid.SelectionChanged += GridSelectionChanged;

            Panel footer = new Panel();
            footer.Dock = DockStyle.Bottom;
            footer.Height = 68;
            footer.BackColor = Theme.Panel;
            compareButton = Theme.Button("对比已选实验", true);
            compareButton.AutoSize = false;
            compareButton.Size = new Size(148, 42);
            compareButton.Location = new Point(0, 15);
            compareButton.Click += OnCompare;
            Label hint = Theme.Label("选择 2–5 条实验", 7.8f, FontStyle.Regular, Theme.Muted);
            hint.Location = new Point(162, 28);
            footer.Controls.Add(compareButton);
            footer.Controls.Add(hint);

            panel.Controls.Add(grid);
            panel.Controls.Add(filters);
            panel.Controls.Add(footer);
            return panel;
        }

        private Control BuildDetails()
        {
            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.RowCount = 2;
            root.ColumnCount = 1;
            root.Margin = new Padding(0);
            root.Padding = new Padding(0);
            root.BackColor = Theme.Back;
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 104));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            TableLayoutPanel cards = new TableLayoutPanel();
            cards.Dock = DockStyle.Fill;
            cards.ColumnCount = 3;
            cards.BackColor = Theme.Back;
            cards.Padding = new Padding(0, 0, 0, 10);
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.334f));
            runningCard = AddCard(cards, 0, "RUNNING", "运行中", Theme.Cyan);
            completedCard = AddCard(cards, 1, "COMPLETED", "已完成", Theme.Green);
            stalledCard = AddCard(cards, 2, "ATTENTION", "需要关注", Theme.Amber);

            TableLayoutPanel tabs = new TableLayoutPanel();
            tabs.Dock = DockStyle.Fill;
            tabs.RowCount = 2;
            tabs.ColumnCount = 1;
            tabs.Margin = new Padding(0);
            tabs.Padding = new Padding(0);
            tabs.BackColor = Theme.Back;
            tabs.RowStyles.Add(new RowStyle(SizeType.Absolute, 50));
            tabs.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            FlowLayoutPanel tabBar = new FlowLayoutPanel();
            tabBar.Dock = DockStyle.Fill;
            tabBar.BackColor = Theme.Back;
            tabBar.Padding = new Padding(0, 4, 0, 5);
            tabBar.WrapContents = false;
            Panel tabHost = new Panel();
            tabHost.Dock = DockStyle.Fill;
            tabHost.BackColor = Theme.Back;

            Panel overview = NewTab();
            Panel metrics = NewTab();
            Panel logs = NewTab();
            Panel manage = NewTab();
            BuildOverview(overview);
            BuildMetrics(metrics);
            BuildLogs(logs);
            BuildManage(manage);
            Panel[] pages = { overview, metrics, logs, manage };
            string[] names = { "概览", "指标趋势", "实时日志", "实验管理" };
            Button[] buttons = new Button[pages.Length];
            Action<int> select = delegate(int selected)
            {
                for (int i = 0; i < pages.Length; i++)
                {
                    bool active = i == selected;
                    pages[i].Visible = active;
                    buttons[i].BackColor = active ? Theme.Blue : Theme.Raised;
                    buttons[i].ForeColor = active ? Color.White : Theme.Muted;
                    RoundButton round = buttons[i] as RoundButton;
                    if (round != null)
                    {
                        round.Primary = active;
                        round.BorderColor = active ? Theme.Blue : Theme.Line;
                    }
                }
            };
            for (int i = 0; i < pages.Length; i++)
            {
                int index = i;
                pages[i].Dock = DockStyle.Fill;
                tabHost.Controls.Add(pages[i]);
                buttons[i] = Theme.Button(names[i], false);
                buttons[i].AutoSize = false;
                buttons[i].Size = new Size(120, 40);
                buttons[i].Margin = new Padding(0, 0, 8, 0);
                buttons[i].Click += delegate { select(index); };
                tabBar.Controls.Add(buttons[i]);
            }
            select(0);
            tabs.Controls.Add(tabBar, 0, 0);
            tabs.Controls.Add(tabHost, 0, 1);
            root.Controls.Add(cards, 0, 0);
            root.Controls.Add(tabs, 0, 1);
            return root;
        }

        private void BuildOverview(Panel page)
        {
            DarkPanel hero = new DarkPanel();
            hero.Dock = DockStyle.Top;
            hero.Height = 194;
            hero.Padding = new Padding(22);
            hero.ShowAccent = true;
            hero.AccentColor = Theme.Blue;

            Label eyebrow = Theme.Label("SELECTED EXPERIMENT", 7.2f, FontStyle.Bold, Theme.Blue);
            eyebrow.Location = new Point(22, 22);
            selectedTitle = Theme.Label("选择一条实验查看详情", 15, FontStyle.Bold, Theme.Text);
            selectedTitle.AutoSize = false;
            selectedTitle.Location = new Point(22, 44);
            selectedTitle.Size = new Size(690, 34);
            selectedTitle.Anchor = AnchorStyles.Top | AnchorStyles.Left;
            selectedTitle.AutoEllipsis = true;
            selectedStatus = Theme.Label("从左侧实验库开始", 8.5f, FontStyle.Regular, Theme.Muted);
            selectedStatus.Location = new Point(23, 79);
            selectedStatusPill = new StatusPill();
            selectedStatusPill.Location = new Point(715, 29);
            selectedStatusPill.Anchor = AnchorStyles.Top | AnchorStyles.Left;
            selectedStatusPill.Status = "";
            progress = new ModernProgress();
            progress.Location = new Point(23, 108);
            progress.Width = 780;
            progress.Height = 12;
            progress.Anchor = AnchorStyles.Top | AnchorStyles.Left;
            progress.Maximum = 1000;
            progress.AccentColor = Theme.Cyan;

            TableLayoutPanel stats = new TableLayoutPanel();
            stats.Location = new Point(18, 134);
            stats.Height = 48;
            stats.Width = 795;
            stats.Anchor = AnchorStyles.Top | AnchorStyles.Left;
            stats.ColumnCount = 3;
            stats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.33f));
            stats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.33f));
            stats.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.34f));
            epochValue = AddHeroStat(stats, 0, "EPOCH");
            batchValue = AddHeroStat(stats, 1, "BATCH");
            timeValue = AddHeroStat(stats, 2, "ELAPSED / ETA");

            hero.Controls.Add(eyebrow);
            hero.Controls.Add(selectedTitle);
            hero.Controls.Add(selectedStatus);
            hero.Controls.Add(selectedStatusPill);
            hero.Controls.Add(progress);
            hero.Controls.Add(stats);
            hero.Resize += delegate
            {
                int innerWidth = Math.Max(280, hero.ClientSize.Width - 46);
                selectedStatusPill.Left = Math.Max(24, hero.ClientSize.Width - selectedStatusPill.Width - 24);
                selectedTitle.Width = Math.Max(180, innerWidth - selectedStatusPill.Width - 22);
                progress.Width = innerWidth;
                stats.Width = innerWidth + 10;
            };

            SplitContainer lower = new SplitContainer();
            lower.Dock = DockStyle.Fill;
            lower.Orientation = Orientation.Vertical;
            lower.BackColor = Theme.Back;
            lower.Padding = new Padding(0, 12, 0, 0);
            lower.SplitterWidth = 12;

            DarkPanel metricPanel = SectionPanel("LATEST METRICS", "最新训练与验证指标", Theme.Cyan);
            metricsText = Theme.Label("尚无指标", 9, FontStyle.Regular, Theme.Muted);
            metricsText.Location = new Point(22, 70);
            metricsText.MaximumSize = new Size(540, 1000);
            metricPanel.Controls.Add(metricsText);

            DarkPanel hostPanel = SectionPanel("HARDWARE TELEMETRY", "GPU / 主机状态", Theme.Purple);
            hostText = new RichTextBox();
            hostText.Text = "训练端尚未上报硬件状态";
            hostText.Location = new Point(22, 70);
            hostText.ReadOnly = true;
            hostText.BorderStyle = BorderStyle.None;
            hostText.BackColor = Theme.Panel;
            hostText.ForeColor = Theme.Muted;
            hostText.Font = Theme.Font(8.8f, FontStyle.Regular);
            hostText.WordWrap = true;
            hostText.DetectUrls = false;
            hostText.ScrollBars = RichTextBoxScrollBars.Vertical;
            hostText.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            hostPanel.Resize += delegate { hostText.Size = new Size(Math.Max(40, hostPanel.ClientSize.Width - 44), Math.Max(40, hostPanel.ClientSize.Height - 92)); };
            hostPanel.Controls.Add(hostText);
            lower.Panel1.Controls.Add(metricPanel);
            lower.Panel2.Controls.Add(hostPanel);
            lower.Resize += delegate
            {
                int width = lower.ClientSize.Width;
                if (width < 650) return;
                int desired = Math.Max(330, Math.Min((int)(width * .61), width - 300));
                if (desired > 0 && desired < width - lower.SplitterWidth) lower.SplitterDistance = desired;
            };

            page.Controls.Add(lower);
            page.Controls.Add(hero);
        }

        private void BuildMetrics(Panel page)
        {
            DarkPanel panel = SectionPanel("EPOCH ANALYTICS", "指标趋势与最佳 Epoch", Theme.Cyan);
            panel.Dock = DockStyle.Fill;
            Panel toolbar = new Panel();
            toolbar.Dock = DockStyle.Top;
            toolbar.Height = 74;
            toolbar.BackColor = Theme.Panel;
            metricSelector = new ComboBox();
            metricSelector.DropDownStyle = ComboBoxStyle.DropDownList;
            metricSelector.Location = new Point(22, 35);
            metricSelector.Width = 330;
            Theme.StyleComboBox(metricSelector);
            metricSelector.SelectedIndexChanged += delegate { RenderMetricChart(); };
            bestText = Theme.Label("选择实验后显示最佳 Epoch", 8.5f, FontStyle.Regular, Theme.Muted);
            bestText.Location = new Point(375, 39);
            toolbar.Controls.Add(metricSelector);
            toolbar.Controls.Add(bestText);
            chart = new TrendChart();
            chart.Dock = DockStyle.Fill;
            chart.BackColor = Theme.Panel;
            chart.Padding = new Padding(12);
            panel.Controls.Add(chart);
            panel.Controls.Add(toolbar);
            page.Controls.Add(panel);
        }

        private void BuildLogs(Panel page)
        {
            DarkPanel panel = SectionPanel("LIVE OUTPUT", "训练日志尾部", Theme.Green);
            panel.Dock = DockStyle.Fill;
            Panel spacer = new Panel { Dock = DockStyle.Top, Height = 65, BackColor = Theme.Panel };
            logText = new RichTextBox();
            logText.Dock = DockStyle.Fill;
            logText.ReadOnly = true;
            logText.BackColor = Theme.Input;
            logText.ForeColor = Theme.Mode == VisualStyleMode.Clay ? Theme.Text : Color.FromArgb(197, 216, 236);
            logText.BorderStyle = BorderStyle.None;
            logText.Font = new Font("Consolas", 9.5f);
            logText.WordWrap = false;
            logText.Margin = new Padding(20);
            Panel logShell = new Panel { Dock = DockStyle.Fill, Padding = new Padding(20, 0, 20, 20), BackColor = Theme.Panel };
            logShell.Controls.Add(logText);
            panel.Controls.Add(logShell);
            panel.Controls.Add(spacer);
            page.Controls.Add(panel);
        }

        private void BuildManage(Panel page)
        {
            DarkPanel panel = SectionPanel("EXPERIMENT MANAGEMENT", "分组、标签、笔记和危险操作", Theme.Amber);
            panel.Dock = DockStyle.Fill;
            TableLayoutPanel form = new TableLayoutPanel();
            form.Dock = DockStyle.Top;
            form.Height = 355;
            form.Padding = new Padding(22, 68, 22, 10);
            form.BackColor = Theme.Panel;
            form.ColumnCount = 2;
            form.RowCount = 6;
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 145));
            form.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
            form.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));

            groupBox = Theme.TextBox("实验分组");
            tagsBox = Theme.TextBox("标签，逗号分隔");
            groupBox.Dock = DockStyle.Fill;
            tagsBox.Dock = DockStyle.Fill;
            favoriteBox = new CheckBox { Text = "收藏这条实验", ForeColor = Theme.Text, AutoSize = true, BackColor = Theme.Panel };
            notesButton = Theme.Button("编辑实验假设 / 结论 / 下一步", false);
            saveButton = Theme.Button("保存管理信息", true);
            deleteButton = Theme.Button("永久删除实验", false);
            deleteButton.BackColor = Theme.Red;
            deleteButton.ForeColor = Color.White;
            RoundButton deleteRound = deleteButton as RoundButton;
            if (deleteRound != null) deleteRound.BorderColor = Theme.Red;
            saveButton.Click += OnSaveMetadata;
            notesButton.Click += OnEditNotes;
            deleteButton.Click += OnDelete;

            AddManageRow(form, 0, "实验分组", groupBox, Theme.Muted);
            AddManageRow(form, 1, "实验标签", tagsBox, Theme.Muted);
            AddManageRow(form, 2, "重要实验", favoriteBox, Theme.Muted);
            AddManageRow(form, 3, "实验笔记", notesButton, Theme.Muted);
            AddManageRow(form, 4, "保存修改", saveButton, Theme.Muted);
            AddManageRow(form, 5, "危险区域", deleteButton, Theme.Red);
            Label security = Theme.Label("浏览无需密码；写入和删除操作会单独验证管理员密码，密码不会保存到本机。", 8.3f, FontStyle.Regular, Theme.Muted);
            security.Location = new Point(23, 371);
            panel.Controls.Add(security);
            panel.Controls.Add(form);
            page.Controls.Add(panel);
        }

        private static void AddManageRow(TableLayoutPanel form, int row, string text, Control control, Color color)
        {
            Label label = Theme.Label(text, 8.8f, FontStyle.Bold, color);
            label.Dock = DockStyle.Fill;
            label.TextAlign = ContentAlignment.MiddleLeft;
            control.Dock = DockStyle.Fill;
            control.Margin = new Padding(4, 5, 4, 5);
            form.Controls.Add(label, 0, row);
            form.Controls.Add(control, 1, row);
        }

        private static DarkPanel SectionPanel(string eyebrowText, string titleText, Color accent)
        {
            DarkPanel panel = new DarkPanel();
            panel.Dock = DockStyle.Fill;
            panel.ShowAccent = true;
            panel.AccentColor = accent;
            Label eyebrow = Theme.Label(eyebrowText, 7.1f, FontStyle.Bold, accent);
            eyebrow.Location = new Point(22, 24);
            Label title = Theme.Label(titleText, 11, FontStyle.Bold, Theme.Text);
            title.Location = new Point(21, 43);
            panel.Controls.Add(eyebrow);
            panel.Controls.Add(title);
            return panel;
        }

        private static Panel NewTab()
        {
            return new Panel { BackColor = Theme.Back, ForeColor = Theme.Text, Padding = new Padding(0), Visible = false };
        }

        private static Label AddCard(TableLayoutPanel cards, int column, string eyebrow, string caption, Color color)
        {
            DarkPanel panel = new DarkPanel();
            panel.Dock = DockStyle.Fill;
            panel.Margin = new Padding(column == 0 ? 0 : 6, 0, column == 2 ? 0 : 6, 0);
            panel.ShowAccent = true;
            panel.AccentColor = color;
            Label small = Theme.Label(eyebrow, 7.1f, FontStyle.Bold, color);
            small.Location = new Point(20, 22);
            Label value = Theme.Label("0", 18, FontStyle.Bold, Theme.Text);
            value.Location = new Point(18, 42);
            Label name = Theme.Label(caption, 8, FontStyle.Regular, Theme.Muted);
            name.Location = new Point(62, 51);
            panel.Controls.Add(small);
            panel.Controls.Add(value);
            panel.Controls.Add(name);
            cards.Controls.Add(panel, column, 0);
            return value;
        }

        private static Label AddHeroStat(TableLayoutPanel layout, int column, string caption)
        {
            Panel panel = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Panel };
            Label value = Theme.Label("—", 10, FontStyle.Bold, Theme.Text);
            value.Location = new Point(5, 0);
            Label label = Theme.Label(caption, 7, FontStyle.Bold, Theme.Muted);
            label.Location = new Point(5, 25);
            panel.Controls.Add(value);
            panel.Controls.Add(label);
            layout.Controls.Add(panel, column, 0);
            return value;
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
            view.ColumnHeadersHeight = 42;
            view.EnableHeadersVisualStyles = false;
            view.ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = Theme.Panel,
                ForeColor = Theme.Muted,
                Font = Theme.Font(7.8f, FontStyle.Bold),
                SelectionBackColor = Theme.Panel,
                Padding = new Padding(5, 0, 5, 0)
            };
            view.DefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = Theme.Panel,
                ForeColor = Theme.Text,
                SelectionBackColor = Theme.Selection,
                SelectionForeColor = Theme.Text,
                Padding = new Padding(6, 5, 6, 5)
            };
            view.AlternatingRowsDefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = Theme.Panel2,
                ForeColor = Theme.Text,
                SelectionBackColor = Theme.Selection,
                SelectionForeColor = Theme.Text
            };
            view.RowTemplate.Height = 56;
            view.RowTemplate.DividerHeight = 1;
        }

        private void ToggleMaximize()
        {
            WindowState = WindowState == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;
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
            int selectedRow = -1;
            foreach (Dictionary<string, object> run in runs)
            {
                string id = Json.Text(run, "id");
                int index = grid.Rows.Add(comparisonIds.Contains(id), Json.Bool(run, "favorite") ? "★" : "☆", Json.Text(run, "name", "未命名实验"), Theme.StatusText(Json.Text(run, "status")), string.Format("{0}/{1}", Json.Int(run, "current_epoch"), Json.Int(run, "total_epochs")));
                grid.Rows[index].Tag = run;
                grid.Rows[index].Cells[3].Style.ForeColor = Theme.StatusColor(Json.Text(run, "status"));
                if (id == selectedId) selectedRow = index;
            }
            if (selectedRow >= 0) grid.Rows[selectedRow].Selected = true;
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
            selectedStatusPill.Status = status;
            selectedStatus.Text = Json.Text(run, "group_name").Length > 0 ? "分组 · " + Json.Text(run, "group_name") : "尚未设置实验分组";
            selectedStatus.ForeColor = Theme.Muted;
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
                Dictionary<string, object> values = Json.Child(Json.Dict(item), "metrics");
                foreach (string key in values.Keys) if (!keys.Contains(key) && IsNumber(values[key])) keys.Add(key);
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
                if (values.TryGetValue(key, out value) && IsNumber(value))
                    series.Points.Add(new ChartPoint(Json.Int(evt, "epoch"), Convert.ToDouble(value, CultureInfo.InvariantCulture)));
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
                selectedRun = null;
                selectedDetail = null;
                ClearDetails();
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
                MessageBox.Show(this, "请输入完整的 HTTPS 地址。", "地址无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            settings.ServerUrl = value;
            settings.Save();
            state.ChangeServer(value);
            await state.RefreshAsync();
        }

        private void SetManagementEnabled(bool enabled)
        {
            groupBox.Enabled = enabled;
            tagsBox.Enabled = enabled;
            favoriteBox.Enabled = enabled;
            notesButton.Enabled = enabled;
            saveButton.Enabled = enabled;
            deleteButton.Enabled = enabled;
        }

        private void ClearDetails()
        {
            selectedTitle.Text = "选择一条实验查看详情";
            selectedStatus.Text = "从左侧实验库开始";
            selectedStatusPill.Status = "";
            epochValue.Text = batchValue.Text = timeValue.Text = "—";
            progress.Value = 0;
            metricsText.Text = "尚无指标";
            hostText.Text = "尚无硬件状态";
            logText.Text = "";
            groupBox.Text = tagsBox.Text = "";
            favoriteBox.Checked = false;
            metricSelector.Items.Clear();
            chart.SetSeries(new List<ChartSeries>());
            bestText.Text = "选择实验后显示最佳 Epoch";
            SetManagementEnabled(false);
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
                double number;
                string text = pair.Value == null ? "—" : Convert.ToString(pair.Value, CultureInfo.InvariantCulture);
                if (double.TryParse(text, NumberStyles.Any, CultureInfo.InvariantCulture, out number))
                    text = number.ToString("0.######", CultureInfo.InvariantCulture);
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
                lines.Add(string.Format("内存   {0:0} / {1:0} MiB   {2:0.0}%", Json.Number(memory, "used_mb", 0), Json.Number(memory, "total_mb", 0), Json.Number(memory, "used_percent", 0)));
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
            Text = title;
            Icon = Theme.CreateAppIcon();
            ClientSize = new Size(450, 210);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MaximizeBox = false;
            MinimizeBox = false;
            BackColor = Theme.Back;
            ForeColor = Theme.Text;
            Font = Theme.Font(9, FontStyle.Regular);
            DarkPanel card = new DarkPanel { Dock = DockStyle.Fill, Margin = new Padding(16), Padding = new Padding(22) };
            Label eyebrow = Theme.Label("ADMIN VERIFICATION", 7.1f, FontStyle.Bold, Theme.Blue);
            eyebrow.Location = new Point(22, 23);
            Label label = Theme.Label(message, 9, FontStyle.Regular, Theme.Text);
            label.Location = new Point(22, 49);
            label.MaximumSize = new Size(390, 42);
            password = Theme.TextBox("");
            password.UseSystemPasswordChar = true;
            password.Location = new Point(22, 98);
            password.Width = 390;
            Button ok = Theme.Button("确认", true);
            ok.AutoSize = false;
            ok.Size = new Size(92, 40);
            ok.Location = new Point(222, 151);
            ok.DialogResult = DialogResult.OK;
            Button cancel = Theme.Button("取消", false);
            cancel.AutoSize = false;
            cancel.Size = new Size(92, 40);
            cancel.Location = new Point(320, 151);
            cancel.DialogResult = DialogResult.Cancel;
            card.Controls.Add(eyebrow);
            card.Controls.Add(label);
            card.Controls.Add(password);
            card.Controls.Add(ok);
            card.Controls.Add(cancel);
            Controls.Add(card);
            AcceptButton = ok;
            CancelButton = cancel;
        }

        public static string Ask(IWin32Window owner, string title, string message)
        {
            using (PasswordDialog form = new PasswordDialog(title, message))
                return form.ShowDialog(owner) == DialogResult.OK ? form.password.Text : null;
        }
    }

    internal sealed class TextPromptDialog : Form
    {
        private readonly TextBox valueBox;

        private TextPromptDialog(string title, string message, string value)
        {
            Text = title;
            Icon = Theme.CreateAppIcon();
            ClientSize = new Size(560, 220);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MaximizeBox = false;
            MinimizeBox = false;
            BackColor = Theme.Back;
            ForeColor = Theme.Text;
            Font = Theme.Font(9, FontStyle.Regular);
            DarkPanel card = new DarkPanel { Dock = DockStyle.Fill, Padding = new Padding(22) };
            Label eyebrow = Theme.Label("CONNECTION SETTINGS", 7.1f, FontStyle.Bold, Theme.Blue);
            eyebrow.Location = new Point(22, 24);
            Label label = Theme.Label(message, 9, FontStyle.Regular, Theme.Text);
            label.Location = new Point(22, 51);
            valueBox = Theme.TextBox("");
            valueBox.Location = new Point(22, 87);
            valueBox.Width = 500;
            valueBox.Text = value;
            Button ok = Theme.Button("保存并连接", true);
            ok.AutoSize = false;
            ok.Size = new Size(128, 40);
            ok.Location = new Point(296, 151);
            ok.DialogResult = DialogResult.OK;
            Button cancel = Theme.Button("取消", false);
            cancel.AutoSize = false;
            cancel.Size = new Size(92, 40);
            cancel.Location = new Point(430, 151);
            cancel.DialogResult = DialogResult.Cancel;
            card.Controls.Add(eyebrow);
            card.Controls.Add(label);
            card.Controls.Add(valueBox);
            card.Controls.Add(ok);
            card.Controls.Add(cancel);
            Controls.Add(card);
            AcceptButton = ok;
            CancelButton = cancel;
        }

        public static string Ask(IWin32Window owner, string title, string message, string value)
        {
            using (TextPromptDialog form = new TextPromptDialog(title, message, value))
                return form.ShowDialog(owner) == DialogResult.OK ? form.valueBox.Text : null;
        }
    }
}
