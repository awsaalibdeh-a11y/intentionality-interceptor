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
    readonly Timer guard = new Timer { Interval = 500 };
    readonly LockCard card;
    readonly string unlockFile = Path.Combine(Program.Data, "unlock.txt");
    readonly string boundsFile = Path.Combine(Program.Data, "window.txt");
    string unlockedApp = "";
    DateTime unlockedUntil = DateTime.MinValue;
    bool quitting, ready;
    string pendingUrl;

    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr h);
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
        card = new LockCard(this);

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
            web.Source = new Uri(pendingUrl ?? Program.Url);
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
        unlockedUntil = DateTime.Now.AddMinutes(Math.Min(minutes, app == "*" ? 1440 : 120));
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

    bool Unlocked(string app) { return (string.Equals(app, unlockedApp, StringComparison.OrdinalIgnoreCase) || unlockedApp == "*") && DateTime.Now < unlockedUntil; }   // "*" = bad day

    /// <summary>The locked app a window is showing (a browser tab's title or a locked program), or null.</summary>
    static string LockedAppIn(IntPtr h)
    {
        if (h == IntPtr.Zero || IsIconic(h) || !IsWindowVisible(h)) return null;
        uint pid; GetWindowThreadProcessId(h, out pid);
        if (pid == (uint)Process.GetCurrentProcess().Id) return null;
        string proc;
        try { proc = Process.GetProcessById((int)pid).ProcessName; } catch { return null; }
        var sb = new StringBuilder(512); GetWindowText(h, sb, sb.Capacity);
        string title = sb.ToString();
        if (Browsers.Contains(proc))
        {
            foreach (var kv in Sites)
                foreach (var word in kv.Value)
                    if (title.IndexOf(word, StringComparison.OrdinalIgnoreCase) >= 0) return kv.Key;
            return null;
        }
        foreach (var kv in Programs)
            if (string.Equals(proc, kv.Value, StringComparison.OrdinalIgnoreCase)) return kv.Key;
        return null;
    }

    // Every half second: if the window in front shows a locked app, cover that window with the lock card. The card
    // stays while that window still shows it, and goes when the tab changes, the window closes, or it is unlocked.
    void Check()
    {
        try
        {
            if (card.Visible)
            {
                string still = LockedAppIn(card.Target);
                if (still == null || Unlocked(still)) { card.Hide(); Log("card hidden"); return; }
                card.Cover(still);
                return;
            }
            IntPtr h = TestWindow() ?? GetForegroundWindow();
            if (h == Handle) return;
            string app = LockedAppIn(h);
            if (app == null || Unlocked(app)) return;
            card.Show(h, app);
            Log("card shown " + app);
        }
        catch { }
    }

    // Test runs can point the guard at one window by its title instead of the window in front
    // (Windows won't let a test pull a window to the front while someone is using the PC).
    static IntPtr? TestWindow()
    {
        var want = Environment.GetEnvironmentVariable("VOUCH_TEST_WINDOW");
        if (string.IsNullOrEmpty(want)) return null;
        foreach (var p in Process.GetProcesses())
            if (p.MainWindowHandle != IntPtr.Zero && p.MainWindowTitle.IndexOf(want, StringComparison.OrdinalIgnoreCase) >= 0) return p.MainWindowHandle;
        return IntPtr.Zero;
    }

    internal static void Log(string what)
    {
        var path = Environment.GetEnvironmentVariable("VOUCH_LOG");   // test runs only
        if (string.IsNullOrEmpty(path)) return;
        try { File.AppendAllText(path, DateTime.Now.ToString("HH:mm:ss.f") + " " + what + Environment.NewLine); } catch { }
    }

    internal void AskFor(string app) { ShowVouch(app); }
    internal void IgnoreFor(string app) { ShowVouch(app, true); }

    void ShowVouch(string app, bool ignore = false)
    {
        ShowInTaskbar = true;
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
        SetForegroundWindow(Handle);
        if (app == null) return;
        var url = Program.Url + (ignore ? "?ignore=" : "?app=") + Uri.EscapeDataString(app);
        if (ready && web.CoreWebView2 != null) web.CoreWebView2.Navigate(url);
        else pendingUrl = url;
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

// The lock card: a borderless, always-on-top panel laid exactly over the window that shows a locked app.
class LockCard : Form
{
    readonly MainForm owner;
    readonly Label title = new Label(), sub = new Label(), honest = new Label(), brand = new Label();
    readonly Button ask = new Button(), close = new Button(), later = new Button(), ignore = new Button();
    readonly Panel middle = new Panel();
    public IntPtr Target { get; private set; }
    string app = "";

    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }

    static readonly Color Bg = Color.FromArgb(246, 245, 241), Ink = Color.FromArgb(29, 29, 27), Muted = Color.FromArgb(139, 138, 132), Bad = Color.FromArgb(196, 85, 63);

    public LockCard(MainForm owner)
    {
        this.owner = owner;
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        BackColor = Bg;
        Text = "Vouch lock";
        KeyPreview = true;

        Style(brand, "Vouch", 12f, FontStyle.Bold, Ink);
        Style(title, "", 26f, FontStyle.Bold, Ink);
        Style(sub, "Tell Vouch why you want it. If your reason holds up, it unlocks for the time the AI picks.", 11.5f, FontStyle.Regular, Muted);
        Style(honest, "If you keep lying to yourself, uninstall the app.", 10f, FontStyle.Regular, Bad);
        Pill(ask, "Ask Vouch", Ink, Color.White);
        Pill(close, "Close the tab", Color.White, Ink);
        Pill(later, "Not now", Bg, Muted);
        later.FlatAppearance.BorderSize = 0;
        Pill(ignore, "Ignore (you'll have to say why)", Bg, Muted);
        ignore.FlatAppearance.BorderSize = 0;
        ignore.Font = new Font("Segoe UI", 9.5f);

        middle.BackColor = Bg;
        foreach (Control c in new Control[] { brand, title, sub, honest, ask, close, later, ignore }) middle.Controls.Add(c);
        Controls.Add(middle);
        Resize += (s, e) => Layout2();

        // Buttons ignore clicks for a moment after the card appears, so a click meant for the video can't press one.
        ask.Click += (s, e) => { if (!Armed()) return; Hide(); MainForm.Log("ask " + app); owner.AskFor(app); };
        close.Click += (s, e) => { if (Armed()) CloseTab(); };
        later.Click += (s, e) => { if (!Armed()) return; MainForm.Log("later clicked at " + Cursor.Position); Hide(); MainForm.ShowWindow(Target, 6); MainForm.Log("not now"); };   // minimize it
        ignore.Click += (s, e) => { if (!Armed()) return; Hide(); MainForm.Log("ignore " + app); owner.IgnoreFor(app); };
        foreach (var b in new[] { ask, close, later, ignore }) b.TabStop = false;
    }

    static void Style(Label l, string text, float size, FontStyle fs, Color c)
    {
        l.Text = text; l.Font = new Font("Segoe UI", size, fs); l.ForeColor = c; l.AutoSize = false; l.TextAlign = ContentAlignment.MiddleCenter;
    }

    static void Pill(Button b, string text, Color bg, Color fg)
    {
        b.Text = text; b.FlatStyle = FlatStyle.Flat; b.BackColor = bg; b.ForeColor = fg; b.Font = new Font("Segoe UI Semibold", 11f);
        b.FlatAppearance.BorderColor = Color.FromArgb(230, 228, 221); b.Cursor = Cursors.Hand; b.Size = new Size(260, 46);
    }

    void Layout2()
    {
        int w = Math.Min(520, ClientSize.Width - 40);
        middle.Size = new Size(w, 440);
        middle.Location = new Point((ClientSize.Width - w) / 2, Math.Max(10, (ClientSize.Height - 440) / 2));
        brand.SetBounds(0, 0, w, 28);
        title.SetBounds(0, 40, w, 56);
        sub.SetBounds(0, 100, w, 72);
        honest.SetBounds(0, 176, w, 26);
        int x = (w - 260) / 2;
        ask.Location = new Point(x, 222);
        close.Location = new Point(x, 276);
        later.Location = new Point(x, 330);
        ignore.Location = new Point(x, 380);
    }

    DateTime shownAt;
    bool Armed() { return (DateTime.Now - shownAt).TotalMilliseconds > 600; }

    // The card takes the keyboard (so typing doesn't reach the locked tab), but no key presses a button:
    // Escape means "Not now", everything else is swallowed.
    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        MainForm.Log("key " + keyData);
        if (keyData == Keys.Escape) { shownAt = DateTime.MinValue; later.PerformClick(); }
        return true;
    }

    public void Show(IntPtr target, string lockedApp)
    {
        Target = target;
        shownAt = DateTime.Now;
        Cover(lockedApp);
        Show();
        Activate();
        ActiveControl = null;
        MainForm.SetForegroundWindow(Handle);
    }

    public void Cover(string lockedApp)
    {
        if (lockedApp != app) { app = lockedApp; title.Text = app + " is locked"; }
        RECT r;
        if (!GetWindowRect(Target, out r)) return;
        var b = Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom);
        b.Intersect(Screen.FromHandle(Target).Bounds);   // maximized windows poke a few pixels off-screen
        if (Bounds != b) Bounds = b;
    }

    // Ctrl+W in the browser closes just this tab; the guard looks again on its next tick.
    void CloseTab()
    {
        Hide();
        keybd_event(0x12, 0, 0, UIntPtr.Zero);   // a tap of Alt lets this app hand the focus over
        keybd_event(0x12, 0, 2, UIntPtr.Zero);
        MainForm.SetForegroundWindow(Target);
        System.Threading.Thread.Sleep(150);
        if (GetForegroundWindow() != Target)
        {
            // Never send Ctrl+W unless the locked window really is in front: it would close a tab somewhere else.
            MainForm.Log("close aborted: could not focus the window");
            Show(Target, app);
            return;
        }
        keybd_event(0x11, 0, 0, UIntPtr.Zero);
        keybd_event(0x57, 0, 0, UIntPtr.Zero);
        keybd_event(0x57, 0, 2, UIntPtr.Zero);
        keybd_event(0x11, 0, 2, UIntPtr.Zero);
        MainForm.Log("closed tab");
    }

    protected override CreateParams CreateParams
    {
        get { var cp = base.CreateParams; cp.ExStyle |= 0x80; return cp; }   // WS_EX_TOOLWINDOW: stays out of Alt+Tab
    }
}
