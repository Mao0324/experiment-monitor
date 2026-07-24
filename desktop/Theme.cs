using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal static class Theme
    {
        public static readonly Color Back = Color.FromArgb(7, 11, 20);
        public static readonly Color Panel = Color.FromArgb(15, 23, 42);
        public static readonly Color Panel2 = Color.FromArgb(10, 17, 31);
        public static readonly Color Line = Color.FromArgb(31, 45, 70);
        public static readonly Color Text = Color.FromArgb(241, 245, 252);
        public static readonly Color Muted = Color.FromArgb(139, 154, 184);
        public static readonly Color Blue = Color.FromArgb(106, 122, 255);
        public static readonly Color Cyan = Color.FromArgb(61, 217, 206);
        public static readonly Color Green = Color.FromArgb(63, 207, 142);
        public static readonly Color Amber = Color.FromArgb(245, 180, 65);
        public static readonly Color Red = Color.FromArgb(246, 93, 112);
        public static readonly Color Purple = Color.FromArgb(169, 116, 255);

        public static Font Font(float size, FontStyle style)
        {
            return new Font("Microsoft YaHei UI", size, style, GraphicsUnit.Point);
        }

        public static GraphicsPath RoundRect(Rectangle rectangle, int radius)
        {
            int diameter = radius * 2;
            GraphicsPath path = new GraphicsPath();
            path.AddArc(rectangle.Left, rectangle.Top, diameter, diameter, 180, 90);
            path.AddArc(rectangle.Right - diameter, rectangle.Top, diameter, diameter, 270, 90);
            path.AddArc(rectangle.Right - diameter, rectangle.Bottom - diameter, diameter, diameter, 0, 90);
            path.AddArc(rectangle.Left, rectangle.Bottom - diameter, diameter, diameter, 90, 90);
            path.CloseFigure();
            return path;
        }

        public static Color StatusColor(string status)
        {
            if (status == "running") return Cyan;
            if (status == "completed") return Green;
            if (status == "stalled") return Amber;
            if (status == "failed") return Red;
            return Muted;
        }

        public static string StatusText(string status)
        {
            if (status == "running") return "运行中";
            if (status == "completed") return "已完成";
            if (status == "stalled") return "疑似卡住";
            if (status == "failed") return "失败";
            return string.IsNullOrEmpty(status) ? "等待实验" : status;
        }

        public static Button Button(string text, bool primary)
        {
            RoundButton button = new RoundButton();
            button.Text = text;
            button.AutoSize = true;
            button.Height = 34;
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderSize = 0;
            button.BorderColor = primary ? Blue : Line;
            button.BackColor = primary ? Blue : Color.FromArgb(23, 34, 56);
            button.ForeColor = Color.White;
            button.Font = Font(9, FontStyle.Bold);
            button.Cursor = Cursors.Hand;
            button.Padding = new Padding(12, 0, 12, 0);
            return button;
        }

        public static TextBox TextBox(string placeholder)
        {
            TextBox box = new TextBox();
            box.BackColor = Color.FromArgb(11, 16, 32);
            box.ForeColor = Text;
            box.BorderStyle = BorderStyle.FixedSingle;
            box.Font = Font(9, FontStyle.Regular);
            box.Tag = placeholder;
            return box;
        }

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr SendMessage(IntPtr handle, int message, IntPtr wParam, string lParam);

        public static void SetCueBanner(TextBox box, string text)
        {
            box.HandleCreated += delegate { SendMessage(box.Handle, 0x1501, (IntPtr)1, text); };
        }

        public static void StyleComboBox(ComboBox combo)
        {
            combo.DrawMode = DrawMode.OwnerDrawFixed;
            combo.ItemHeight = 24;
            combo.DrawItem += delegate(object sender, DrawItemEventArgs e)
            {
                e.DrawBackground();
                if (e.Index < 0) return;
                ComboBox control = (ComboBox)sender;
                bool selected = (e.State & DrawItemState.Selected) == DrawItemState.Selected;
                using (SolidBrush background = new SolidBrush(selected ? Color.FromArgb(40, 73, 116) : Panel2)) e.Graphics.FillRectangle(background, e.Bounds);
                TextRenderer.DrawText(e.Graphics, Convert.ToString(control.Items[e.Index]), Font(8.5f, FontStyle.Regular), e.Bounds, Text, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            };
        }

        public static Label Label(string text, float size, FontStyle style, Color color)
        {
            Label label = new Label();
            label.Text = text;
            label.ForeColor = color;
            label.Font = Font(size, style);
            label.AutoSize = true;
            return label;
        }

        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern bool DestroyIcon(IntPtr handle);

        public static Icon CreateAppIcon()
        {
            Bitmap bitmap = new Bitmap(32, 32);
            using (Graphics graphics = Graphics.FromImage(bitmap))
            {
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.Clear(Color.Transparent);
                using (SolidBrush body = new SolidBrush(Color.FromArgb(25, 38, 68))) graphics.FillEllipse(body, 3, 5, 26, 24);
                using (Pen ring = new Pen(Cyan, 3)) graphics.DrawArc(ring, 4, 5, 24, 24, -80, 300);
                using (SolidBrush eye = new SolidBrush(Color.White))
                {
                    graphics.FillEllipse(eye, 10, 13, 4, 5);
                    graphics.FillEllipse(eye, 19, 13, 4, 5);
                }
            }
            IntPtr handle = bitmap.GetHicon();
            Icon icon = (Icon)Icon.FromHandle(handle).Clone();
            DestroyIcon(handle);
            bitmap.Dispose();
            return icon;
        }
    }

    internal sealed class DarkPanel : Panel
    {
        public int Radius { get; set; }
        public Color AccentColor { get; set; }
        public bool ShowAccent { get; set; }
        public DarkPanel()
        {
            Radius = 14;
            AccentColor = Theme.Blue;
            BackColor = Theme.Panel;
            Padding = new Padding(14);
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (GraphicsPath path = Theme.RoundRect(new Rectangle(0, 0, Width - 1, Height - 1), Radius))
            using (SolidBrush brush = new SolidBrush(BackColor))
            using (Pen pen = new Pen(Theme.Line))
            {
                e.Graphics.FillPath(brush, path);
                e.Graphics.DrawPath(pen, path);
                if (ShowAccent)
                {
                    using (SolidBrush accent = new SolidBrush(AccentColor))
                    using (GraphicsPath accentPath = Theme.RoundRect(new Rectangle(1, 1, 5, Height - 3), 2))
                        e.Graphics.FillPath(accent, accentPath);
                }
            }
        }
    }

    internal sealed class RoundButton : Button
    {
        private bool hover;
        private bool pressed;
        public Color BorderColor { get; set; }
        public int Radius { get; set; }

        public RoundButton()
        {
            Radius = 9;
            BorderColor = Theme.Line;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
            MouseEnter += delegate { hover = true; Invalidate(); };
            MouseLeave += delegate { hover = false; pressed = false; Invalidate(); };
            MouseDown += delegate { pressed = true; Invalidate(); };
            MouseUp += delegate { pressed = false; Invalidate(); };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color fill = BackColor;
            if (hover) fill = ControlPaint.Light(fill, .08f);
            if (pressed) fill = ControlPaint.Dark(fill, .08f);
            Rectangle bounds = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath path = Theme.RoundRect(bounds, Radius))
            using (SolidBrush brush = new SolidBrush(fill))
            using (Pen pen = new Pen(BorderColor))
            {
                e.Graphics.FillPath(brush, path);
                e.Graphics.DrawPath(pen, path);
            }
            TextRenderer.DrawText(e.Graphics, Text, Font, bounds, Enabled ? ForeColor : Theme.Muted, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
        }
    }

    internal sealed class ModernProgress : Control
    {
        private int value;
        public int Maximum { get; set; }
        public Color AccentColor { get; set; }
        public int Value
        {
            get { return value; }
            set { this.value = Math.Max(0, Math.Min(Maximum, value)); Invalidate(); }
        }

        public ModernProgress()
        {
            Maximum = 1000;
            AccentColor = Theme.Cyan;
            Height = 10;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle track = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath path = Theme.RoundRect(track, Math.Max(2, Height / 2)))
            using (SolidBrush background = new SolidBrush(Color.FromArgb(25, 38, 60)))
                e.Graphics.FillPath(background, path);
            int fillWidth = Maximum <= 0 ? 0 : (int)Math.Round((Width - 1) * value / (double)Maximum);
            if (fillWidth > 2)
            {
                Rectangle fill = new Rectangle(0, 0, fillWidth, Height - 1);
                using (GraphicsPath path = Theme.RoundRect(fill, Math.Max(2, Height / 2)))
                using (LinearGradientBrush brush = new LinearGradientBrush(fill, Theme.Blue, AccentColor, 0f))
                    e.Graphics.FillPath(brush, path);
            }
        }
    }
}
