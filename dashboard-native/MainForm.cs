using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace HelixGrid.Dashboard;

internal sealed class MainForm : Form
{
    private readonly AppConfig _config;
    private readonly HelixClient _client = new();
    private DockerManager _docker;

    private readonly Panel _contentHost = new();
    private readonly Label _pageTitle = new();
    private readonly Label _pageSubtitle = new();
    private readonly Label _topStatus = new();
    private readonly Label _activity = new();

    private readonly Label _dockerValue = new();
    private readonly Label _coordinatorValue = new();
    private readonly Label _workersValue = new();
    private readonly Label _workflowsValue = new();

    private readonly TextBox _workspaceBox = new();
    private readonly TextBox _resultsBox = new();
    private readonly NumericUpDown _workerCount = new();
    private readonly CheckBox _autoStart = new();

    private readonly DataGridView _workflowGrid = new();
    private readonly RichTextBox _resultsText = new();
    private readonly RichTextBox _logsText = new();

    private readonly Dictionary<string, Panel> _pages = new();
    private readonly Dictionary<string, NavButton> _nav = new();
    private readonly System.Windows.Forms.Timer _timer = new();

    private bool _busy;
    private bool _refreshing;
    private string _currentPage = "overview";

    public MainForm()
    {
        _config = AppConfig.Load();
        _docker = new DockerManager(_config);

        Text = "HelixGrid";
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(1080, 700);
        Size = new Size(1280, 820);
        BackColor = Theme.Background;
        ForeColor = Theme.Text;
        Font = Theme.Font();
        FormBorderStyle = FormBorderStyle.None;
        DoubleBuffered = true;

        BuildShell();
        BuildPages();
        ShowPage("overview");
        LoadConfigIntoUi();

        _timer.Interval = 3500;
        _timer.Tick += async (_, _) => await RefreshStatusAsync();
        _timer.Start();

        Shown += async (_, _) =>
        {
            if (!EnsureRepoRoot())
                return;

            await RefreshStatusAsync();
            if (_config.AutoStart)
                await StartClusterAsync(silent: true);
        };
    }

    private void BuildShell()
    {
        var top = new Panel
        {
            Dock = DockStyle.Top,
            Height = 66,
            BackColor = Theme.Background
        };
        top.MouseDown += TitleDrag;
        Controls.Add(top);

        var brand = new Label
        {
            Text = "HELIXGRID",
            ForeColor = Theme.Text,
            Font = Theme.Font(16f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 20)
        };
        brand.MouseDown += TitleDrag;
        top.Controls.Add(brand);

        var version = new Label
        {
            Text = "CONTROL CENTER",
            ForeColor = Theme.Muted,
            Font = Theme.Font(8.5f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(140, 25)
        };
        version.MouseDown += TitleDrag;
        top.Controls.Add(version);

        _topStatus.Text = "● Kontrollerer…";
        _topStatus.ForeColor = Theme.Muted;
        _topStatus.Font = Theme.Font(9f, FontStyle.Bold);
        _topStatus.AutoSize = true;
        _topStatus.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        _topStatus.Location = new Point(1000, 24);
        top.Controls.Add(_topStatus);

        var close = WindowButton("×", () => Close());
        close.Location = new Point(1230, 12);
        close.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        top.Controls.Add(close);

        var minimize = WindowButton("—", () => WindowState = FormWindowState.Minimized);
        minimize.Location = new Point(1182, 12);
        minimize.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        top.Controls.Add(minimize);

        var sidebar = new Panel
        {
            Dock = DockStyle.Left,
            Width = 222,
            BackColor = Theme.Sidebar,
            Padding = new Padding(0, 16, 0, 16)
        };
        Controls.Add(sidebar);
        sidebar.BringToFront();

        var sideBrand = new RoundedPanel
        {
            Height = 86,
            Dock = DockStyle.Top,
            Margin = new Padding(14),
            Padding = new Padding(18, 15, 18, 12),
            BackColor = Color.FromArgb(15, 24, 41),
            BorderColor = Color.FromArgb(27, 42, 66)
        };
        sidebar.Controls.Add(sideBrand);

        var mark = new Label
        {
            Text = "H",
            BackColor = Theme.Accent,
            ForeColor = Color.FromArgb(5, 15, 28),
            Font = Theme.Font(18f, FontStyle.Bold),
            TextAlign = ContentAlignment.MiddleCenter,
            Size = new Size(44, 44),
            Location = new Point(16, 19)
        };
        mark.Region = new Region(RoundedPanel.RoundedRect(mark.ClientRectangle, 11));
        sideBrand.Controls.Add(mark);

        sideBrand.Controls.Add(new Label
        {
            Text = "HelixGrid",
            ForeColor = Theme.Text,
            Font = Theme.Font(12f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(72, 20)
        });
        sideBrand.Controls.Add(new Label
        {
            Text = "Distributed Runtime",
            ForeColor = Theme.Muted,
            Font = Theme.Font(8.5f),
            AutoSize = true,
            Location = new Point(72, 43)
        });

        var navPanel = new Panel
        {
            Dock = DockStyle.Fill,
            BackColor = Theme.Sidebar,
            Padding = new Padding(0, 18, 0, 0)
        };
        sidebar.Controls.Add(navPanel);
        navPanel.BringToFront();

        AddNav(navPanel, "overview", "  Overview");
        AddNav(navPanel, "files", "  Filer & Backup");
        AddNav(navPanel, "workflows", "  Workflows");
        AddNav(navPanel, "results", "  Resultater");
        AddNav(navPanel, "logs", "  Logs");
        AddNav(navPanel, "settings", "  Indstillinger");

        var sidebarBottom = new Panel
        {
            Dock = DockStyle.Bottom,
            Height = 112,
            BackColor = Theme.Sidebar,
            Padding = new Padding(14, 10, 14, 14)
        };
        sidebar.Controls.Add(sidebarBottom);

        var update = new AccentButton
        {
            Text = "Opdater HelixGrid",
            Dock = DockStyle.Top,
            Height = 40
        };
        update.Click += (_, _) => LaunchUpdater();
        sidebarBottom.Controls.Add(update);

        _activity.Text = "Klar";
        _activity.ForeColor = Theme.Muted;
        _activity.Font = Theme.Font(8.5f);
        _activity.Dock = DockStyle.Bottom;
        _activity.Height = 28;
        _activity.TextAlign = ContentAlignment.BottomLeft;
        sidebarBottom.Controls.Add(_activity);

        var header = new Panel
        {
            Dock = DockStyle.Top,
            Height = 100,
            BackColor = Theme.Background,
            Padding = new Padding(28, 14, 28, 0)
        };
        Controls.Add(header);
        header.BringToFront();

        _pageTitle.Text = "Overview";
        _pageTitle.ForeColor = Theme.Text;
        _pageTitle.Font = Theme.Font(22f, FontStyle.Bold);
        _pageTitle.AutoSize = true;
        _pageTitle.Location = new Point(28, 14);
        header.Controls.Add(_pageTitle);

        _pageSubtitle.Text = "Status og hurtige handlinger";
        _pageSubtitle.ForeColor = Theme.Muted;
        _pageSubtitle.Font = Theme.Font(9.5f);
        _pageSubtitle.AutoSize = true;
        _pageSubtitle.Location = new Point(30, 51);
        header.Controls.Add(_pageSubtitle);

        _contentHost.Dock = DockStyle.Fill;
        _contentHost.BackColor = Theme.Background;
        _contentHost.Padding = new Padding(28, 0, 28, 28);
        Controls.Add(_contentHost);
        _contentHost.BringToFront();
    }

    private Button WindowButton(string text, Action action)
    {
        var button = new Button
        {
            Text = text,
            Width = 42,
            Height = 34,
            FlatStyle = FlatStyle.Flat,
            FlatAppearance = { BorderSize = 0, MouseOverBackColor = Color.FromArgb(36, 46, 65) },
            BackColor = Theme.Background,
            ForeColor = Theme.Muted,
            Font = Theme.Font(12f, FontStyle.Bold),
            Cursor = Cursors.Hand
        };
        button.Click += (_, _) => action();
        return button;
    }

    private void AddNav(Panel parent, string key, string text)
    {
        var button = new NavButton { Text = text };
        button.Click += (_, _) => ShowPage(key);
        parent.Controls.Add(button);
        button.BringToFront();
        _nav[key] = button;
    }

    private void BuildPages()
    {
        _pages["overview"] = BuildOverviewPage();
        _pages["files"] = BuildFilesPage();
        _pages["workflows"] = BuildWorkflowsPage();
        _pages["results"] = BuildResultsPage();
        _pages["logs"] = BuildLogsPage();
        _pages["settings"] = BuildSettingsPage();

        foreach (var page in _pages.Values)
        {
            page.Dock = DockStyle.Fill;
            page.Visible = false;
            _contentHost.Controls.Add(page);
        }
    }

    private Panel BuildOverviewPage()
    {
        var page = Page();

        var cards = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 132,
            ColumnCount = 4,
            RowCount = 1,
            BackColor = Theme.Background,
            Padding = new Padding(0, 0, 0, 14)
        };
        for (var i = 0; i < 4; i++)
            cards.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 25));

        cards.Controls.Add(StatusCard("DOCKER", _dockerValue, "Container engine"), 0, 0);
        cards.Controls.Add(StatusCard("COORDINATOR", _coordinatorValue, "API & scheduler"), 1, 0);
        cards.Controls.Add(StatusCard("WORKERS", _workersValue, "Connected executors"), 2, 0);
        cards.Controls.Add(StatusCard("WORKFLOWS", _workflowsValue, "Known jobs"), 3, 0);
        page.Controls.Add(cards);

        var hero = new RoundedPanel
        {
            Dock = DockStyle.Top,
            Height = 235,
            BackColor = Theme.Surface,
            Padding = new Padding(24)
        };
        page.Controls.Add(hero);
        hero.BringToFront();

        hero.Controls.Add(new Label
        {
            Text = "HelixGrid er din lokale job-motor.",
            ForeColor = Theme.Text,
            Font = Theme.Font(17f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 25)
        });

        hero.Controls.Add(new Label
        {
            Text = "Start clusteret, vælg en mappe og brug workers til audit, backup og andre workflows.\nAlt styres herfra uden terminalkommandoer.",
            ForeColor = Theme.Muted,
            Font = Theme.Font(10f),
            AutoSize = false,
            Size = new Size(700, 52),
            Location = new Point(25, 63)
        });

        var start = new AccentButton { Text = "▶  Start HelixGrid", Width = 175, Location = new Point(24, 135) };
        start.Click += async (_, _) => await StartClusterAsync();
        hero.Controls.Add(start);

        var restart = new ModernButton { Text = "↻  Genstart", Width = 135, Location = new Point(211, 135) };
        restart.Click += async (_, _) => await RestartClusterAsync();
        hero.Controls.Add(restart);

        var stop = new DangerButton { Text = "■  Stop", Width = 110, Location = new Point(358, 135) };
        stop.Click += async (_, _) => await StopClusterAsync();
        hero.Controls.Add(stop);

        var tip = new RoundedPanel
        {
            Width = 300,
            Height = 164,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
            Location = new Point(700, 25),
            BackColor = Theme.Surface2,
            BorderColor = Color.FromArgb(40, 60, 90)
        };
        hero.Controls.Add(tip);
        tip.Controls.Add(new Label
        {
            Text = "QUICK TIP",
            ForeColor = Theme.Accent,
            Font = Theme.Font(8.5f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(18, 17)
        });
        tip.Controls.Add(new Label
        {
            Text = "Start med 3 workers. Flere workers er ikke altid hurtigere på én computer.",
            ForeColor = Theme.Text,
            Font = Theme.Font(10f, FontStyle.Bold),
            AutoSize = false,
            Size = new Size(250, 54),
            Location = new Point(18, 45)
        });
        tip.Controls.Add(new Label
        {
            Text = "Du kan ændre antallet under Filer & Backup.",
            ForeColor = Theme.Muted,
            Font = Theme.Font(8.5f),
            AutoSize = false,
            Size = new Size(250, 38),
            Location = new Point(18, 106)
        });

        return page;
    }

    private Control StatusCard(string title, Label value, string sub)
    {
        var panel = new RoundedPanel
        {
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 0, 12, 0),
            Padding = new Padding(18),
            BackColor = Theme.Surface
        };

        panel.Controls.Add(new Label
        {
            Text = title,
            ForeColor = Theme.Muted,
            Font = Theme.Font(8f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(18, 15)
        });

        value.Text = "—";
        value.ForeColor = Theme.Text;
        value.Font = Theme.Font(16f, FontStyle.Bold);
        value.AutoSize = true;
        value.Location = new Point(18, 43);
        panel.Controls.Add(value);

        panel.Controls.Add(new Label
        {
            Text = sub,
            ForeColor = Theme.Muted,
            Font = Theme.Font(8.5f),
            AutoSize = true,
            Location = new Point(18, 77)
        });

        return panel;
    }

    private Panel BuildFilesPage()
    {
        var page = Page();

        var settings = new RoundedPanel
        {
            Dock = DockStyle.Top,
            Height = 205,
            BackColor = Theme.Surface
        };
        page.Controls.Add(settings);

        settings.Controls.Add(LabelAt("Mappe HelixGrid må læse", 22, 18, 10f, FontStyle.Bold));
        StyleInput(_workspaceBox);
        _workspaceBox.Location = new Point(22, 48);
        _workspaceBox.Width = 700;
        settings.Controls.Add(_workspaceBox);

        var browseWorkspace = new ModernButton { Text = "Vælg mappe", Width = 120, Location = new Point(735, 45) };
        browseWorkspace.Click += (_, _) => ChooseFolder(_workspaceBox);
        settings.Controls.Add(browseWorkspace);

        settings.Controls.Add(LabelAt("Resultater gemmes her", 22, 96, 10f, FontStyle.Bold));
        StyleInput(_resultsBox);
        _resultsBox.Location = new Point(22, 126);
        _resultsBox.Width = 700;
        settings.Controls.Add(_resultsBox);

        var browseResults = new ModernButton { Text = "Vælg mappe", Width = 120, Location = new Point(735, 123) };
        browseResults.Click += (_, _) => ChooseFolder(_resultsBox);
        settings.Controls.Add(browseResults);

        settings.Controls.Add(LabelAt("Workers", 885, 18, 9f, FontStyle.Bold));
        _workerCount.Minimum = 1;
        _workerCount.Maximum = 16;
        _workerCount.Width = 80;
        _workerCount.Height = 34;
        _workerCount.Location = new Point(885, 47);
        _workerCount.BackColor = Theme.Surface2;
        _workerCount.ForeColor = Theme.Text;
        _workerCount.BorderStyle = BorderStyle.FixedSingle;
        settings.Controls.Add(_workerCount);

        var save = new AccentButton { Text = "Gem", Width = 100, Location = new Point(885, 123) };
        save.Click += (_, _) => SaveUiConfig(showMessage: true);
        settings.Controls.Add(save);

        var actions = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 1,
            BackColor = Theme.Background,
            Padding = new Padding(0, 18, 0, 0)
        };
        actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        page.Controls.Add(actions);
        actions.BringToFront();

        actions.Controls.Add(ActionCard(
            "FIL-AUDIT",
            "Find dubletter, store filer og checksums",
            "HelixGrid scanner den valgte mappe med workers og laver en læsbar rapport. Originale filer ændres ikke.",
            "Start audit",
            async () => await RunFileWorkflowAsync("audit")), 0, 0);

        actions.Controls.Add(ActionCard(
            "BACKUP",
            "Lav en komprimeret backup",
            "HelixGrid laver et backup-arkiv og SHA-256 metadata i resultatmappen. Kildemappen er read-only.",
            "Start backup",
            async () => await RunFileWorkflowAsync("backup")), 1, 0);

        return page;
    }

    private Control ActionCard(string eyebrow, string title, string body, string buttonText, Func<Task> action)
    {
        var panel = new RoundedPanel
        {
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 0, 12, 0),
            BackColor = Theme.Surface,
            Padding = new Padding(24)
        };

        panel.Controls.Add(new Label
        {
            Text = eyebrow,
            ForeColor = Theme.Accent,
            Font = Theme.Font(8.5f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 24)
        });

        panel.Controls.Add(new Label
        {
            Text = title,
            ForeColor = Theme.Text,
            Font = Theme.Font(15f, FontStyle.Bold),
            AutoSize = true,
            Location = new Point(24, 53)
        });

        panel.Controls.Add(new Label
        {
            Text = body,
            ForeColor = Theme.Muted,
            Font = Theme.Font(9.5f),
            AutoSize = false,
            Size = new Size(430, 76),
            Location = new Point(24, 89)
        });

        var button = new AccentButton { Text = buttonText, Width = 150, Location = new Point(24, 180) };
        button.Click += async (_, _) => await action();
        panel.Controls.Add(button);

        return panel;
    }

    private Panel BuildWorkflowsPage()
    {
        var page = Page();

        var toolbar = new Panel
        {
            Dock = DockStyle.Top,
            Height = 56,
            BackColor = Theme.Background
        };
        page.Controls.Add(toolbar);

        var refresh = new ModernButton { Text = "↻ Opdater", Width = 120, Location = new Point(0, 6) };
        refresh.Click += async (_, _) => await RefreshWorkflowsAsync();
        toolbar.Controls.Add(refresh);

        var cancel = new DangerButton { Text = "Annuller valgt", Width = 145, Location = new Point(132, 6) };
        cancel.Click += async (_, _) => await CancelSelectedWorkflowAsync();
        toolbar.Controls.Add(cancel);

        _workflowGrid.Dock = DockStyle.Fill;
        _workflowGrid.BackgroundColor = Theme.Surface;
        _workflowGrid.BorderStyle = BorderStyle.None;
        _workflowGrid.GridColor = Theme.Border;
        _workflowGrid.EnableHeadersVisualStyles = false;
        _workflowGrid.ColumnHeadersDefaultCellStyle.BackColor = Theme.Surface2;
        _workflowGrid.ColumnHeadersDefaultCellStyle.ForeColor = Theme.Muted;
        _workflowGrid.ColumnHeadersDefaultCellStyle.Font = Theme.Font(8.5f, FontStyle.Bold);
        _workflowGrid.ColumnHeadersHeight = 42;
        _workflowGrid.DefaultCellStyle.BackColor = Theme.Surface;
        _workflowGrid.DefaultCellStyle.ForeColor = Theme.Text;
        _workflowGrid.DefaultCellStyle.SelectionBackColor = Color.FromArgb(35, 69, 111);
        _workflowGrid.DefaultCellStyle.SelectionForeColor = Theme.Text;
        _workflowGrid.DefaultCellStyle.Font = Theme.Font(9f);
        _workflowGrid.DefaultCellStyle.Padding = new Padding(6);
        _workflowGrid.RowTemplate.Height = 38;
        _workflowGrid.ReadOnly = true;
        _workflowGrid.AllowUserToAddRows = false;
        _workflowGrid.AllowUserToDeleteRows = false;
        _workflowGrid.AllowUserToResizeRows = false;
        _workflowGrid.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
        _workflowGrid.MultiSelect = false;
        _workflowGrid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;

        _workflowGrid.Columns.Add("id", "ID");
        _workflowGrid.Columns.Add("name", "Navn");
        _workflowGrid.Columns.Add("state", "Status");
        _workflowGrid.Columns.Add("created", "Oprettet");
        _workflowGrid.Columns["id"]!.FillWeight = 85;
        _workflowGrid.Columns["name"]!.FillWeight = 125;
        _workflowGrid.Columns["state"]!.FillWeight = 60;
        _workflowGrid.Columns["created"]!.FillWeight = 90;

        var card = new RoundedPanel { Dock = DockStyle.Fill, Padding = new Padding(1), BackColor = Theme.Surface };
        card.Controls.Add(_workflowGrid);
        page.Controls.Add(card);
        card.BringToFront();

        return page;
    }

    private Panel BuildResultsPage()
    {
        var page = Page();

        var toolbar = new Panel { Dock = DockStyle.Top, Height = 56, BackColor = Theme.Background };
        page.Controls.Add(toolbar);

        var load = new ModernButton { Text = "↻ Genindlæs", Width = 125, Location = new Point(0, 6) };
        load.Click += (_, _) => LoadResults();
        toolbar.Controls.Add(load);

        var open = new AccentButton { Text = "Åbn resultatmappe", Width = 170, Location = new Point(138, 6) };
        open.Click += (_, _) => OpenResultsFolder();
        toolbar.Controls.Add(open);

        StyleRichText(_resultsText);
        var card = new RoundedPanel { Dock = DockStyle.Fill, Padding = new Padding(14), BackColor = Theme.Surface };
        card.Controls.Add(_resultsText);
        page.Controls.Add(card);
        card.BringToFront();

        return page;
    }

    private Panel BuildLogsPage()
    {
        var page = Page();

        var toolbar = new Panel { Dock = DockStyle.Top, Height = 56, BackColor = Theme.Background };
        page.Controls.Add(toolbar);

        var load = new ModernButton { Text = "Hent logs", Width = 120, Location = new Point(0, 6) };
        load.Click += async (_, _) => await RefreshLogsAsync();
        toolbar.Controls.Add(load);

        var clear = new ModernButton { Text = "Ryd", Width = 90, Location = new Point(132, 6) };
        clear.Click += (_, _) => _logsText.Clear();
        toolbar.Controls.Add(clear);

        StyleRichText(_logsText);
        _logsText.Font = new Font("Cascadia Mono", 9f);
        var card = new RoundedPanel { Dock = DockStyle.Fill, Padding = new Padding(14), BackColor = Color.FromArgb(7, 11, 19) };
        card.Controls.Add(_logsText);
        page.Controls.Add(card);
        card.BringToFront();

        return page;
    }

    private Panel BuildSettingsPage()
    {
        var page = Page();

        var panel = new RoundedPanel { Dock = DockStyle.Top, Height = 325, BackColor = Theme.Surface };
        page.Controls.Add(panel);

        panel.Controls.Add(LabelAt("HelixGrid installation", 22, 22, 12f, FontStyle.Bold));
        panel.Controls.Add(new Label
        {
            Text = "Dashboardet skal kende mappen der indeholder docker-compose.yml.",
            ForeColor = Theme.Muted,
            Font = Theme.Font(9f),
            AutoSize = true,
            Location = new Point(22, 52)
        });

        var repoBox = new TextBox
        {
            Name = "repoBox",
            Text = _config.RepoRoot,
            Width = 720,
            Location = new Point(22, 87)
        };
        StyleInput(repoBox);
        panel.Controls.Add(repoBox);

        var choose = new ModernButton { Text = "Vælg mappe", Width = 120, Location = new Point(755, 84) };
        choose.Click += (_, _) =>
        {
            using var dialog = new FolderBrowserDialog
            {
                Description = "Vælg HelixGrid-mappen",
                InitialDirectory = Directory.Exists(repoBox.Text) ? repoBox.Text : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
            };
            if (dialog.ShowDialog(this) == DialogResult.OK)
                repoBox.Text = dialog.SelectedPath;
        };
        panel.Controls.Add(choose);

        _autoStart.Text = "Start HelixGrid automatisk når dashboardet åbner";
        _autoStart.ForeColor = Theme.Text;
        _autoStart.BackColor = Theme.Surface;
        _autoStart.Font = Theme.Font(9.5f);
        _autoStart.AutoSize = true;
        _autoStart.Location = new Point(22, 145);
        panel.Controls.Add(_autoStart);

        var save = new AccentButton { Text = "Gem indstillinger", Width = 150, Location = new Point(22, 195) };
        save.Click += (_, _) =>
        {
            _config.RepoRoot = repoBox.Text.Trim();
            _docker = new DockerManager(_config);
            SaveUiConfig(showMessage: true);
        };
        panel.Controls.Add(save);

        var update = new ModernButton { Text = "Opdater HelixGrid", Width = 150, Location = new Point(184, 195) };
        update.Click += (_, _) => LaunchUpdater();
        panel.Controls.Add(update);

        panel.Controls.Add(new Label
        {
            Text = "Native dashboard · .NET 8 · Windows x64",
            ForeColor = Theme.Muted,
            Font = Theme.Font(8.5f),
            AutoSize = true,
            Location = new Point(22, 267)
        });

        return page;
    }

    private static Panel Page() => new()
    {
        BackColor = Theme.Background,
        Padding = new Padding(0)
    };

    private static Label LabelAt(string text, int x, int y, float size, FontStyle style) => new()
    {
        Text = text,
        ForeColor = Theme.Text,
        Font = Theme.Font(size, style),
        AutoSize = true,
        Location = new Point(x, y)
    };

    private static void StyleInput(TextBox box)
    {
        box.BackColor = Theme.Surface2;
        box.ForeColor = Theme.Text;
        box.BorderStyle = BorderStyle.FixedSingle;
        box.Font = Theme.Font(9.5f);
        box.Height = 34;
    }

    private static void StyleRichText(RichTextBox box)
    {
        box.Dock = DockStyle.Fill;
        box.BackColor = Theme.Surface;
        box.ForeColor = Color.FromArgb(213, 222, 238);
        box.BorderStyle = BorderStyle.None;
        box.ReadOnly = true;
        box.Font = new Font("Cascadia Mono", 9.5f);
        box.DetectUrls = true;
    }

    private void LoadConfigIntoUi()
    {
        _workspaceBox.Text = _config.Workspace;
        _resultsBox.Text = _config.Results;
        _workerCount.Value = Math.Clamp(_config.Workers, 1, 16);
        _autoStart.Checked = _config.AutoStart;
    }

    private bool EnsureRepoRoot()
    {
        if (!string.IsNullOrWhiteSpace(_config.RepoRoot) &&
            File.Exists(Path.Combine(_config.RepoRoot, "docker-compose.yml")))
            return true;

        using var dialog = new FolderBrowserDialog
        {
            Description = "Vælg HelixGrid-mappen (den der indeholder docker-compose.yml)",
            ShowNewFolderButton = false
        };

        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            MessageBox.Show(
                this,
                "Dashboardet kan ikke styre HelixGrid før du vælger installationsmappen.",
                "HelixGrid",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return false;
        }

        if (!File.Exists(Path.Combine(dialog.SelectedPath, "docker-compose.yml")))
        {
            MessageBox.Show(this, "Den valgte mappe indeholder ikke docker-compose.yml.", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return false;
        }

        _config.RepoRoot = dialog.SelectedPath;
        _config.Normalize();
        _config.Save();
        _docker = new DockerManager(_config);
        LoadConfigIntoUi();
        return true;
    }

    private void ShowPage(string key)
    {
        if (!_pages.TryGetValue(key, out var page))
            return;

        _currentPage = key;
        foreach (var pair in _pages)
            pair.Value.Visible = pair.Key == key;

        foreach (var pair in _nav)
            pair.Value.Selected = pair.Key == key;

        var titles = new Dictionary<string, (string title, string subtitle)>
        {
            ["overview"] = ("Overview", "Status og hurtige handlinger"),
            ["files"] = ("Filer & Backup", "Brug workers på rigtige filer uden terminal"),
            ["workflows"] = ("Workflows", "Se og administrer jobs"),
            ["results"] = ("Resultater", "Rapporter og output fra HelixGrid"),
            ["logs"] = ("Logs", "Docker- og worker-output"),
            ["settings"] = ("Indstillinger", "Installation, opstart og dashboard")
        };

        (_pageTitle.Text, _pageSubtitle.Text) = titles[key];

        if (key == "workflows")
            _ = RefreshWorkflowsAsync();
        if (key == "results")
            LoadResults();
    }

    private void ChooseFolder(TextBox target)
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "Vælg mappe",
            InitialDirectory = Directory.Exists(target.Text)
                ? target.Text
                : Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
        };

        if (dialog.ShowDialog(this) == DialogResult.OK)
            target.Text = dialog.SelectedPath;
    }

    private bool SaveUiConfig(bool showMessage)
    {
        _config.Workspace = _workspaceBox.Text.Trim();
        _config.Results = _resultsBox.Text.Trim();
        _config.Workers = (int)_workerCount.Value;
        _config.AutoStart = _autoStart.Checked;
        _config.Normalize();

        if (!_config.IsValid(out var error))
        {
            if (showMessage)
                MessageBox.Show(this, error, "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return false;
        }

        try
        {
            _config.Save();
            _docker = new DockerManager(_config);
            if (showMessage)
                MessageBox.Show(this, "Indstillingerne er gemt.", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return true;
        }
        catch (Exception ex)
        {
            if (showMessage)
                MessageBox.Show(this, ex.Message, "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return false;
        }
    }

    private async Task RunBusyAsync(string text, Func<Task> action)
    {
        if (_busy)
        {
            _activity.Text = "Der kører allerede en handling";
            return;
        }

        _busy = true;
        _activity.Text = text;
        UseWaitCursor = true;

        try
        {
            await action();
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            _busy = false;
            UseWaitCursor = false;
            _activity.Text = "Klar";
            await RefreshStatusAsync();
        }
    }

    private async Task<bool> EnsureDockerAsync()
    {
        if (await _docker.IsDockerReadyAsync())
            return true;

        _activity.Text = "Starter Docker Desktop…";
        if (!await _docker.EnsureDockerDesktopAsync())
        {
            MessageBox.Show(
                this,
                "Docker Desktop kunne ikke startes. Kør windows-install.bat eller start Docker Desktop manuelt.",
                "HelixGrid",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return false;
        }

        return true;
    }

    private async Task StartClusterAsync(bool silent = false)
    {
        if (!EnsureRepoRoot() || !SaveUiConfig(false))
            return;

        await RunBusyAsync("Starter HelixGrid…", async () =>
        {
            if (!await EnsureDockerAsync())
                return;

            var result = await _docker.StartClusterAsync();
            if (!result.Success)
                throw new InvalidOperationException("HelixGrid kunne ikke starte.\n\n" + result.Combined);

            for (var i = 0; i < 50; i++)
            {
                if (await _client.IsOnlineAsync())
                {
                    if (!silent)
                        MessageBox.Show(this, "HelixGrid kører.", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                await Task.Delay(700);
            }

            throw new InvalidOperationException("Coordinator blev ikke klar. Tjek Logs.");
        });
    }

    private async Task StopClusterAsync()
    {
        if (!EnsureRepoRoot())
            return;

        await RunBusyAsync("Stopper HelixGrid…", async () =>
        {
            var result = await _docker.StopClusterAsync();
            if (!result.Success && !result.Combined.Contains("not found", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(result.Combined);
        });
    }

    private async Task RestartClusterAsync()
    {
        if (!EnsureRepoRoot() || !SaveUiConfig(false))
            return;

        await RunBusyAsync("Genstarter HelixGrid…", async () =>
        {
            if (!await EnsureDockerAsync())
                return;

            var result = await _docker.RestartClusterAsync();
            if (!result.Success)
                throw new InvalidOperationException(result.Combined);
        });
    }

    private async Task RunFileWorkflowAsync(string mode)
    {
        if (!EnsureRepoRoot() || !SaveUiConfig(true))
            return;

        var file = mode == "audit" ? "windows-file-audit.json" : "windows-file-backup.json";
        var workflowPath = Path.Combine(_config.RepoRoot, "examples", file);
        if (!File.Exists(workflowPath))
        {
            MessageBox.Show(this, $"Workflow-filen mangler: {workflowPath}", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        await RunBusyAsync(mode == "audit" ? "Kører fil-audit…" : "Laver backup…", async () =>
        {
            if (!await EnsureDockerAsync())
                return;

            var start = await _docker.StartClusterAsync();
            if (!start.Success)
                throw new InvalidOperationException(start.Combined);

            for (var i = 0; i < 40 && !await _client.IsOnlineAsync(); i++)
                await Task.Delay(700);

            if (!await _client.IsOnlineAsync())
                throw new InvalidOperationException("Coordinator er ikke klar.");

            var id = await _client.SubmitWorkflowAsync(await File.ReadAllTextAsync(workflowPath));
            _activity.Text = $"Workflow {id}";

            while (true)
            {
                var state = await _client.GetWorkflowStateAsync(id);
                _activity.Text = $"{file.Replace(".json", "")}: {state}";

                if (state is "SUCCEEDED" or "FAILED" or "CANCELLED")
                {
                    if (state != "SUCCEEDED")
                        throw new InvalidOperationException($"Workflow endte som {state}.");
                    break;
                }

                await Task.Delay(850);
            }

            LoadResults();
            MessageBox.Show(
                this,
                mode == "audit" ? "Fil-audit er færdig." : "Backup er færdig.",
                "HelixGrid",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        });
    }

    private async Task RefreshStatusAsync()
    {
        if (_refreshing || IsDisposed)
            return;

        _refreshing = true;
        try
        {
            var dockerReady = false;
            try { dockerReady = await _docker.IsDockerReadyAsync(); } catch { }

            var status = dockerReady
                ? await _client.GetStatusAsync()
                : new HelixStatus(false, 0, 0);

            if (IsDisposed)
                return;

            _dockerValue.Text = dockerReady ? "Kører" : "Stoppet";
            _dockerValue.ForeColor = dockerReady ? Theme.Green : Theme.Muted;

            _coordinatorValue.Text = status.Online ? "Online" : "Offline";
            _coordinatorValue.ForeColor = status.Online ? Theme.Green : Theme.Muted;

            _workersValue.Text = status.Workers.ToString();
            _workflowsValue.Text = status.Workflows.ToString();

            _topStatus.Text = status.Online ? "● System online" : "● Offline";
            _topStatus.ForeColor = status.Online ? Theme.Green : Theme.Muted;

            if (_currentPage == "workflows" && status.Online)
                await RefreshWorkflowsAsync();
        }
        catch
        {
            _topStatus.Text = "● Offline";
            _topStatus.ForeColor = Theme.Muted;
        }
        finally
        {
            _refreshing = false;
        }
    }

    private async Task RefreshWorkflowsAsync()
    {
        if (!await _client.IsOnlineAsync())
        {
            _workflowGrid.Rows.Clear();
            return;
        }

        try
        {
            var rows = await _client.GetWorkflowsAsync();
            _workflowGrid.SuspendLayout();
            _workflowGrid.Rows.Clear();
            foreach (var row in rows.OrderByDescending(r => r.CreatedAt))
                _workflowGrid.Rows.Add(row.Id, row.Name, row.State, row.CreatedAt);
            _workflowGrid.ResumeLayout();
        }
        catch { }
    }

    private async Task CancelSelectedWorkflowAsync()
    {
        if (_workflowGrid.SelectedRows.Count == 0)
        {
            MessageBox.Show(this, "Vælg et workflow først.", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        var id = _workflowGrid.SelectedRows[0].Cells["id"].Value?.ToString();
        if (string.IsNullOrWhiteSpace(id))
            return;

        if (MessageBox.Show(this, $"Annuller workflow {id}?", "HelixGrid", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return;

        await RunBusyAsync("Annullerer workflow…", async () =>
        {
            await _client.CancelWorkflowAsync(id);
            await RefreshWorkflowsAsync();
        });
    }

    private void LoadResults()
    {
        try
        {
            var resultDir = _resultsBox.Text.Trim();
            if (string.IsNullOrWhiteSpace(resultDir))
                resultDir = _config.Results;

            var summary = Path.Combine(resultDir, "summary.txt");
            var backup = Path.Combine(resultDir, "backup.json");

            if (File.Exists(summary))
            {
                _resultsText.Text = File.ReadAllText(summary);
                return;
            }

            if (File.Exists(backup))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(backup));
                _resultsText.Text = JsonSerializer.Serialize(doc.RootElement, new JsonSerializerOptions { WriteIndented = true });
                return;
            }

            _resultsText.Text =
                "Der er endnu ingen rapport.\n\n" +
                "Gå til Filer & Backup, vælg en mappe og start en audit eller backup.";
        }
        catch (Exception ex)
        {
            _resultsText.Text = ex.Message;
        }
    }

    private void OpenResultsFolder()
    {
        var path = _resultsBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(path))
            path = _config.Results;

        try
        {
            Directory.CreateDirectory(path);
            Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private async Task RefreshLogsAsync()
    {
        if (!EnsureRepoRoot())
            return;

        await RunBusyAsync("Henter logs…", async () =>
        {
            var result = await _docker.LogsAsync();
            _logsText.Text = result.Combined;
            _logsText.SelectionStart = _logsText.TextLength;
            _logsText.ScrollToCaret();
        });
    }

    private void LaunchUpdater()
    {
        if (!EnsureRepoRoot())
            return;

        var updater = Path.Combine(_config.RepoRoot, "update.bat");
        if (!File.Exists(updater))
        {
            MessageBox.Show(this, "update.bat blev ikke fundet.", "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        if (MessageBox.Show(
            this,
            "HelixGrid henter den nyeste version fra GitHub, opdaterer Docker og starter igen. Fortsæt?",
            "Opdater HelixGrid",
            MessageBoxButtons.YesNo,
            MessageBoxIcon.Question) != DialogResult.Yes)
            return;

        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = updater,
                WorkingDirectory = _config.RepoRoot,
                UseShellExecute = true
            });
            Close();
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "HelixGrid", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void TitleDrag(object? sender, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left)
            return;
        ReleaseCapture();
        SendMessage(Handle, 0xA1, 0x2, 0);
    }

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, int wParam, int lParam);
}
