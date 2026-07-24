using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class ChartPoint
    {
        public int X;
        public double Y;
        public ChartPoint(int x, double y) { X = x; Y = y; }
    }

    internal sealed class ChartSeries
    {
        public string Name;
        public Color Color;
        public List<ChartPoint> Points;
    }

    internal sealed class TrendChart : Control
    {
        private List<ChartSeries> series = new List<ChartSeries>();
        private Point mouse = new Point(-1000, -1000);
        private readonly ToolTip tooltip = new ToolTip();

        public TrendChart()
        {
            DoubleBuffered = true;
            BackColor = Theme.Panel2;
            ForeColor = Theme.Text;
            SetStyle(ControlStyles.ResizeRedraw, true);
            MouseMove += OnMouseMoveChart;
            MouseLeave += delegate { mouse = new Point(-1000, -1000); tooltip.Hide(this); Invalidate(); };
        }

        public void SetSeries(List<ChartSeries> value)
        {
            series = value ?? new List<ChartSeries>();
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(BackColor);
            Rectangle plot = new Rectangle(70, 28, Math.Max(10, Width - 98), Math.Max(10, Height - 88));
            List<ChartPoint> all = series.Where(delegate(ChartSeries s) { return s.Points != null; }).SelectMany(delegate(ChartSeries s) { return s.Points; }).ToList();
            if (all.Count == 0)
            {
                using (Font font = Theme.Font(10, FontStyle.Regular))
                using (SolidBrush brush = new SolidBrush(Theme.Muted))
                {
                    string text = "当前指标暂无 Epoch 数据";
                    SizeF size = g.MeasureString(text, font);
                    g.DrawString(text, font, brush, (Width - size.Width) / 2, (Height - size.Height) / 2);
                }
                return;
            }
            int minX = all.Min(delegate(ChartPoint p) { return p.X; });
            int maxX = all.Max(delegate(ChartPoint p) { return p.X; });
            double minY = all.Min(delegate(ChartPoint p) { return p.Y; });
            double maxY = all.Max(delegate(ChartPoint p) { return p.Y; });
            if (maxX == minX) maxX++;
            if (Math.Abs(maxY - minY) < 1e-12) { double pad = Math.Abs(maxY) < 1 ? .05 : Math.Abs(maxY) * .05; minY -= pad; maxY += pad; }
            else { double pad = (maxY - minY) * .08; minY -= pad; maxY += pad; }

            using (Font axisFont = Theme.Font(7.5f, FontStyle.Regular))
            using (SolidBrush muted = new SolidBrush(Theme.Muted))
            using (Pen gridPen = new Pen(Color.FromArgb(35, Theme.Muted)))
            using (Pen axisPen = new Pen(Theme.Line))
            {
                for (int i = 0; i <= 5; i++)
                {
                    float y = plot.Top + plot.Height * i / 5f;
                    double value = maxY - (maxY - minY) * i / 5.0;
                    g.DrawLine(gridPen, plot.Left, y, plot.Right, y);
                    string label = value.ToString("0.####", CultureInfo.InvariantCulture);
                    SizeF size = g.MeasureString(label, axisFont);
                    g.DrawString(label, axisFont, muted, plot.Left - size.Width - 8, y - size.Height / 2);
                }
                for (int i = 0; i <= 5; i++)
                {
                    float x = plot.Left + plot.Width * i / 5f;
                    int epoch = (int)Math.Round(minX + (maxX - minX) * i / 5.0);
                    g.DrawLine(gridPen, x, plot.Top, x, plot.Bottom);
                    string label = epoch.ToString(CultureInfo.InvariantCulture);
                    SizeF size = g.MeasureString(label, axisFont);
                    g.DrawString(label, axisFont, muted, x - size.Width / 2, plot.Bottom + 8);
                }
                g.DrawRectangle(axisPen, plot);
                g.DrawString("Epoch", axisFont, muted, plot.Left + plot.Width / 2 - 18, plot.Bottom + 30);
            }

            foreach (ChartSeries item in series)
            {
                if (item.Points == null || item.Points.Count == 0) continue;
                PointF[] points = item.Points.Select(delegate(ChartPoint point)
                {
                    return new PointF(plot.Left + (float)(point.X - minX) / (maxX - minX) * plot.Width, plot.Bottom - (float)((point.Y - minY) / (maxY - minY)) * plot.Height);
                }).ToArray();
                using (Pen pen = new Pen(item.Color, 2.2f))
                {
                    pen.LineJoin = LineJoin.Round;
                    if (points.Length > 1) g.DrawLines(pen, points);
                }
                using (SolidBrush pointBrush = new SolidBrush(item.Color))
                    foreach (PointF point in points) g.FillEllipse(pointBrush, point.X - 2.5f, point.Y - 2.5f, 5, 5);
            }

            int legendX = plot.Left;
            using (Font legendFont = Theme.Font(7.5f, FontStyle.Regular))
            {
                foreach (ChartSeries item in series)
                {
                    using (SolidBrush dot = new SolidBrush(item.Color)) g.FillEllipse(dot, legendX, 9, 8, 8);
                    using (SolidBrush text = new SolidBrush(Theme.Muted)) g.DrawString(Shorten(item.Name, 34), legendFont, text, legendX + 12, 5);
                    legendX += Math.Min(280, 24 + (int)g.MeasureString(Shorten(item.Name, 34), legendFont).Width);
                }
            }

            if (plot.Contains(mouse))
            {
                using (Pen cross = new Pen(Color.FromArgb(110, Theme.Blue))) g.DrawLine(cross, mouse.X, plot.Top, mouse.X, plot.Bottom);
            }
        }

        private void OnMouseMoveChart(object sender, MouseEventArgs e)
        {
            mouse = e.Location;
            List<ChartPoint> all = series.Where(delegate(ChartSeries s) { return s.Points != null; }).SelectMany(delegate(ChartSeries s) { return s.Points; }).ToList();
            if (all.Count == 0) return;
            Rectangle plot = new Rectangle(70, 28, Math.Max(10, Width - 98), Math.Max(10, Height - 88));
            if (!plot.Contains(e.Location)) { tooltip.Hide(this); Invalidate(); return; }
            int minX = all.Min(delegate(ChartPoint p) { return p.X; }), maxX = all.Max(delegate(ChartPoint p) { return p.X; });
            int epoch = minX == maxX ? minX : (int)Math.Round(minX + (maxX - minX) * (e.X - plot.Left) / (double)plot.Width);
            List<string> lines = new List<string> { "Epoch " + epoch };
            foreach (ChartSeries item in series)
            {
                ChartPoint point = item.Points == null ? null : item.Points.OrderBy(delegate(ChartPoint p) { return Math.Abs(p.X - epoch); }).FirstOrDefault();
                if (point != null) lines.Add(Shorten(item.Name, 28) + "  " + point.Y.ToString("0.######", CultureInfo.InvariantCulture));
            }
            tooltip.Show(string.Join(Environment.NewLine, lines.ToArray()), this, e.X + 12, e.Y + 12, 800);
            Invalidate();
        }

        private static string Shorten(string value, int length) { value = value ?? "实验"; return value.Length <= length ? value : value.Substring(0, length - 1) + "…"; }
    }

    internal sealed class ComparisonForm : Form
    {
        private readonly MonitorApi api;
        private readonly List<string> ids;
        private readonly ComboBox metric;
        private readonly TrendChart chart;
        private readonly Label status;
        private readonly List<Dictionary<string, object>> details = new List<Dictionary<string, object>>();
        private static Color[] SeriesColors
        {
            get { return new Color[] { Theme.Cyan, Theme.Amber, Theme.Green, Theme.Red, Theme.Purple }; }
        }

        public ComparisonForm(MonitorApi api, List<string> ids)
        {
            this.api = api; this.ids = ids;
            Text = "多实验指标对比"; Icon = Theme.CreateAppIcon(); StartPosition = FormStartPosition.CenterParent; ClientSize = new Size(1060, 650); MinimumSize = new Size(820, 520); BackColor = Theme.Back; ForeColor = Theme.Text;
            Panel header = new Panel { Dock = DockStyle.Top, Height = 65, BackColor = Theme.Panel, Padding = new Padding(18) };
            header.Controls.Add(Theme.Label("统一纵轴对比", 13, FontStyle.Bold, Theme.Text));
            metric = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Theme.Panel2, ForeColor = Theme.Text, FlatStyle = FlatStyle.Flat, Location = new Point(230, 18), Width = 350 };
            Theme.StyleComboBox(metric);
            metric.SelectedIndexChanged += delegate { Render(); };
            status = Theme.Label("正在读取实验指标…", 8.5f, FontStyle.Regular, Theme.Muted); status.Location = new Point(600, 21);
            header.Controls.Add(metric); header.Controls.Add(status);
            chart = new TrendChart { Dock = DockStyle.Fill, BackColor = Theme.Panel2, Padding = new Padding(10) };
            Controls.Add(chart); Controls.Add(header);
        }

        public async Task LoadDataAsync()
        {
            try
            {
                details.Clear();
                foreach (string id in ids) details.Add(await api.GetRunAsync(id));
                List<string> keys = new List<string>();
                foreach (Dictionary<string, object> detail in details)
                    foreach (object item in Json.Children(detail, "events"))
                        foreach (KeyValuePair<string, object> pair in Json.Child(Json.Dict(item), "metrics"))
                            if (!keys.Contains(pair.Key) && IsNumber(pair.Value)) keys.Add(pair.Key);
                metric.Items.Clear(); foreach (string key in keys) metric.Items.Add(key);
                if (metric.Items.Count > 0) metric.SelectedIndex = Preferred(keys); else status.Text = "所选实验没有可对比的 Epoch 指标";
            }
            catch (Exception ex) { status.Text = "读取失败：" + ex.Message; status.ForeColor = Theme.Red; }
        }

        private void Render()
        {
            if (metric.SelectedItem == null) return;
            string key = metric.SelectedItem.ToString();
            List<ChartSeries> output = new List<ChartSeries>();
            for (int i = 0; i < details.Count; i++)
            {
                Dictionary<string, object> detail = details[i];
                Color[] colors = SeriesColors;
                ChartSeries item = new ChartSeries { Name = Json.Text(Json.Child(detail, "run"), "name", ids[i]), Color = colors[i % colors.Length], Points = new List<ChartPoint>() };
                foreach (object raw in Json.Children(detail, "events"))
                {
                    Dictionary<string, object> evt = Json.Dict(raw), values = Json.Child(evt, "metrics"); object value;
                    if (values.TryGetValue(key, out value) && IsNumber(value)) item.Points.Add(new ChartPoint(Json.Int(evt, "epoch"), Convert.ToDouble(value, CultureInfo.InvariantCulture)));
                }
                output.Add(item);
            }
            chart.SetSeries(output);
            status.Text = string.Format("{0} 条实验 · {1}", details.Count, key); status.ForeColor = Theme.Green;
        }

        private static int Preferred(List<string> keys)
        {
            string[] order = { "map50-95", "map50", "precision", "recall", "loss" };
            foreach (string token in order) for (int i = 0; i < keys.Count; i++) if (keys[i].ToLowerInvariant().Replace("_", "-").Contains(token)) return i;
            return 0;
        }
        private static bool IsNumber(object value) { return value is byte || value is short || value is int || value is long || value is float || value is double || value is decimal; }
    }
}
