using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class ExperimentNotesDialog : Form
    {
        private readonly TextBox hypothesis = MultiLine();
        private readonly TextBox changes = MultiLine();
        private readonly TextBox results = MultiLine();
        private readonly TextBox conclusion = MultiLine();
        private readonly TextBox nextStep = MultiLine();
        private readonly TextBox baseline = Theme.TextBox("");

        public ExperimentNotesDialog(Dictionary<string, object> run, string configDiff)
        {
            Text = "实验笔记与结论"; Icon = Theme.CreateAppIcon(); StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(860, 790); MinimumSize = new Size(740, 650); BackColor = Theme.Back; ForeColor = Theme.Text; Font = Theme.Font(9, FontStyle.Regular);
            Panel header = new Panel { Dock = DockStyle.Top, Height = 82, Padding = new Padding(24, 14, 24, 8), BackColor = Theme.Panel2 };
            Label eyebrow = Theme.Label("EXPERIMENT JOURNAL", 7.2f, FontStyle.Bold, Theme.Blue); eyebrow.Location = new Point(24, 14);
            Label title = Theme.Label("实验笔记与结论", 14, FontStyle.Bold, Theme.Text); title.Location = new Point(23, 35);
            Label subtitle = Theme.Label("记录假设、改动、结果和下一步", 8.3f, FontStyle.Regular, Theme.Muted); subtitle.Location = new Point(190, 43);
            header.Controls.Add(eyebrow); header.Controls.Add(title); header.Controls.Add(subtitle);
            TableLayoutPanel layout = new TableLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, Padding = new Padding(28, 22, 28, 22), ColumnCount = 2, RowCount = 7, BackColor = Theme.Panel };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120)); layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            Add(layout, 0, "实验假设", hypothesis, 100); Add(layout, 1, "修改内容", changes, 100); Add(layout, 2, "结果记录", results, 100);
            Add(layout, 3, "结论", conclusion, 100); Add(layout, 4, "下一步", nextStep, 100); Add(layout, 5, "基线 Run ID", baseline, 48);
            RichTextBox diff = new RichTextBox { ReadOnly = true, BorderStyle = BorderStyle.FixedSingle, BackColor = Theme.Input, ForeColor = Theme.Mode == VisualStyleMode.Clay ? Theme.Text : Color.FromArgb(195, 215, 238), Font = new Font("Consolas", 9), Text = string.IsNullOrWhiteSpace(configDiff) ? "保存基线 Run ID，并让新旧实验上传模型 YAML 后自动生成差异。" : configDiff };
            Add(layout, 6, "配置差异", diff, 160);
            hypothesis.Text = Json.Text(run, "hypothesis"); changes.Text = Json.Text(run, "change_notes"); results.Text = Json.Text(run, "result_notes");
            conclusion.Text = Json.Text(run, "conclusion"); nextStep.Text = Json.Text(run, "next_step"); baseline.Text = Json.Text(run, "baseline_run_id");
            Panel buttons = new Panel { Dock = DockStyle.Bottom, Height = 74, BackColor = Theme.Panel2 };
            Label hint = Theme.Label("保存时才会要求管理员密码；监控系统不会保存密码。", 8.5f, FontStyle.Regular, Theme.Muted); hint.Location = new Point(24, 27);
            Button ok = Theme.Button("保存笔记", true); ok.AutoSize = false; ok.Size = new Size(112, 42); ok.DialogResult = DialogResult.OK;
            Button cancel = Theme.Button("取消", false); cancel.AutoSize = false; cancel.Size = new Size(92, 42); cancel.DialogResult = DialogResult.Cancel;
            buttons.Resize += delegate { cancel.Location = new Point(buttons.ClientSize.Width - 116, 16); ok.Location = new Point(buttons.ClientSize.Width - 238, 16); };
            buttons.Controls.Add(hint); buttons.Controls.Add(ok); buttons.Controls.Add(cancel); Controls.Add(layout); Controls.Add(buttons); Controls.Add(header); AcceptButton = ok; CancelButton = cancel;
        }

        private static TextBox MultiLine() { TextBox box = Theme.TextBox(""); box.Multiline = true; box.ScrollBars = ScrollBars.Vertical; return box; }
        private static void Add(TableLayoutPanel layout, int row, string text, Control control, int height)
        {
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, height)); Label label = Theme.Label(text, 9, FontStyle.Bold, Theme.Muted); label.Dock = DockStyle.Fill; label.TextAlign = ContentAlignment.TopLeft; label.Padding = new Padding(0, 8, 0, 0); control.Dock = DockStyle.Fill; control.Margin = new Padding(4, 5, 4, 9); layout.Controls.Add(label, 0, row); layout.Controls.Add(control, 1, row);
        }

        public Dictionary<string, object> BuildPayload()
        {
            Dictionary<string, object> values = new Dictionary<string, object>();
            values["hypothesis"] = hypothesis.Text; values["change_notes"] = changes.Text; values["result_notes"] = results.Text;
            values["conclusion"] = conclusion.Text; values["next_step"] = nextStep.Text; values["baseline_run_id"] = baseline.Text.Trim();
            return values;
        }
    }
}
