using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal enum VisualStyleMode
    {
        Material,
        Clay,
        Elegant
    }

    internal sealed class ThemePalette
    {
        public Color Back;
        public Color Panel;
        public Color Panel2;
        public Color Raised;
        public Color Input;
        public Color Line;
        public Color Text;
        public Color Muted;
        public Color Primary;
        public Color Cyan;
        public Color Green;
        public Color Amber;
        public Color Red;
        public Color Purple;
        public Color Selection;
        public Color Shadow;
        public Color Highlight;
        public int CardRadius;
        public int ButtonRadius;
    }

    internal static class Theme
    {
        private static VisualStyleMode mode = VisualStyleMode.Material;
        private static ThemePalette palette = MaterialPalette();

        public static VisualStyleMode Mode { get { return mode; } }
        public static Color Back { get { return palette.Back; } }
        public static Color Panel { get { return palette.Panel; } }
        public static Color Panel2 { get { return palette.Panel2; } }
        public static Color Raised { get { return palette.Raised; } }
        public static Color Input { get { return palette.Input; } }
        public static Color Line { get { return palette.Line; } }
        public static Color Text { get { return palette.Text; } }
        public static Color Muted { get { return palette.Muted; } }
        public static Color Blue { get { return palette.Primary; } }
        public static Color Cyan { get { return palette.Cyan; } }
        public static Color Green { get { return palette.Green; } }
        public static Color Amber { get { return palette.Amber; } }
        public static Color Red { get { return palette.Red; } }
        public static Color Purple { get { return palette.Purple; } }
        public static Color Selection { get { return palette.Selection; } }
        public static Color Shadow { get { return palette.Shadow; } }
        public static Color Highlight { get { return palette.Highlight; } }
        public static int CardRadius { get { return palette.CardRadius; } }
        public static int ButtonRadius { get { return palette.ButtonRadius; } }

        public static string ModeKey
        {
            get
            {
                if (mode == VisualStyleMode.Clay) return "clay";
                if (mode == VisualStyleMode.Elegant) return "elegant";
                return "material";
            }
        }

        public static string ModeDisplayName
        {
            get
            {
                if (mode == VisualStyleMode.Clay) return "Clay";
                if (mode == VisualStyleMode.Elegant) return "Elegant";
                return "Material";
            }
        }

        public static void Use(string value)
        {
            string key = (value ?? "").Trim().ToLowerInvariant();
            if (key == "clay" || key == "claymorphism")
            {
                mode = VisualStyleMode.Clay;
                palette = ClayPalette();
            }
            else if (key == "elegant")
            {
                mode = VisualStyleMode.Elegant;
                palette = ElegantPalette();
            }
            else
            {
                mode = VisualStyleMode.Material;
                palette = MaterialPalette();
            }
        }

        private static ThemePalette MaterialPalette()
        {
            return new ThemePalette
            {
                Back = Color.FromArgb(8, 13, 24),
                Panel = Color.FromArgb(17, 25, 42),
                Panel2 = Color.FromArgb(11, 18, 32),
                Raised = Color.FromArgb(24, 35, 57),
                Input = Color.FromArgb(12, 19, 34),
                Line = Color.FromArgb(37, 51, 77),
                Text = Color.FromArgb(244, 247, 252),
                Muted = Color.FromArgb(145, 159, 187),
                Primary = Color.FromArgb(105, 123, 255),
                Cyan = Color.FromArgb(54, 214, 198),
                Green = Color.FromArgb(67, 205, 143),
                Amber = Color.FromArgb(246, 184, 72),
                Red = Color.FromArgb(243, 92, 114),
                Purple = Color.FromArgb(171, 124, 255),
                Selection = Color.FromArgb(39, 55, 88),
                Shadow = Color.FromArgb(105, 0, 0, 0),
                Highlight = Color.FromArgb(30, 255, 255, 255),
                CardRadius = 14,
                ButtonRadius = 9
            };
        }

        private static ThemePalette ClayPalette()
        {
            return new ThemePalette
            {
                Back = Color.FromArgb(226, 232, 245),
                Panel = Color.FromArgb(232, 237, 248),
                Panel2 = Color.FromArgb(218, 226, 242),
                Raised = Color.FromArgb(239, 243, 251),
                Input = Color.FromArgb(241, 244, 251),
                Line = Color.FromArgb(197, 207, 226),
                Text = Color.FromArgb(39, 48, 71),
                Muted = Color.FromArgb(101, 113, 140),
                Primary = Color.FromArgb(101, 111, 198),
                Cyan = Color.FromArgb(55, 165, 160),
                Green = Color.FromArgb(70, 162, 113),
                Amber = Color.FromArgb(204, 143, 48),
                Red = Color.FromArgb(205, 79, 102),
                Purple = Color.FromArgb(143, 105, 192),
                Selection = Color.FromArgb(204, 214, 236),
                Shadow = Color.FromArgb(72, 145, 157, 184),
                Highlight = Color.FromArgb(220, 255, 255, 255),
                CardRadius = 22,
                ButtonRadius = 14
            };
        }

        private static ThemePalette ElegantPalette()
        {
            return new ThemePalette
            {
                Back = Color.FromArgb(14, 14, 16),
                Panel = Color.FromArgb(23, 23, 25),
                Panel2 = Color.FromArgb(18, 18, 20),
                Raised = Color.FromArgb(29, 28, 29),
                Input = Color.FromArgb(17, 17, 19),
                Line = Color.FromArgb(58, 54, 53),
                Text = Color.FromArgb(244, 239, 231),
                Muted = Color.FromArgb(158, 149, 139),
                Primary = Color.FromArgb(202, 168, 105),
                Cyan = Color.FromArgb(124, 168, 158),
                Green = Color.FromArgb(112, 174, 135),
                Amber = Color.FromArgb(210, 168, 89),
                Red = Color.FromArgb(205, 101, 105),
                Purple = Color.FromArgb(154, 124, 158),
                Selection = Color.FromArgb(49, 45, 43),
                Shadow = Color.FromArgb(150, 0, 0, 0),
                Highlight = Color.FromArgb(24, 244, 239, 231),
                CardRadius = 6,
                ButtonRadius = 4
            };
        }

        public static Font Font(float size, FontStyle style)
        {
            return new Font("Microsoft YaHei UI", size, style, GraphicsUnit.Point);
        }

        public static GraphicsPath RoundRect(Rectangle rectangle, int radius)
        {
            radius = Math.Max(1, Math.Min(radius, Math.Min(rectangle.Width, rectangle.Height) / 2));
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
            button.Primary = primary;
            button.AutoSize = true;
            button.Height = 38;
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderSize = 0;
            button.BorderColor = primary ? Blue : Line;
            button.BackColor = primary ? Blue : Raised;
            button.ForeColor = primary || mode != VisualStyleMode.Clay ? Color.White : Text;
            button.Font = Font(9, FontStyle.Bold);
            button.Cursor = Cursors.Hand;
            button.Padding = new Padding(14, 0, 14, 0);
            return button;
        }

        public static TextBox TextBox(string placeholder)
        {
            TextBox box = new TextBox();
            box.BackColor = Input;
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
            combo.BackColor = Input;
            combo.ForeColor = Text;
            combo.FlatStyle = FlatStyle.Flat;
            combo.DrawMode = DrawMode.OwnerDrawFixed;
            combo.ItemHeight = 26;
            combo.DrawItem += delegate(object sender, DrawItemEventArgs e)
            {
                if (e.Index < 0) return;
                ComboBox control = (ComboBox)sender;
                bool selected = (e.State & DrawItemState.Selected) == DrawItemState.Selected;
                using (SolidBrush background = new SolidBrush(selected ? Selection : Panel))
                    e.Graphics.FillRectangle(background, e.Bounds);
                TextRenderer.DrawText(e.Graphics, Convert.ToString(control.Items[e.Index]), Font(8.7f, FontStyle.Regular), e.Bounds, Text, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
            };
        }

        public static Label Label(string text, float size, FontStyle style, Color color)
        {
            Label label = new Label();
            label.Text = text;
            label.ForeColor = color;
            label.Font = Font(size, style);
            label.AutoSize = true;
            label.BackColor = Color.Transparent;
            return label;
        }

        public static ToolStripMenuItem CreateStyleMenu(Action<string> select)
        {
            ToolStripMenuItem root = new ToolStripMenuItem("界面风格");
            AddStyleItem(root, "Material Design", "material", select);
            AddStyleItem(root, "Claymorphism", "clay", select);
            AddStyleItem(root, "Elegant", "elegant", select);
            root.DropDownOpening += delegate
            {
                foreach (ToolStripItem raw in root.DropDownItems)
                {
                    ToolStripMenuItem item = raw as ToolStripMenuItem;
                    if (item != null) item.Checked = Convert.ToString(item.Tag) == ModeKey;
                }
            };
            return root;
        }

        private static void AddStyleItem(ToolStripMenuItem root, string text, string key, Action<string> select)
        {
            ToolStripMenuItem item = new ToolStripMenuItem(text);
            item.Tag = key;
            item.Checked = ModeKey == key;
            item.Click += delegate { select(key); };
            root.DropDownItems.Add(item);
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
                using (SolidBrush body = new SolidBrush(Panel)) graphics.FillEllipse(body, 3, 5, 26, 24);
                using (Pen ring = new Pen(Cyan, 3)) graphics.DrawArc(ring, 4, 5, 24, 24, -80, 300);
                using (SolidBrush eye = new SolidBrush(Text))
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
            Radius = Theme.CardRadius;
            AccentColor = Theme.Blue;
            BackColor = Theme.Panel;
            Padding = new Padding(16);
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            e.Graphics.Clear(Parent == null ? Theme.Back : Parent.BackColor);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            int inset = Theme.Mode == VisualStyleMode.Clay ? 5 : 1;
            Rectangle card = new Rectangle(inset, inset, Math.Max(2, Width - inset * 2 - 1), Math.Max(2, Height - inset * 2 - 1));

            if (Theme.Mode == VisualStyleMode.Clay)
            {
                Rectangle shadowBounds = card;
                shadowBounds.Offset(3, 4);
                using (GraphicsPath shadowPath = Theme.RoundRect(shadowBounds, Radius))
                using (SolidBrush shadow = new SolidBrush(Theme.Shadow))
                    e.Graphics.FillPath(shadow, shadowPath);
            }

            using (GraphicsPath path = Theme.RoundRect(card, Radius))
            using (SolidBrush brush = new SolidBrush(BackColor))
            using (Pen pen = new Pen(Theme.Line))
            {
                e.Graphics.FillPath(brush, path);
                e.Graphics.DrawPath(pen, path);
            }

            if (Theme.Mode == VisualStyleMode.Clay)
            {
                Rectangle highlightBounds = new Rectangle(card.Left + 2, card.Top + 2, card.Width - 5, card.Height - 5);
                using (GraphicsPath highlightPath = Theme.RoundRect(highlightBounds, Math.Max(2, Radius - 2)))
                using (Pen highlight = new Pen(Theme.Highlight, 1.4f))
                    e.Graphics.DrawPath(highlight, highlightPath);
            }
            else if (Theme.Mode == VisualStyleMode.Elegant)
            {
                using (Pen topLine = new Pen(Color.FromArgb(90, AccentColor), 1))
                    e.Graphics.DrawLine(topLine, card.Left + Radius, card.Top + 2, card.Right - Radius, card.Top + 2);
            }

            if (ShowAccent)
            {
                Rectangle accentBounds = new Rectangle(card.Left + 15, card.Top + 12, Theme.Mode == VisualStyleMode.Elegant ? 42 : 30, 3);
                using (GraphicsPath accentPath = Theme.RoundRect(accentBounds, 2))
                using (SolidBrush accent = new SolidBrush(AccentColor))
                    e.Graphics.FillPath(accent, accentPath);
            }
        }
    }

    internal sealed class RoundButton : Button
    {
        private bool hover;
        private bool pressed;
        public bool Primary { get; set; }
        public Color BorderColor { get; set; }
        public int Radius { get; set; }

        public RoundButton()
        {
            Radius = Theme.ButtonRadius;
            BorderColor = Theme.Line;
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            MouseEnter += delegate { hover = true; Invalidate(); };
            MouseLeave += delegate { hover = false; pressed = false; Invalidate(); };
            MouseDown += delegate { pressed = true; Invalidate(); };
            MouseUp += delegate { pressed = false; Invalidate(); };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            int inset = Theme.Mode == VisualStyleMode.Clay ? 4 : 1;
            Rectangle bounds = new Rectangle(inset, inset, Math.Max(2, Width - inset * 2 - 1), Math.Max(2, Height - inset * 2 - 1));
            Color fill = BackColor;
            if (hover) fill = Theme.Mode == VisualStyleMode.Clay ? ControlPaint.Light(fill, .04f) : ControlPaint.Light(fill, .10f);
            if (pressed) { fill = ControlPaint.Dark(fill, .08f); bounds.Offset(0, 1); }

            if (Theme.Mode == VisualStyleMode.Clay && !pressed)
            {
                Rectangle shadowBounds = bounds;
                shadowBounds.Offset(2, 3);
                using (GraphicsPath shadowPath = Theme.RoundRect(shadowBounds, Radius))
                using (SolidBrush shadow = new SolidBrush(Color.FromArgb(55, Theme.Shadow)))
                    e.Graphics.FillPath(shadow, shadowPath);
            }

            using (GraphicsPath path = Theme.RoundRect(bounds, Radius))
            using (SolidBrush brush = new SolidBrush(fill))
            using (Pen pen = new Pen(BorderColor))
            {
                e.Graphics.FillPath(brush, path);
                e.Graphics.DrawPath(pen, path);
            }

            if (Theme.Mode == VisualStyleMode.Clay && !Primary)
            {
                Rectangle shineBounds = new Rectangle(bounds.Left + 2, bounds.Top + 2, bounds.Width - 5, bounds.Height - 5);
                using (GraphicsPath shinePath = Theme.RoundRect(shineBounds, Math.Max(2, Radius - 2)))
                using (Pen shine = new Pen(Theme.Highlight))
                    e.Graphics.DrawPath(shine, shinePath);
            }

            TextRenderer.DrawText(e.Graphics, Text, Font, bounds, Enabled ? ForeColor : Theme.Muted, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
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
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Rectangle track = new Rectangle(0, 1, Width - 1, Height - 3);
            using (GraphicsPath path = Theme.RoundRect(track, Math.Max(2, track.Height / 2)))
            using (SolidBrush background = new SolidBrush(Theme.Line))
                e.Graphics.FillPath(background, path);
            int fillWidth = Maximum <= 0 ? 0 : (int)Math.Round((Width - 1) * value / (double)Maximum);
            if (fillWidth > 2)
            {
                Rectangle fill = new Rectangle(0, 1, fillWidth, Height - 3);
                using (GraphicsPath path = Theme.RoundRect(fill, Math.Max(2, fill.Height / 2)))
                using (LinearGradientBrush brush = new LinearGradientBrush(fill, Theme.Mode == VisualStyleMode.Elegant ? Theme.Blue : AccentColor, Theme.Mode == VisualStyleMode.Material ? Theme.Blue : AccentColor, 0f))
                    e.Graphics.FillPath(brush, path);
                if (Theme.Mode == VisualStyleMode.Clay)
                    using (SolidBrush dot = new SolidBrush(Theme.Highlight)) e.Graphics.FillEllipse(dot, Math.Max(0, fill.Right - 7), 2, 5, 5);
            }
        }
    }

    internal sealed class StatusPill : Control
    {
        private string status = "";
        public string Status
        {
            get { return status; }
            set { status = value ?? ""; Invalidate(); }
        }

        public StatusPill()
        {
            Height = 26;
            Width = 92;
            Font = Theme.Font(8.3f, FontStyle.Bold);
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color color = Theme.StatusColor(status);
            Rectangle bounds = new Rectangle(0, 0, Width - 1, Height - 1);
            using (GraphicsPath path = Theme.RoundRect(bounds, Height / 2))
            using (SolidBrush background = new SolidBrush(Color.FromArgb(28, color)))
            using (Pen border = new Pen(Color.FromArgb(120, color)))
            {
                e.Graphics.FillPath(background, path);
                e.Graphics.DrawPath(border, path);
            }
            using (SolidBrush dot = new SolidBrush(color)) e.Graphics.FillEllipse(dot, 10, Height / 2 - 3, 6, 6);
            TextRenderer.DrawText(e.Graphics, Theme.StatusText(status), Font, new Rectangle(21, 0, Width - 25, Height), color, TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
        }
    }
}
