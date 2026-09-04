using System.Text.Json;

namespace HelixGrid.Dashboard;

internal sealed class AppConfig
{
    public string RepoRoot { get; set; } = "";
    public string Workspace { get; set; } = "";
    public string Results { get; set; } = "";
    public int Workers { get; set; } = 3;
    public bool AutoStart { get; set; } = true;

    public static string StateDirectory =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HelixGrid");

    public static string ConfigPath => Path.Combine(StateDirectory, "dashboard-native.json");

    public static AppConfig Load()
    {
        try
        {
            if (File.Exists(ConfigPath))
            {
                var parsed = JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(ConfigPath));
                if (parsed is not null)
                {
                    parsed.Workers = Math.Clamp(parsed.Workers, 1, 16);
                    parsed.Normalize();
                    return parsed;
                }
            }
        }
        catch { }

        var config = new AppConfig();
        config.RepoRoot = FindRepoRoot() ?? "";
        config.Normalize();
        return config;
    }

    public void Normalize()
    {
        Workers = Math.Clamp(Workers, 1, 16);

        if (string.IsNullOrWhiteSpace(RepoRoot))
            RepoRoot = FindRepoRoot() ?? "";

        if (!string.IsNullOrWhiteSpace(RepoRoot))
        {
            if (string.IsNullOrWhiteSpace(Workspace))
                Workspace = Path.Combine(RepoRoot, "workspace");
            if (string.IsNullOrWhiteSpace(Results))
                Results = Path.Combine(RepoRoot, "helix-results");
        }
    }

    public void Save()
    {
        Normalize();
        Directory.CreateDirectory(StateDirectory);
        if (!string.IsNullOrWhiteSpace(Workspace))
            Directory.CreateDirectory(Workspace);
        if (!string.IsNullOrWhiteSpace(Results))
            Directory.CreateDirectory(Results);

        var options = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(ConfigPath, JsonSerializer.Serialize(this, options));
    }

    public bool IsValid(out string error)
    {
        error = "";
        if (string.IsNullOrWhiteSpace(RepoRoot) || !File.Exists(Path.Combine(RepoRoot, "docker-compose.yml")))
        {
            error = "HelixGrid-mappen blev ikke fundet. Vælg mappen der indeholder docker-compose.yml.";
            return false;
        }

        if (string.IsNullOrWhiteSpace(Workspace) || string.IsNullOrWhiteSpace(Results))
        {
            error = "Vælg både en arbejdsmappe og en resultatmappe.";
            return false;
        }

        var workspace = Path.GetFullPath(Workspace).TrimEnd(Path.DirectorySeparatorChar);
        var results = Path.GetFullPath(Results).TrimEnd(Path.DirectorySeparatorChar);

        if (workspace.Equals(results, StringComparison.OrdinalIgnoreCase) ||
            IsInside(workspace, results) || IsInside(results, workspace))
        {
            error = "Arbejdsmappe og resultatmappe skal være to separate mapper.";
            return false;
        }

        return true;
    }

    private static bool IsInside(string child, string parent)
    {
        var parentWithSlash = parent + Path.DirectorySeparatorChar;
        return child.StartsWith(parentWithSlash, StringComparison.OrdinalIgnoreCase);
    }

    public static string? FindRepoRoot()
    {
        var candidates = new List<string>();
        var baseDir = AppContext.BaseDirectory;
        var current = new DirectoryInfo(baseDir);

        for (var i = 0; i < 6 && current is not null; i++, current = current.Parent)
            candidates.Add(current.FullName);

        candidates.Add(@"C:\HelixGrid");
        candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "HelixGrid"));
        candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "HelixGrid"));

        return candidates.FirstOrDefault(path =>
            File.Exists(Path.Combine(path, "docker-compose.yml")));
    }
}
