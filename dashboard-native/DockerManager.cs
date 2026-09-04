using System.Diagnostics;
using System.Text;

namespace HelixGrid.Dashboard;

internal sealed record CommandResult(int ExitCode, string Output, string Error)
{
    public bool Success => ExitCode == 0;
    public string Combined => string.Join(Environment.NewLine, new[] { Output, Error }.Where(x => !string.IsNullOrWhiteSpace(x)));
}

internal sealed class DockerManager
{
    private readonly AppConfig _config;

    public DockerManager(AppConfig config) => _config = config;

    private ProcessStartInfo CreateStartInfo(string fileName, IEnumerable<string> args)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            WorkingDirectory = _config.RepoRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };

        foreach (var arg in args)
            psi.ArgumentList.Add(arg);

        psi.Environment["HELIX_WORKSPACE"] = Path.GetFullPath(_config.Workspace);
        psi.Environment["HELIX_RESULTS"] = Path.GetFullPath(_config.Results);
        return psi;
    }

    public async Task<CommandResult> RunAsync(string fileName, IEnumerable<string> args, int timeoutSeconds = 120, CancellationToken ct = default)
    {
        using var process = new Process { StartInfo = CreateStartInfo(fileName, args) };
        try
        {
            if (!process.Start())
                return new CommandResult(-1, "", $"Kunne ikke starte {fileName}.");
        }
        catch (Exception ex)
        {
            return new CommandResult(-1, "", ex.Message);
        }

        var stdout = process.StandardOutput.ReadToEndAsync(ct);
        var stderr = process.StandardError.ReadToEndAsync(ct);

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));

        try
        {
            await process.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException)
        {
            try { process.Kill(true); } catch { }
            return new CommandResult(-2, await Safe(stdout), $"Kommandoen overskred {timeoutSeconds} sekunder.");
        }

        return new CommandResult(process.ExitCode, await Safe(stdout), await Safe(stderr));
    }

    private static async Task<string> Safe(Task<string> task)
    {
        try { return await task; } catch { return ""; }
    }

    public async Task<bool> IsDockerReadyAsync()
    {
        var result = await RunAsync("docker", new[] { "info" }, 12);
        return result.Success;
    }

    public async Task<bool> EnsureDockerDesktopAsync()
    {
        if (await IsDockerReadyAsync())
            return true;

        var candidates = new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Docker", "Docker", "Docker Desktop.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Docker", "Docker Desktop.exe")
        };

        var executable = candidates.FirstOrDefault(File.Exists);
        if (executable is null)
            return false;

        try
        {
            Process.Start(new ProcessStartInfo { FileName = executable, UseShellExecute = true });
        }
        catch
        {
            return false;
        }

        for (var i = 0; i < 45; i++)
        {
            await Task.Delay(2000);
            if (await IsDockerReadyAsync())
                return true;
        }
        return false;
    }

    public async Task<CommandResult> StartClusterAsync(bool allowBuild = true)
    {
        var fast = await RunAsync("docker", new[] { "compose", "up", "-d", "--scale", $"worker={_config.Workers}" }, 180);
        if (fast.Success || !allowBuild)
            return fast;

        return await RunAsync("docker", new[] { "compose", "up", "-d", "--build", "--scale", $"worker={_config.Workers}" }, 900);
    }

    public Task<CommandResult> StopClusterAsync() =>
        RunAsync("docker", new[] { "compose", "down" }, 180);

    public async Task<CommandResult> RestartClusterAsync()
    {
        await StopClusterAsync();
        return await StartClusterAsync();
    }

    public Task<CommandResult> LogsAsync(int tail = 400) =>
        RunAsync("docker", new[] { "compose", "logs", "--no-color", "--tail", tail.ToString() }, 30);
}
