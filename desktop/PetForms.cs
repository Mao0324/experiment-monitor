using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class HoverCardForm : Form
    {
        private readonly Label statusLabel;
        private readonly Label nameLabel;
        private readonly Label epochLabel;
        private readonly Label detailLabel;
        private readonly ModernProgress progress;

        protected override bool ShowWithoutActivation { get { return true; } }

        public HoverCardForm()
        {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            BackColor = Theme.Panel;
            ClientSize = new Size(380, 184);
            Padding = new Padding(20, 16, 20, 16);
            statusLabel = Theme.Label("等待连接", 8.5f, FontStyle.Bold, Theme.Cyan);
            nameLabel = Theme.Label("Epoch 精灵", 12.5f, FontStyle.Bold, Theme.Text);
            nameLabel.MaximumSize = new Size(340, 44);
            epochLabel = Theme.Label("尚无正在运行的实验", 10f, FontStyle.Regular, Theme.Text);
            detailLabel = Theme.Label("鼠标单击打开实验面板", 8.8f, FontStyle.Regular, Theme.Muted);
            progress = new ModernProgress();
            progress.Height = 9;
            progress.Width = 340;
            progress.Maximum = 1000;

            FlowLayoutPanel layout = new FlowLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.FlowDirection = FlowDirection.TopDown;
            layout.WrapContents = false;
            layout.Controls.Add(statusLabel);
            layout.Controls.Add(nameLabel);
            layout.Controls.Add(epochLabel);
            layout.Controls.Add(progress);
            layout.Controls.Add(detailLabel);
            Controls.Add(layout);
            Resize += delegate
            {
                using (GraphicsPath path = Theme.RoundRect(new Rectangle(0, 0, Width, Height), 16)) Region = new Region(path);
            };
            Paint += delegate(object sender, PaintEventArgs e)
            {
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (GraphicsPath path = Theme.RoundRect(new Rectangle(0, 0, Width - 1, Height - 1), 16))
                using (Pen pen = new Pen(Theme.Line)) e.Graphics.DrawPath(pen, path);
            };
        }

        public void UpdateRun(Dictionary<string, object> run, string error)
        {
            if (!string.IsNullOrEmpty(error))
            {
                statusLabel.Text = "● 连接中断";
                statusLabel.ForeColor = Theme.Red;
                nameLabel.Text = "无法连接监控服务器";
                epochLabel.Text = error;
                progress.Value = 0;
                detailLabel.Text = "程序会自动重试 · 单击打开设置";
                return;
            }
            if (run == null)
            {
                statusLabel.Text = "● 在线";
                statusLabel.ForeColor = Theme.Green;
                nameLabel.Text = "当前没有实验记录";
                epochLabel.Text = "等待训练端开始上报";
                progress.Value = 0;
                detailLabel.Text = "单击打开实验面板";
                return;
            }
            string status = Json.Text(run, "status");
            int epoch = Json.Int(run, "current_epoch");
            int total = Json.Int(run, "total_epochs");
            int batch = Json.Int(run, "current_batch");
            int totalBatch = Json.Int(run, "total_batches");
            double percent = total > 0 ? Math.Min(1.0, Math.Max(0.0, (double)epoch / total)) : 0;
            statusLabel.Text = "● " + Theme.StatusText(status);
            statusLabel.ForeColor = Theme.StatusColor(status);
            nameLabel.Text = Json.Text(run, "name", "未命名实验");
            epochLabel.Text = string.Format("Epoch {0}/{1}   Batch {2}/{3}", epoch, total, batch, totalBatch);
            progress.Value = Math.Max(0, Math.Min(1000, (int)(percent * 1000)));
            double eta = Json.Number(run, "eta_seconds", 0);
            detailLabel.Text = "进度 " + (percent * 100).ToString("0.0") + "%" + (eta > 0 ? "  ·  ETA " + ShortDuration(eta) : "") + "  ·  单击查看详情";
        }

        private static string ShortDuration(double seconds)
        {
            TimeSpan span = TimeSpan.FromSeconds(Math.Max(0, seconds));
            return span.TotalHours >= 1 ? string.Format("{0}h {1}m", (int)span.TotalHours, span.Minutes) : string.Format("{0}m", Math.Max(1, span.Minutes));
        }
    }

    internal sealed class PetForm : Form
    {
        private readonly MonitorState state;
        private readonly AppSettings settings;
        private readonly HoverCardForm hover;
        private readonly Timer pollTimer;
        private readonly Timer animationTimer;
        private readonly Timer hoverHideTimer;
        private readonly NotifyIcon tray;
        private DashboardForm dashboard;
        private int frame;
        private bool dragging;
        private bool moved;
        private Point dragMouse;
        private Point dragForm;

        public PetForm(MonitorState state, AppSettings settings)
        {
            this.state = state;
            this.settings = settings;
            ClientSize = new Size(158, 172);
            FormBorderStyle = FormBorderStyle.None;
            BackColor = Color.FromArgb(1, 2, 3);
            TransparencyKey = Color.FromArgb(1, 2, 3);
            ShowInTaskbar = false;
            TopMost = true;
            DoubleBuffered = true;
            Cursor = Cursors.Hand;
            StartPosition = FormStartPosition.Manual;
            Icon = Theme.CreateAppIcon();

            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            Location = settings.HasPosition ? new Point(settings.Left, settings.Top) : new Point(area.Right - Width - 22, area.Bottom - Height - 20);
            EnsureVisible();

            hover = new HoverCardForm();
            hover.MouseEnter += delegate { hoverHideTimer.Stop(); };
            hover.MouseLeave += delegate { hoverHideTimer.Start(); };

            pollTimer = new Timer();
            pollTimer.Interval = 3000;
            pollTimer.Tick += async delegate { await state.RefreshAsync(); };
            pollTimer.Start();

            animationTimer = new Timer();
            animationTimer.Interval = 80;
            animationTimer.Tick += delegate { frame++; Invalidate(); };
            animationTimer.Start();

            hoverHideTimer = new Timer();
            hoverHideTimer.Interval = 220;
            hoverHideTimer.Tick += delegate { hoverHideTimer.Stop(); if (!Bounds.Contains(Cursor.Position) && !hover.Bounds.Contains(Cursor.Position)) hover.Hide(); };

            state.Updated += OnStateUpdated;
            MouseEnter += delegate { ShowHover(); };
            MouseLeave += delegate { hoverHideTimer.Start(); };
            MouseDown += OnPetMouseDown;
            MouseMove += OnPetMouseMove;
            MouseUp += OnPetMouseUp;

            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("打开实验面板", null, delegate { OpenDashboard(); });
            menu.Items.Add("立即刷新", null, async delegate { await state.RefreshAsync(); });
            menu.Items.Add("检查软件更新", null, async delegate { await UpdateWorkflow.CheckAndPromptAsync(this, settings.ServerUrl, false, null); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出 Epoch 精灵", null, delegate { Close(); });
            tray = new NotifyIcon();
            tray.Icon = Icon;
            tray.Text = "YOLO Epoch 精灵";
            tray.Visible = true;
            tray.ContextMenuStrip = menu;
            tray.DoubleClick += delegate { OpenDashboard(); };

            Shown += async delegate
            {
                await state.RefreshAsync();
                await UpdateWorkflow.CheckAndPromptAsync(this, settings.ServerUrl, true, null);
            };
        }

        private void EnsureVisible()
        {
            Rectangle area = Screen.FromPoint(Location).WorkingArea;
            Left = Math.Max(area.Left, Math.Min(Left, area.Right - Width));
            Top = Math.Max(area.Top, Math.Min(Top, area.Bottom - Height));
        }

        private void OnStateUpdated(object sender, EventArgs e)
        {
            if (IsDisposed) return;
            if (InvokeRequired) { BeginInvoke(new Action(delegate { OnStateUpdated(sender, e); })); return; }
            hover.UpdateRun(state.PrimaryRun(), state.Error);
            Invalidate();
        }

        internal void ShowHover()
        {
            hoverHideTimer.Stop();
            hover.UpdateRun(state.PrimaryRun(), state.Error);
            Rectangle area = Screen.FromControl(this).WorkingArea;
            int x = Left - hover.Width - 10;
            if (x < area.Left) x = Right + 10;
            int y = Math.Max(area.Top, Math.Min(Top - 6, area.Bottom - hover.Height));
            hover.Location = new Point(x, y);
            if (!hover.Visible)
                hover.Show(this);
            else
                hover.Invalidate();
        }

        private void OnPetMouseDown(object sender, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left) return;
            dragging = true;
            moved = false;
            dragMouse = Cursor.Position;
            dragForm = Location;
        }

        private void OnPetMouseMove(object sender, MouseEventArgs e)
        {
            if (!dragging) return;
            Point now = Cursor.Position;
            int dx = now.X - dragMouse.X;
            int dy = now.Y - dragMouse.Y;
            if (Math.Abs(dx) + Math.Abs(dy) > 4) moved = true;
            Location = new Point(dragForm.X + dx, dragForm.Y + dy);
            hover.Hide();
        }

        private void OnPetMouseUp(object sender, MouseEventArgs e)
        {
            if (!dragging || e.Button != MouseButtons.Left) return;
            dragging = false;
            EnsureVisible();
            settings.Left = Left;
            settings.Top = Top;
            settings.HasPosition = true;
            settings.Save();
            if (!moved) OpenDashboard();
        }

        private void OpenDashboard()
        {
            hover.Hide();
            if (dashboard == null || dashboard.IsDisposed)
            {
                dashboard = new DashboardForm(state, settings);
                dashboard.FormClosed += delegate { dashboard = null; };
            }
            dashboard.Show();
            dashboard.WindowState = FormWindowState.Normal;
            dashboard.Activate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Dictionary<string, object> run = state.PrimaryRun();
            string status = state.Error != null ? "failed" : (run == null ? "idle" : Json.Text(run, "status"));
            Color accent = Theme.StatusColor(status);
            int bob = status == "running" ? (int)(Math.Sin(frame / 3.0) * 2) : 0;
            Rectangle body = new Rectangle(28, 19 + bob, 102, 102);
            using (SolidBrush shadow = new SolidBrush(Color.FromArgb(72, 0, 0, 0))) e.Graphics.FillEllipse(shadow, 31, 112, 96, 17);
            using (Pen ear = new Pen(Color.FromArgb(48, 65, 101), 7))
            {
                ear.StartCap = LineCap.Round;
                ear.EndCap = LineCap.Round;
                e.Graphics.DrawLine(ear, 51, 30 + bob, 40, 10 + bob);
                e.Graphics.DrawLine(ear, 107, 30 + bob, 118, 10 + bob);
            }
            using (LinearGradientBrush bodyBrush = new LinearGradientBrush(body, Color.FromArgb(38, 55, 92), Color.FromArgb(16, 25, 48), 90))
                e.Graphics.FillEllipse(bodyBrush, body);
            using (Pen outline = new Pen(Color.FromArgb(61, 82, 126), 2)) e.Graphics.DrawEllipse(outline, body);

            int eyeHeight = frame > 0 && (frame % 55 == 0 || frame % 55 == 1) ? 2 : 9;
            using (SolidBrush eye = new SolidBrush(Color.FromArgb(235, 248, 255)))
            {
                e.Graphics.FillEllipse(eye, 55, 58 + bob + (9 - eyeHeight) / 2, 9, eyeHeight);
                e.Graphics.FillEllipse(eye, 94, 58 + bob + (9 - eyeHeight) / 2, 9, eyeHeight);
            }
            using (Pen smile = new Pen(Color.FromArgb(180, accent), 2)) e.Graphics.DrawArc(smile, 65, 70 + bob, 28, 18, 15, 150);
            using (SolidBrush glow = new SolidBrush(Color.FromArgb(58, accent))) e.Graphics.FillEllipse(glow, 71, 91 + bob, 17, 17);
            using (SolidBrush core = new SolidBrush(accent)) e.Graphics.FillEllipse(core, 76, 96 + bob, 7, 7);

            double percent = 0;
            if (run != null && Json.Int(run, "total_epochs") > 0) percent = Math.Min(1, (double)Json.Int(run, "current_epoch") / Json.Int(run, "total_epochs"));
            using (Pen track = new Pen(Color.FromArgb(75, Theme.Muted), 5)) e.Graphics.DrawArc(track, 19, 10 + bob, 120, 120, -90, 360);
            using (Pen ring = new Pen(accent, 5))
            {
                ring.StartCap = LineCap.Round;
                ring.EndCap = LineCap.Round;
                e.Graphics.DrawArc(ring, 19, 10 + bob, 120, 120, -90, (float)(Math.Max(.02, percent) * 360));
            }

            Rectangle captionBounds = new Rectangle(14, 137, 130, 28);
            using (GraphicsPath captionPath = Theme.RoundRect(captionBounds, 14))
            using (SolidBrush captionBack = new SolidBrush(Color.FromArgb(235, 15, 23, 42)))
            using (Pen captionBorder = new Pen(Color.FromArgb(150, accent)))
            {
                e.Graphics.FillPath(captionBack, captionPath);
                e.Graphics.DrawPath(captionBorder, captionPath);
            }
            string caption = run == null ? "等待实验" : string.Format("{0}  /  {1}", Json.Int(run, "current_epoch"), Json.Int(run, "total_epochs"));
            using (Font font = Theme.Font(9.5f, FontStyle.Bold))
                TextRenderer.DrawText(e.Graphics, caption, font, captionBounds, Theme.Text, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);

            using (SolidBrush statusGlow = new SolidBrush(Color.FromArgb(55, accent))) e.Graphics.FillEllipse(statusGlow, 119, 23 + bob, 20, 20);
            using (SolidBrush statusDot = new SolidBrush(accent)) e.Graphics.FillEllipse(statusDot, 125, 29 + bob, 8, 8);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            tray.Visible = false;
            tray.Dispose();
            hover.Close();
            if (dashboard != null) dashboard.Close();
            pollTimer.Dispose();
            animationTimer.Dispose();
            hoverHideTimer.Dispose();
            state.Dispose();
            base.OnFormClosed(e);
        }
    }
}
