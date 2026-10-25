using System.Diagnostics;
using System.Drawing;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32;
using System.Windows.Forms;

namespace LocalAIAgentSetup;

internal static class Program
{
    private const string Product = "本地语音助手";
    private const string QwenUrl = "https://huggingface.co/Qwen/Qwen3-8B";
    private const string AsrUrl = "https://huggingface.co/Qwen/Qwen3-ASR-1.7B";

    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        string here = AppContext.BaseDirectory;
        bool installed = File.Exists(Path.Combine(here, "release-manifest.json"));
        Application.Run(new MainForm(installed && !args.Contains("--setup")));
    }

    private sealed class MainForm : Form
    {
        private readonly bool installed;
        private readonly TextBox folder = new() { Width = 410 };
        private readonly TextBox apiKey = new() { Width = 330, UseSystemPasswordChar = true };
        private readonly CheckBox desktopShortcut = new() { Text = "创建桌面快捷方式", Checked = true, AutoSize = true };
        private readonly Label report = new() { AutoSize = false, Width = 580, Height = 180 };
        private readonly Label status = new() { AutoSize = false, Width = 580, Height = 42 };

        public MainForm(bool installed)
        {
            this.installed = installed;
            Text = installed ? Product : Product + " · 安装 v0.1.1 预览版";
            ClientSize = new Size(640, installed ? 560 : 645);
            MinimumSize = Size;
            MaximumSize = Size;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Microsoft YaHei UI", 9F);
            BackColor = Color.FromArgb(23, 24, 31);
            ForeColor = Color.WhiteSmoke;

            var layout = new FlowLayoutPanel {
                Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
                WrapContents = false, AutoScroll = true, Padding = new Padding(22)
            };
            Controls.Add(layout);
            var heading = new Label {
                Text = installed ? "本地语音助手" : "安装本地语音助手 · v0.1.1",
                Font = new Font("Microsoft YaHei UI", 17F, FontStyle.Bold),
                Width = 580, Height = 45
            };
            layout.Controls.Add(heading);
            layout.Controls.Add(NewLabel("主贡献人：@01hhhLLL", 28));
            layout.Controls.Add(NewLabel("龙娘语音助手。安装包不含模型、账号、测试数据或 API 密钥。", 42));

            if (!installed)
            {
                layout.Controls.Add(NewLabel("安装位置（请选择空文件夹）", 27));
                folder.Text = Path.Combine(Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData), "Programs", "LocalAI Agent");
                var row = new FlowLayoutPanel { Width = 580, Height = 38 };
                row.Controls.Add(folder);
                row.Controls.Add(MakeButton("浏览…", (_, _) => Browse()));
                layout.Controls.Add(row);
                layout.Controls.Add(desktopShortcut);
                layout.Controls.Add(NewLabel("视觉 API 密钥（可留空；仅存入 Windows 凭据管理器）", 29));
                layout.Controls.Add(apiKey);
                layout.Controls.Add(NewLabel("跳过后：本地 OCR 仍可读清晰文字；图片理解、OCR 失败时的定位兜底及依赖它的画面核验将不可用。", 57));
            }

            layout.Controls.Add(NewLabel("硬件和运行环境", 28));
            report.Text = Diagnostics(installed ? AppContext.BaseDirectory : folder.Text);
            layout.Controls.Add(report);
            var actions = new FlowLayoutPanel { Width = 590, Height = 45 };
            actions.Controls.Add(MakeButton(installed ? "检查并启动" : "安装到所选位置",
                installed ? Run : Install));
            actions.Controls.Add(MakeButton("模型下载说明", (_, _) => DownloadPages()));
            actions.Controls.Add(MakeButton("刷新检查", (_, _) =>
                report.Text = Diagnostics(installed ? AppContext.BaseDirectory : folder.Text)));
            layout.Controls.Add(actions);
            layout.Controls.Add(status);
            TryAddIcon(layout);
        }

        private static Label NewLabel(string text, int height) => new() {
            Text = text, Width = 580, Height = height, AutoSize = false
        };

        private static Button MakeButton(string text, EventHandler handler)
        {
            var button = new Button { Text = text, AutoSize = true, Height = 34,
                BackColor = Color.FromArgb(84, 61, 141), ForeColor = Color.White,
                FlatStyle = FlatStyle.Flat };
            button.Click += handler;
            return button;
        }

        private void TryAddIcon(FlowLayoutPanel layout)
        {
            try
            {
                using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("LocalAI.Payload");
                if (stream == null) return;
                using var archive = new ZipArchive(stream);
                var entry = archive.GetEntry("assets/dragon-voice-icon-head-v1.png");
                if (entry == null) return;
                using var imageStream = entry.Open();
                using var image = Image.FromStream(imageStream);
                layout.Controls.Add(new PictureBox { Image = new Bitmap(image),
                    SizeMode = PictureBoxSizeMode.Zoom, Width = 112, Height = 112 });
            }
            catch (Exception) { /* Icon failure never blocks diagnostics. */ }
        }

        private void Browse()
        {
            using var dialog = new FolderBrowserDialog {
                Description = "选择本地语音助手的安装目录", UseDescriptionForTitle = true
            };
            if (dialog.ShowDialog(this) == DialogResult.OK) folder.Text = dialog.SelectedPath;
        }

        private void Install(object? sender, EventArgs e)
        {
            try
            {
                string target = Path.GetFullPath(folder.Text.Trim());
                if (Path.GetPathRoot(target) == target)
                    throw new InvalidOperationException("不能安装在磁盘根目录。");
                if (Directory.Exists(target) && Directory.EnumerateFileSystemEntries(target).Any())
                    throw new InvalidOperationException("目标文件夹已有内容；请选择空文件夹，安装器不会覆盖现有文件。");
                var drive = new DriveInfo(Path.GetPathRoot(target)!);
                if (drive.AvailableFreeSpace < 500_000_000)
                    throw new InvalidOperationException("磁盘剩余空间不足 500 MB。模型还需要更多空间。");
                using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("LocalAI.Payload")
                    ?? throw new InvalidOperationException("安装包缺少应用文件。");
                using var archive = new ZipArchive(stream);
                string basePath = Path.TrimEndingDirectorySeparator(target) + Path.DirectorySeparatorChar;
                foreach (var entry in archive.Entries)
                {
                    string destination = Path.GetFullPath(Path.Combine(target,
                        entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                    if (!destination.StartsWith(basePath, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("安装包包含非法路径。");
                }
                Directory.CreateDirectory(target);
                foreach (var entry in archive.Entries)
                {
                    if (entry.FullName.EndsWith('/')) continue;
                    string destination = Path.Combine(target,
                        entry.FullName.Replace('/', Path.DirectorySeparatorChar));
                    Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                    using var source = entry.Open();
                    using var output = new FileStream(destination, FileMode.CreateNew);
                    source.CopyTo(output);
                }
                string app = Path.Combine(target, "LocalAI-Agent.exe");
                File.Copy(Application.ExecutablePath, app, overwrite: false);
                if (!string.IsNullOrWhiteSpace(apiKey.Text))
                    SaveCredential(apiKey.Text.Trim());
                apiKey.Clear();
                CreateShortcut(Path.Combine(Environment.GetFolderPath(
                    Environment.SpecialFolder.Programs), "LocalAI Agent.lnk"), app);
                if (desktopShortcut.Checked)
                    CreateShortcut(Path.Combine(Environment.GetFolderPath(
                        Environment.SpecialFolder.DesktopDirectory), "LocalAI Agent.lnk"), app);
                report.Text = Diagnostics(target);
                status.Text = "安装文件已复制。运行环境仍需按上方检查结果准备；未就绪时不会假报可用。";
                MessageBox.Show(this, status.Text, Product, MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            catch (Exception error)
            {
                status.Text = "安装未完成：" + error.Message;
                MessageBox.Show(this, status.Text, Product, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private void Run(object? sender, EventArgs e)
        {
            string root = AppContext.BaseDirectory;
            var missing = MissingRuntime(root);
            if (missing.Count > 0)
            {
                status.Text = "尚未就绪：" + string.Join("、", missing) + "。请先按技术文档准备，不能假装已启动。";
                MessageBox.Show(this, status.Text, Product, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo("powershell.exe") {
                    Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" +
                        Path.Combine(root, "scripts", "voice-hotkey-desktop.ps1") + "\"",
                    WorkingDirectory = root, UseShellExecute = false,
                    CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden
                });
                status.Text = "启动请求已发出；以随后出现的热键就绪提示为准。";
            }
            catch (Exception error) { status.Text = "启动失败：" + error.Message; }
        }

        private static List<string> MissingRuntime(string root)
        {
            var required = new Dictionary<string, string> {
                ["Python 语音环境"] = "runtime/asr/Scripts/pythonw.exe",
                ["Python ASR 环境"] = "runtime/asr-compare-qwen/Scripts/python.exe",
                ["Python 任务环境"] = "runtime/foundation/Scripts/python.exe",
                ["Python 文本环境"] = "runtime/fast/Scripts/python.exe",
                ["Hermes"] = "runtime/hermes-home/bin/hermes.cmd",
                ["本地文本模型"] = "models/model-qwen3-8b/source/config.json",
                ["本地语音模型"] = "models/asr-comparison/qwen3-asr-1.7b/source/config.json",
            };
            return required.Where(item => !File.Exists(Path.Combine(root,
                item.Value.Replace('/', Path.DirectorySeparatorChar))))
                .Select(item => item.Key).ToList();
        }

        private static string Diagnostics(string root)
        {
            var lines = new List<string> {
                Environment.Is64BitOperatingSystem ? "✓ Windows 64 位" : "✗ 需要 Windows 64 位",
                "系统内存：" + (AvailableMemoryGiB()?.ToString("0.0") ?? "未知") + " GiB 可用",
            };
            string gpu = GpuInfo();
            lines.Add("显卡：" + gpu);
            lines.Add("本地组件缺失：" + (MissingRuntime(root).Count == 0 ? "无" :
                string.Join("、", MissingRuntime(root))));
            lines.Add("视觉 API：需由每位用户单独配置；留空时不能使用图片理解兜底。");
            return string.Join(Environment.NewLine, lines);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MemoryStatus {
            public uint Length, MemoryLoad;
            public ulong TotalPhysical, AvailablePhysical, TotalPageFile,
                AvailablePageFile, TotalVirtual, AvailableVirtual, AvailableExtendedVirtual;
        }
        [DllImport("kernel32.dll")]
        private static extern bool GlobalMemoryStatusEx(ref MemoryStatus status);
        private static double? AvailableMemoryGiB()
        {
            var state = new MemoryStatus { Length = (uint)Marshal.SizeOf<MemoryStatus>() };
            return GlobalMemoryStatusEx(ref state) ? state.AvailablePhysical / 1073741824.0 : null;
        }
        private static string GpuInfo()
        {
            try
            {
                using var process = Process.Start(new ProcessStartInfo("nvidia-smi") {
                    Arguments = "--query-gpu=name,memory.free --format=csv,noheader,nounits",
                    RedirectStandardOutput = true, RedirectStandardError = true,
                    UseShellExecute = false, CreateNoWindow = true
                });
                if (process == null || !process.WaitForExit(3000)) {
                    try { process?.Kill(); } catch (InvalidOperationException) { }
                    return "未检测到可用 NVIDIA 信息";
                }
                string output = process.StandardOutput.ReadToEnd().Trim();
                return process.ExitCode == 0 && output.Length > 0 ? output : "未检测到可用 NVIDIA 信息";
            }
            catch (Exception) { return "未检测到 NVIDIA CUDA 工具；本地 Qwen 无法验证可运行"; }
        }

        private static void DownloadPages()
        {
            Process.Start(new ProcessStartInfo(QwenUrl) { UseShellExecute = true });
            Process.Start(new ProcessStartInfo(AsrUrl) { UseShellExecute = true });
        }

        private static void CreateShortcut(string path, string executable)
        {
            if (File.Exists(path)) return;
            Type shellType = Type.GetTypeFromProgID("WScript.Shell")
                ?? throw new InvalidOperationException("Windows 快捷方式服务不可用");
            dynamic shell = Activator.CreateInstance(shellType)!;
            dynamic shortcut = shell.CreateShortcut(path);
            shortcut.TargetPath = executable;
            shortcut.WorkingDirectory = Path.GetDirectoryName(executable);
            shortcut.Description = "本地语音助手";
            shortcut.Save();
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Credential {
            public uint Flags, Type;
            public string TargetName;
            public string? Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public uint CredentialBlobSize;
            public IntPtr CredentialBlob;
            public uint Persist, AttributeCount;
            public IntPtr Attributes;
            public string? TargetAlias;
            public string UserName;
        }
        [DllImport("Advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CredWriteW(ref Credential credential, uint flags);
        private static void SaveCredential(string value)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(value);
            if (bytes.Length is 0 or > 2560 || bytes.Any(b => b < 32))
                throw new InvalidOperationException("视觉 API 密钥为空或格式无效。");
            IntPtr buffer = Marshal.AllocHGlobal(bytes.Length);
            try
            {
                Marshal.Copy(bytes, 0, buffer, bytes.Length);
                var credential = new Credential {
                    Type = 1, TargetName = "LocalAI-Agent/ModelScopeVision",
                    CredentialBlobSize = (uint)bytes.Length, CredentialBlob = buffer,
                    Persist = 2, UserName = "ModelScope"
                };
                if (!CredWriteW(ref credential, 0))
                    throw new InvalidOperationException("Windows 未能保存视觉 API 密钥。");
            }
            finally {
                for (int i = 0; i < bytes.Length; i++) Marshal.WriteByte(buffer, i, 0);
                Marshal.FreeHGlobal(buffer);
                Array.Clear(bytes);
            }
        }
    }
}
