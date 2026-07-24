using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YoloMonitorPet
{
    internal sealed class UpdateInfo
    {
        public string Version { get; set; }
        public Uri DownloadUri { get; set; }
        public string Sha256 { get; set; }
        public long Size { get; set; }
        public string Notes { get; set; }
    }

    internal static class UpdateManager
    {
        private const long MaximumDownloadSize = 50L * 1024L * 1024L;

        public static string CurrentVersion
        {
            get
            {
                Version version = Assembly.GetExecutingAssembly().GetName().Version;
                return string.Format("{0}.{1}.{2}", version.Major, version.Minor, Math.Max(0, version.Build));
            }
        }

        public static async Task<UpdateInfo> CheckAsync(string serverUrl)
        {
            Uri server;
            if (!Uri.TryCreate((serverUrl ?? "").TrimEnd('/') + "/", UriKind.Absolute, out server) ||
                !string.Equals(server.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("更新检查要求使用有效的 HTTPS 服务器地址。");

            using (HttpClientHandler handler = new HttpClientHandler())
            using (HttpClient client = new HttpClient(handler))
            {
                handler.AllowAutoRedirect = false;
                handler.AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate;
                client.Timeout = TimeSpan.FromSeconds(15);
                client.DefaultRequestHeaders.UserAgent.ParseAdd("YoloMonitorPet/" + CurrentVersion);
                string json = await client.GetStringAsync(new Uri(server, "api/v1/public/desktop/latest"));
                UpdateInfo info = ParseManifest(json, server);
                return IsNewer(info.Version, CurrentVersion) ? info : null;
            }
        }

        internal static UpdateInfo ParseManifest(string json, Uri server)
        {
            Dictionary<string, object> data = Json.Dict(Json.Deserialize(json));
            string versionText = Json.Text(data, "version").Trim().TrimStart('v', 'V');
            string downloadText = Json.Text(data, "download_url").Trim();
            string sha256 = Json.Text(data, "sha256").Trim().ToUpperInvariant();
            long size;
            try { size = Convert.ToInt64(data["size"]); }
            catch { throw new InvalidOperationException("服务器返回的更新文件大小无效。"); }

            Version ignored;
            Uri downloadUri;
            if (!Version.TryParse(versionText, out ignored))
                throw new InvalidOperationException("服务器返回的更新版本号无效。");
            if (!Uri.TryCreate(downloadText, UriKind.Absolute, out downloadUri) ||
                !string.Equals(downloadUri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("服务器返回的更新下载地址不是 HTTPS。");
            if (!string.Equals(downloadUri.Host, server.Host, StringComparison.OrdinalIgnoreCase) || downloadUri.Port != server.Port)
                throw new InvalidOperationException("更新下载地址与监控服务器不属于同一主机。");
            if (sha256.Length != 64 || !IsHex(sha256))
                throw new InvalidOperationException("服务器返回的 SHA-256 校验值无效。");
            if (size <= 0 || size > MaximumDownloadSize)
                throw new InvalidOperationException("更新文件大小超出安全范围。");

            return new UpdateInfo
            {
                Version = versionText,
                DownloadUri = downloadUri,
                Sha256 = sha256,
                Size = size,
                Notes = Json.Text(data, "notes", "本次更新未提供说明。")
            };
        }

        public static async Task DownloadVerifyAndRestartAsync(UpdateInfo info)
        {
            string target = Application.ExecutablePath;
            string targetDirectory = Path.GetDirectoryName(target);
            string writeTest = Path.Combine(targetDirectory, ".yolo-update-write-test-" + Guid.NewGuid().ToString("N"));
            try
            {
                File.WriteAllText(writeTest, "ok", Encoding.ASCII);
                File.Delete(writeTest);
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException("程序所在目录不可写，无法自动更新。请把程序移动到个人文件夹后重试。", ex);
            }

            byte[] bytes;
            using (HttpClientHandler handler = new HttpClientHandler())
            using (HttpClient client = new HttpClient(handler))
            {
                handler.AllowAutoRedirect = false;
                client.Timeout = TimeSpan.FromMinutes(3);
                client.DefaultRequestHeaders.UserAgent.ParseAdd("YoloMonitorPet/" + CurrentVersion);
                using (HttpResponseMessage response = await client.GetAsync(info.DownloadUri, HttpCompletionOption.ResponseHeadersRead))
                {
                    response.EnsureSuccessStatusCode();
                    if (response.Content.Headers.ContentLength.HasValue && response.Content.Headers.ContentLength.Value != info.Size)
                        throw new InvalidOperationException("下载文件大小与更新清单不一致。");
                    using (Stream input = await response.Content.ReadAsStreamAsync())
                    using (MemoryStream output = new MemoryStream())
                    {
                        byte[] buffer = new byte[64 * 1024];
                        int read;
                        while ((read = await input.ReadAsync(buffer, 0, buffer.Length)) > 0)
                        {
                            output.Write(buffer, 0, read);
                            if (output.Length > MaximumDownloadSize || output.Length > info.Size)
                                throw new InvalidOperationException("下载文件超过更新清单声明的大小。");
                        }
                        bytes = output.ToArray();
                    }
                }
            }

            if (bytes.LongLength != info.Size)
                throw new InvalidOperationException("下载文件不完整，请稍后重试。");
            string actualHash = Sha256Hex(bytes);
            if (!string.Equals(actualHash, info.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("更新文件 SHA-256 校验失败，已拒绝安装。");

            string temporary = Path.Combine(Path.GetTempPath(), "YoloMonitorPet-update-" + info.Version + "-" + Guid.NewGuid().ToString("N") + ".exe");
            File.WriteAllBytes(temporary, bytes);
            StartReplacementProcess(temporary, target);
            Application.Exit();
        }

        internal static bool IsNewer(string candidate, string current)
        {
            Version candidateVersion;
            Version currentVersion;
            if (!Version.TryParse((candidate ?? "").Trim().TrimStart('v', 'V'), out candidateVersion)) return false;
            if (!Version.TryParse((current ?? "").Trim().TrimStart('v', 'V'), out currentVersion)) return false;
            return Normalize(candidateVersion).CompareTo(Normalize(currentVersion)) > 0;
        }

        internal static string Sha256Hex(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(bytes);
                StringBuilder result = new StringBuilder(hash.Length * 2);
                foreach (byte value in hash) result.Append(value.ToString("X2"));
                return result.ToString();
            }
        }

        private static Version Normalize(Version version)
        {
            return new Version(version.Major, version.Minor, Math.Max(0, version.Build), Math.Max(0, version.Revision));
        }

        private static bool IsHex(string value)
        {
            foreach (char character in value)
            {
                bool valid = (character >= '0' && character <= '9') || (character >= 'A' && character <= 'F');
                if (!valid) return false;
            }
            return true;
        }

        private static void StartReplacementProcess(string temporary, string target)
        {
            string script =
                "$ErrorActionPreference='Stop';" +
                "try { Wait-Process -Id " + Process.GetCurrentProcess().Id + " -Timeout 60 -ErrorAction SilentlyContinue } catch {};" +
                "Copy-Item -LiteralPath '" + EscapePowerShell(temporary) + "' -Destination '" + EscapePowerShell(target) + "' -Force;" +
                "Start-Process -FilePath '" + EscapePowerShell(target) + "';" +
                "Remove-Item -LiteralPath '" + EscapePowerShell(temporary) + "' -Force -ErrorAction SilentlyContinue";
            string encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
            ProcessStartInfo start = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded,
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            Process.Start(start);
        }

        private static string EscapePowerShell(string value) { return (value ?? "").Replace("'", "''"); }
    }

    internal static class UpdateWorkflow
    {
        public static async Task<UpdateInfo> CheckAndPromptAsync(IWin32Window owner, string serverUrl, bool silent, Action<string> setStatus)
        {
            try
            {
                if (setStatus != null) setStatus("检查中…");
                UpdateInfo info = await UpdateManager.CheckAsync(serverUrl);
                if (info == null)
                {
                    if (!silent) MessageBox.Show(owner, "当前已是最新版本（" + UpdateManager.CurrentVersion + "）。", "检查更新", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return null;
                }

                string message = "发现新版本 " + info.Version + "\n\n" + info.Notes + "\n\n文件大小：" + FormatSize(info.Size) + "\n\n现在下载并安装吗？";
                if (MessageBox.Show(owner, message, "YOLO Epoch 精灵更新", MessageBoxButtons.YesNo, MessageBoxIcon.Information, MessageBoxDefaultButton.Button1) != DialogResult.Yes)
                    return info;
                if (setStatus != null) setStatus("下载更新…");
                await UpdateManager.DownloadVerifyAndRestartAsync(info);
                return info;
            }
            catch (Exception ex)
            {
                if (!silent) MessageBox.Show(owner, ex.Message, "更新失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return null;
            }
            finally
            {
                if (setStatus != null) setStatus("检查更新");
            }
        }

        private static string FormatSize(long size)
        {
            return size >= 1024 * 1024 ? (size / 1024d / 1024d).ToString("0.0") + " MB" : (size / 1024d).ToString("0.0") + " KB";
        }
    }
}
