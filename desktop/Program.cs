using System;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (args.Length > 0 && args[0] == "--self-test")
            {
                Environment.ExitCode = SelfTest.Run();
                return;
            }
            if (args.Length > 1 && args[0] == "--render-preview")
            {
                Environment.ExitCode = PreviewRenderer.Run(args[1]);
                return;
            }
            if (args.Length > 1 && args[0] == "--connection-test")
            {
                Environment.ExitCode = ConnectionTest.Run(args[1]);
                return;
            }

            bool created;
            using (Mutex mutex = new Mutex(true, "YoloMonitorPet.SingleInstance", out created))
            {
                if (!created)
                {
                    MessageBox.Show("Epoch 精灵已经在运行。", "YOLO 实验监控", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                AppSettings settings = AppSettings.Load();
                MonitorState state = new MonitorState(new MonitorApi(settings.ServerUrl));
                Application.Run(new PetForm(state, settings));
            }
        }
    }

    internal static class ConnectionTest
    {
        public static int Run(string serverUrl)
        {
            try
            {
                using (MonitorApi api = new MonitorApi(serverUrl))
                {
                    System.Collections.Generic.List<System.Collections.Generic.Dictionary<string, object>> runs = api.GetRunsAsync().GetAwaiter().GetResult();
                    return runs == null ? 2 : 0;
                }
            }
            catch { return 1; }
        }
    }

    internal static class PreviewRenderer
    {
        public static int Run(string directory)
        {
            try
            {
                Directory.CreateDirectory(directory);
                AppSettings settings = AppSettings.Default();
                using (DashboardForm dashboard = new DashboardForm(null, settings))
                using (System.Drawing.Bitmap image = new System.Drawing.Bitmap(1280, 780))
                {
                    dashboard.ClientSize = new System.Drawing.Size(1280, 780);
                    dashboard.StartPosition = FormStartPosition.Manual;
                    dashboard.Location = new System.Drawing.Point(-20000, -20000);
                    dashboard.ShowInTaskbar = false;
                    dashboard.Show();
                    Application.DoEvents();
                    dashboard.DrawToBitmap(image, new System.Drawing.Rectangle(0, 0, image.Width, image.Height));
                    image.Save(Path.Combine(directory, "dashboard-preview.png"), System.Drawing.Imaging.ImageFormat.Png);
                    dashboard.Hide();
                }
                using (MonitorState state = new MonitorState(new MonitorApi(settings.ServerUrl)))
                using (PetForm pet = new PetForm(state, settings))
                using (System.Drawing.Bitmap image = new System.Drawing.Bitmap(158, 172))
                {
                    pet.CreateControl();
                    pet.DrawToBitmap(image, new System.Drawing.Rectangle(0, 0, image.Width, image.Height));
                    image.MakeTransparent(System.Drawing.Color.FromArgb(1, 2, 3));
                    image.Save(Path.Combine(directory, "pet-preview.png"), System.Drawing.Imaging.ImageFormat.Png);
                }
                return 0;
            }
            catch { return 1; }
        }
    }

    internal static class SelfTest
    {
        public static int Run()
        {
            try
            {
                object value = Json.Deserialize("{\"runs\":[{\"id\":\"x\",\"name\":\"demo\",\"status\":\"running\",\"current_epoch\":1,\"total_epochs\":100}]}");
                System.Collections.Generic.Dictionary<string, object> root = Json.Dict(value);
                object[] runs = Json.Array(root["runs"]);
                System.Collections.Generic.Dictionary<string, object> run = Json.Dict(runs[0]);
                if (Json.Text(run, "id") != "x" || Json.Int(run, "total_epochs") != 100)
                    return 2;
                if (!UpdateManager.IsNewer("1.2.1", "1.2.0") || UpdateManager.IsNewer("1.2.0", "1.2.0"))
                    return 3;
                if (UpdateManager.Sha256Hex(System.Text.Encoding.ASCII.GetBytes("abc")) != "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD")
                    return 4;
                UpdateInfo update = UpdateManager.ParseManifest("{\"version\":\"2.0.0\",\"download_url\":\"https://monitor.maocong.me/downloads/YoloMonitorPet.exe\",\"sha256\":\"0000000000000000000000000000000000000000000000000000000000000000\",\"size\":123}", new Uri("https://monitor.maocong.me/"));
                if (update.Size != 123 || update.Version != "2.0.0") return 5;
                using (TrendChart chart = new TrendChart())
                using (DashboardForm dashboard = new DashboardForm(null, AppSettings.Default()))
                using (HoverCardForm hover = new HoverCardForm())
                using (MonitorApi queueApi = new MonitorApi(AppSettings.Default().ServerUrl))
                using (QueueForm queueForm = new QueueForm(queueApi))
                using (ExperimentNotesDialog notes = new ExperimentNotesDialog(new System.Collections.Generic.Dictionary<string, object>(), ""))
                {
                    chart.Size = new System.Drawing.Size(600, 300);
                    dashboard.CreateControl();
                    hover.CreateControl();
                    queueForm.CreateControl();
                    notes.CreateControl();
                }
                AppSettings settings = AppSettings.Default();
                settings.HasPosition = true;
                settings.Left = -20000;
                settings.Top = -20000;
                using (MonitorState state = new MonitorState(new MonitorApi(settings.ServerUrl)))
                using (PetForm pet = new PetForm(state, settings))
                {
                    pet.CreateControl();
                    pet.ShowHover();
                    pet.ShowHover();
                    pet.Close();
                }
                return 0;
            }
            catch
            {
                return 1;
            }
        }
    }
}
