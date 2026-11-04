// Vouch for Windows: the Vouch site in a native window (Microsoft Edge WebView2) plus a guard that keeps distracting
// apps and sites shut until Vouch unlocks them. Every second it looks at the window in front: a browser showing a locked
// site (YouTube, TikTok, ...) or a locked app (Discord) gets minimized and Vouch comes up with that app already picked.
// An approval in the page unlocks that one app for the minutes the AI chose. Closing the window keeps the guard running
// in the tray. One file to download: the WebView2 parts ride inside the exe, and the first run installs it.
// Made by Legion.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Vouch")]
[assembly: AssemblyProduct("Vouch")]
[assembly: AssemblyCompany("Legion")]
[assembly: AssemblyVersion("1.0.0.0")]

static class Program
{
    public const string Url = "https://intentionality-interceptor.vercel.app/";
    public static readonly string Data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Vouch");
    static readonly string Home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Vouch");

    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

    [STAThread]
    static void Main(string[] args)
    {
        bool first;
        using (new System.Threading.Mutex(true, "Vouch.Guard", out first))
        {
            if (!first) return;   // already running (maybe in the tray)
            Directory.CreateDirectory(Data);
            AppDomain.CurrentDomain.AssemblyResolve += (s, e) =>
            {
                using (var st = Assembly.GetExecutingAssembly().GetManifestResourceStream(new AssemblyName(e.Name).Name + ".dll"))
                {
                    if (st == null) return null;
                    var b = new byte[st.Length]; st.Read(b, 0, b.Length); return Assembly.Load(b);
                }
            };
            Unpack("WebView2Loader.dll");
            try { SetProcessDPIAware(); } catch { }
            Install();
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Run(Array.IndexOf(args, "--tray") >= 0);
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    static void Run(bool tray) { Application.Run(new MainForm(tray)); }

    static void Unpack(string name)
    {
        var path = Path.Combine(Data, name);
        using (var st = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        {
            if (st == null || (File.Exists(path) && new FileInfo(path).Length == st.Length)) return;
            using (var f = File.Create(path)) st.CopyTo(f);
        }
    }

    // first run from a download: copy to %LOCALAPPDATA%\Programs\Vouch, add shortcuts, and start with Windows (in the tray)
    static void Install()
    {
        if (Environment.GetEnvironmentVariable("VOUCH_NO_INSTALL") == "1") return;   // test runs
        try
        {
            string me = Assembly.GetExecutingAssembly().Location, target = Path.Combine(Home, "Vouch.exe");
            if (!string.Equals(Path.GetFullPath(me), Path.GetFullPath(target), StringComparison.OrdinalIgnoreCase))
            {
                Directory.CreateDirectory(Home);
                File.Copy(me, target, true);
                dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
                foreach (var dir in new[] { Environment.GetFolderPath(Environment.SpecialFolder.Programs), Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory) })
                {
                    dynamic lnk = shell.CreateShortcut(Path.Combine(dir, "Vouch.lnk"));
                    lnk.TargetPath = target; lnk.WorkingDirectory = Home; lnk.Description = "Vouch"; lnk.IconLocation = target + ",0"; lnk.Save();
                }
            }
            using (var run = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                run.SetValue("Vouch", "\"" + target + "\" --tray");
        }
        catch { }   // installing is a convenience; the app still runs from wherever it is
    }
}

class MainForm : Form
{
    // Locked apps: the name the page uses, words to look for in a browser tab's title, and desktop programs.
    static readonly Dictionary<string, string[]> Sites = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
    {
        { "YouTube", new[] { "YouTube" } },
        { "TikTok", new[] { "TikTok" } },
        { "Instagram", new[] { "Instagram" } },
        { "Reddit", new[] { "Reddit" } },
        { "Netflix", new[] { "Netflix" } },
        { "Discord", new[] { "Discord" } },
        { "Facebook", new[] { "Facebook" } },
        { "Snapchat", new[] { "Snapchat" } },
    };
    static readonly Dictionary<string, string> Programs = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
    {
        { "Discord", "Discord" }, { "Netflix", "Netflix" },
    };
    static readonly HashSet<string> Browsers = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "chrome", "msedge", "firefox", "brave", "opera", "opera_gx", "vivaldi", "arc", "iexplore",
    };

    readonly WebView2 web = new WebView2();
    readonly NotifyIcon tray = new NotifyIcon();
    readonly Timer guard = new Timer { Interval = 1000 };
    readonly string unlockFile = Path.Combine(Program.Data, "unlock.txt");
    readonly string boundsFile = Path.Combine(Program.Data, "window.txt");
    string unlockedApp = "";
    DateTime unlockedUntil = DateTime.MinValue;
    bool quitting, ready;
    string pendingApp;
    DateTime lastBlock = DateTime.MinValue;

    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);

    public MainForm(bool startInTray)
    {
        Text = "Vouch";
        BackColor = Color.FromArgb(246, 245, 241);
        Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location);
        MinimumSize = new Size(420, 600);
        StartPosition = FormStartPosition.Manual;
        var area = Screen.PrimaryScreen.WorkingArea;
        Bounds = new Rectangle(area.X + area.Width / 2 - 320, area.Y + area.Height / 14, 640, area.Height * 12 / 14);
        try
        {
            var p = File.ReadAllText(boundsFile).Split(',');
            var r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
            if (Screen.FromRectangle(r).WorkingArea.IntersectsWith(r)) Bounds = r;
        }
        catch { }
        LoadUnlock();

        web.Dock = DockStyle.Fill;
        web.DefaultBackgroundColor = BackColor;
        Controls.Add(web);

        tray.Icon = Icon;
        tray.Text = "Vouch is guarding your apps";
        tray.Visible = true;
        tray.ContextMenu = new ContextMenu(new[] {
            new MenuItem("Open Vouch", (s, e) => ShowVouch(null)),
            new MenuItem("Quit (stops the lock)", (s, e) => { quitting = true; Close(); }),
        });
        tray.DoubleClick += (s, e) => ShowVouch(null);

        if (startInTray) { ShowInTaskbar = false; WindowState = FormWindowState.Minimized; Load += (s, e) => Hide(); }

        Load += async (s, e) =>
        {
            try
            {
                CoreWebView2Environment.SetLoaderDllFolderPath(Program.Data);
                var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Program.Data, "web"));
                await web.EnsureCoreWebView2Async(env);
            }
            catch (Exception ex)
            {
                if (MessageBox.Show("Vouch needs the Microsoft Edge WebView2 Runtime, which comes with Windows 11.\n\nOpen the download page?\n\n(" + ex.Message + ")", "Vouch", MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                    Open("https://developer.microsoft.com/microsoft-edge/webview2/");
                quitting = true; Close(); return;
            }
            var c = web.CoreWebView2;
            c.Settings.UserAgent += " VouchApp/1 Windows";
            c.Settings.IsStatusBarEnabled = false;
            c.Settings.AreDevToolsEnabled = false;
            // The page talks to this app through window.VouchNative, the same names the Android app uses.
            await c.AddScriptToExecuteOnDocumentCreatedAsync(@"
                window.VouchNative = {
                  unlock: (app, minutes) => chrome.webview.postMessage(JSON.stringify({ t: 'unlock', app: String(app), minutes: Number(minutes) })),
                  lock: () => chrome.webview.postMessage(JSON.stringify({ t: 'lock' })),
                  status: () => '{""usage"":true,""overlay"":true,""guard"":true}'
                };");
            c.WebMessageReceived += (o, a) => OnMessage(a.TryGetWebMessageAsString());
            c.NewWindowRequested += (o, a) => { a.Handled = true; Open(a.Uri); };
            c.NavigationStarting += (o, a) => { if (!a.Uri.StartsWith(Program.Url) && !a.Uri.StartsWith("about:")) { a.Cancel = true; Open(a.Uri); } };
            web.Source = new Uri(Program.Url + (pendingApp != null ? "?app=" + Uri.EscapeDataString(pendingApp) : ""));
            ready = true;
        };

        guard.Tick += (s, e) => Check();
        guard.Start();

        FormClosing += (s, e) =>
        {
            try { if (WindowState == FormWindowState.Normal) File.WriteAllText(boundsFile, string.Join(",", Bounds.X, Bounds.Y, Bounds.Width, Bounds.Height)); } catch { }
            if (!quitting && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;   // keep guarding from the tray
                Hide();
                tray.ShowBalloonTip(3000, "Vouch is still on", "Your apps stay locked. Quit from the tray icon to stop.", ToolTipIcon.None);
                return;
            }
            tray.Visible = false;
        };
        KeyPreview = true;
        KeyDown += (s, e) => { if (e.KeyCode == Keys.F5 && web.CoreWebView2 != null) web.CoreWebView2.Reload(); };
    }

    void OnMessage(string json)
    {
        if (string.IsNullOrEmpty(json)) return;
        if (json.Contains("\"t\":\"lock\""))
        {
            unlockedApp = ""; unlockedUntil = DateTime.MinValue; SaveUnlock();
            return;
        }
        if (!json.Contains("\"t\":\"unlock\"")) return;
        var app = Field(json, "app");
        int minutes;
        if (string.IsNullOrEmpty(app) || !int.TryParse(Field(json, "minutes"), out minutes) || minutes <= 0) return;
        unlockedApp = app;
        unlockedUntil = DateTime.Now.AddMinutes(Math.Min(minutes, 120));
        SaveUnlock();
    }

    // tiny reader for the flat JSON the page sends
    static string Field(string json, string name)
    {
        var key = "\"" + name + "\":";
        int i = json.IndexOf(key, StringComparison.Ordinal);
        if (i < 0) return null;
        i += key.Length;
        if (i < json.Length && json[i] == '"')
        {
            int end = json.IndexOf('"', i + 1);
            return end > i ? json.Substring(i + 1, end - i - 1) : null;
        }
        int stop = i;
        while (stop < json.Length && (char.IsDigit(json[stop]) || json[stop] == '.' || json[stop] == '-')) stop++;
        return json.Substring(i, stop - i).Split('.')[0];
    }

    bool Unlocked(string app) { return string.Equals(app, unlockedApp, StringComparison.OrdinalIgnoreCase) && DateTime.Now < unlockedUntil; }

    void Check()
    {
        try
        {
            IntPtr h = GetForegroundWindow();
            if (h == IntPtr.Zero || h == Handle) return;
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == (uint)Process.GetCurrentProcess().Id) return;
            string proc;
            try { proc = Process.GetProcessById((int)pid).ProcessName; } catch { return; }
            var sb = new StringBuilder(512); GetWindowText(h, sb, sb.Capacity);
            string title = sb.ToString();
            string app = null;
            if (Browsers.Contains(proc))
            {
                foreach (var kv in Sites)
                    foreach (var word in kv.Value)
                        if (title.IndexOf(word, StringComparison.OrdinalIgnoreCase) >= 0) { app = kv.Key; break; }
            }
            else
            {
                foreach (var kv in Programs)
                    if (string.Equals(proc, kv.Value, StringComparison.OrdinalIgnoreCase)) { app = kv.Key; break; }
            }
            if (app == null || Unlocked(app)) return;
            ShowWindow(h, 6);   // SW_MINIMIZE
            if ((DateTime.Now - lastBlock).TotalSeconds > 2) ShowVouch(app);
            lastBlock = DateTime.Now;
        }
        catch { }
    }

    void ShowVouch(string app)
    {
        ShowInTaskbar = true;
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
        SetForegroundWindow(Handle);
        if (app == null) return;
        if (ready && web.CoreWebView2 != null) web.CoreWebView2.Navigate(Program.Url + "?app=" + Uri.EscapeDataString(app));
        else pendingApp = app;
    }

    void LoadUnlock()
    {
        try
        {
            var p = File.ReadAllText(unlockFile).Split('|');
            unlockedApp = p[0];
            unlockedUntil = new DateTime(long.Parse(p[1]));
        }
        catch { }
    }

    void SaveUnlock() { try { File.WriteAllText(unlockFile, unlockedApp + "|" + unlockedUntil.Ticks); } catch { } }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        int round = 2; try { DwmSetWindowAttribute(Handle, 33, ref round, 4); } catch { }        // rounded corners
        int cap = 0x00F1F5F6; try { DwmSetWindowAttribute(Handle, 35, ref cap, 4); } catch { }   // caption colour = page background
    }

    static void Open(string url) { try { if (url.StartsWith("http")) Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); } catch { } }
}
