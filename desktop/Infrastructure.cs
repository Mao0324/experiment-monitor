using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace YoloMonitorPet
{
    internal static class Json
    {
        private static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };

        public static object Deserialize(string value) { return Serializer.DeserializeObject(value); }
        public static string Serialize(object value) { return Serializer.Serialize(value); }
        public static Dictionary<string, object> Dict(object value) { return value as Dictionary<string, object> ?? new Dictionary<string, object>(); }
        public static object[] Array(object value) { return value as object[] ?? new object[0]; }
        public static string Text(Dictionary<string, object> data, string key, string fallback)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : fallback;
        }
        public static string Text(Dictionary<string, object> data, string key) { return Text(data, key, ""); }
        public static int Int(Dictionary<string, object> data, string key)
        {
            object value;
            if (data == null || !data.TryGetValue(key, out value) || value == null) return 0;
            try { return Convert.ToInt32(value, CultureInfo.InvariantCulture); } catch { return 0; }
        }
        public static double Number(Dictionary<string, object> data, string key, double fallback)
        {
            object value;
            if (data == null || !data.TryGetValue(key, out value) || value == null) return fallback;
            try { return Convert.ToDouble(value, CultureInfo.InvariantCulture); } catch { return fallback; }
        }
        public static bool Bool(Dictionary<string, object> data, string key)
        {
            object value;
            if (data == null || !data.TryGetValue(key, out value) || value == null) return false;
            try { return Convert.ToBoolean(value, CultureInfo.InvariantCulture); } catch { return false; }
        }
        public static Dictionary<string, object> Child(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) ? Dict(value) : new Dictionary<string, object>();
        }
        public static object[] Children(Dictionary<string, object> data, string key)
        {
            object value;
            return data != null && data.TryGetValue(key, out value) ? Array(value) : new object[0];
        }
    }

    internal sealed class AppSettings
    {
        public string ServerUrl { get; set; }
        public int Left { get; set; }
        public int Top { get; set; }
        public bool HasPosition { get; set; }
        public string VisualStyle { get; set; }

        public static AppSettings Default()
        {
            return new AppSettings { ServerUrl = "https://monitor.maocong.me", VisualStyle = "material" };
        }

        private static string SettingsPath
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "YoloMonitorPet", "settings.json");
            }
        }

        public static AppSettings Load()
        {
            try
            {
                if (!File.Exists(SettingsPath)) return Default();
                Dictionary<string, object> root = Json.Dict(Json.Deserialize(File.ReadAllText(SettingsPath, Encoding.UTF8)));
                AppSettings settings = Default();
                settings.ServerUrl = Json.Text(root, "server_url", settings.ServerUrl).TrimEnd('/');
                settings.Left = Json.Int(root, "left");
                settings.Top = Json.Int(root, "top");
                settings.HasPosition = Json.Bool(root, "has_position");
                settings.VisualStyle = Json.Text(root, "visual_style", settings.VisualStyle);
                return settings;
            }
            catch { return Default(); }
        }

        public void Save()
        {
            string directory = Path.GetDirectoryName(SettingsPath);
            Directory.CreateDirectory(directory);
            Dictionary<string, object> root = new Dictionary<string, object>();
            root["server_url"] = ServerUrl;
            root["left"] = Left;
            root["top"] = Top;
            root["has_position"] = HasPosition;
            root["visual_style"] = VisualStyle;
            File.WriteAllText(SettingsPath, Json.Serialize(root), Encoding.UTF8);
        }
    }

    internal sealed class AdminPasswordException : Exception
    {
        public AdminPasswordException() : base("管理员密码错误") { }
    }

    internal sealed class MonitorApi : IDisposable
    {
        private readonly HttpClient client;
        public string BaseUrl { get; private set; }

        public MonitorApi(string baseUrl)
        {
            BaseUrl = (baseUrl ?? "").Trim().TrimEnd('/');
            HttpClientHandler handler = new HttpClientHandler();
            handler.AllowAutoRedirect = true;
            handler.AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate;
            client = new HttpClient(handler);
            client.Timeout = TimeSpan.FromSeconds(12);
            client.DefaultRequestHeaders.UserAgent.ParseAdd("YoloMonitorPet/1.0");
        }

        public async Task<List<Dictionary<string, object>>> GetRunsAsync()
        {
            string json = await client.GetStringAsync(BaseUrl + "/api/v1/public/runs");
            Dictionary<string, object> root = Json.Dict(Json.Deserialize(json));
            return Json.Children(root, "runs").Select(Json.Dict).ToList();
        }

        public async Task<Dictionary<string, object>> GetRunAsync(string runId)
        {
            string json = await client.GetStringAsync(BaseUrl + "/api/v1/public/runs/" + Uri.EscapeDataString(runId));
            return Json.Dict(Json.Deserialize(json));
        }

        public async Task<Dictionary<string, object>> GetQueueAsync()
        {
            string json = await client.GetStringAsync(BaseUrl + "/api/v1/public/queue");
            return Json.Dict(Json.Deserialize(json));
        }

        private async Task<Dictionary<string, object>> PostWebJsonAsync(string path, Dictionary<string, object> payload)
        {
            HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + path);
            request.Headers.Add("X-Monitor-Request", "dashboard");
            request.Content = new StringContent(Json.Serialize(payload), Encoding.UTF8, "application/json");
            HttpResponseMessage response = await client.SendAsync(request);
            if (response.StatusCode == HttpStatusCode.Forbidden) throw new AdminPasswordException();
            response.EnsureSuccessStatusCode();
            return Json.Dict(Json.Deserialize(await response.Content.ReadAsStringAsync()));
        }

        public async Task CreateQueueJobAsync(Dictionary<string, object> payload, string password)
        {
            payload["password"] = password ?? "";
            await PostWebJsonAsync("/api/v1/web/queue", payload);
        }

        public async Task QueueActionAsync(string jobId, string action, string password)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>();
            payload["action"] = action;
            payload["password"] = password ?? "";
            await PostWebJsonAsync("/api/v1/web/queue/" + Uri.EscapeDataString(jobId) + "/action", payload);
        }

        public async Task SaveExperimentNotesAsync(string runId, Dictionary<string, object> notes, string password)
        {
            notes["password"] = password ?? "";
            await PostWebJsonAsync("/api/v1/web/runs/" + Uri.EscapeDataString(runId) + "/metadata", notes);
        }

        public async Task SaveMetadataAsync(string runId, string group, string tags, bool favorite, string password)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>();
            payload["group_name"] = group ?? "";
            payload["tags"] = tags ?? "";
            payload["favorite"] = favorite;
            payload["password"] = password ?? "";
            HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/api/v1/web/runs/" + Uri.EscapeDataString(runId) + "/metadata");
            request.Headers.Add("X-Monitor-Request", "dashboard");
            request.Content = new StringContent(Json.Serialize(payload), Encoding.UTF8, "application/json");
            HttpResponseMessage response = await client.SendAsync(request);
            if (response.StatusCode == HttpStatusCode.Forbidden) throw new AdminPasswordException();
            response.EnsureSuccessStatusCode();
        }

        public async Task DeleteRunAsync(string runId, string password)
        {
            List<KeyValuePair<string, string>> values = new List<KeyValuePair<string, string>>();
            values.Add(new KeyValuePair<string, string>("password", password ?? ""));
            HttpResponseMessage response = await client.PostAsync(BaseUrl + "/runs/" + Uri.EscapeDataString(runId) + "/delete", new FormUrlEncodedContent(values));
            if (response.StatusCode == HttpStatusCode.Forbidden) throw new AdminPasswordException();
            response.EnsureSuccessStatusCode();
        }

        public void Dispose() { client.Dispose(); }
    }

    internal sealed class MonitorState : IDisposable
    {
        private MonitorApi api;
        private bool refreshing;
        public List<Dictionary<string, object>> Runs { get; private set; }
        public string Error { get; private set; }
        public DateTime LastSuccess { get; private set; }
        public event EventHandler Updated;

        public MonitorApi Api { get { return api; } }

        public MonitorState(MonitorApi api)
        {
            this.api = api;
            Runs = new List<Dictionary<string, object>>();
        }

        public async Task RefreshAsync()
        {
            if (refreshing) return;
            refreshing = true;
            try
            {
                Runs = await api.GetRunsAsync();
                Error = null;
                LastSuccess = DateTime.Now;
            }
            catch (Exception ex)
            {
                Error = ex.Message;
            }
            finally
            {
                refreshing = false;
                EventHandler handler = Updated;
                if (handler != null) handler(this, EventArgs.Empty);
            }
        }

        public void ChangeServer(string serverUrl)
        {
            MonitorApi old = api;
            api = new MonitorApi(serverUrl);
            Runs = new List<Dictionary<string, object>>();
            Error = null;
            old.Dispose();
        }

        public Dictionary<string, object> PrimaryRun()
        {
            Dictionary<string, object> running = Runs.FirstOrDefault(delegate(Dictionary<string, object> run) { return Json.Text(run, "status") == "running"; });
            if (running != null) return running;
            Dictionary<string, object> stalled = Runs.FirstOrDefault(delegate(Dictionary<string, object> run) { return Json.Text(run, "status") == "stalled"; });
            return stalled ?? Runs.FirstOrDefault();
        }

        public void Dispose() { api.Dispose(); }
    }
}
