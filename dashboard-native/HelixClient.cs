using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace HelixGrid.Dashboard;

internal sealed record HelixStatus(bool Online, int Workers, int Workflows);

internal sealed record WorkflowRow(string Id, string Name, string State, string CreatedAt);

internal sealed class HelixClient
{
    private readonly HttpClient _http = new() { BaseAddress = new Uri("http://127.0.0.1:8080"), Timeout = TimeSpan.FromSeconds(8) };

    public async Task<bool> IsOnlineAsync()
    {
        try
        {
            using var response = await _http.GetAsync("/healthz");
            return response.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<HelixStatus> GetStatusAsync()
    {
        if (!await IsOnlineAsync())
            return new HelixStatus(false, 0, 0);

        try
        {
            var workers = await CountDataArrayAsync("/v1/workers");
            var workflows = await CountDataArrayAsync("/v1/workflows");
            return new HelixStatus(true, workers, workflows);
        }
        catch
        {
            return new HelixStatus(true, 0, 0);
        }
    }

    private async Task<int> CountDataArrayAsync(string path)
    {
        using var response = await _http.GetAsync(path);
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        return doc.RootElement.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Array
            ? data.GetArrayLength()
            : 0;
    }

    public async Task<List<WorkflowRow>> GetWorkflowsAsync()
    {
        using var response = await _http.GetAsync("/v1/workflows");
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());

        var rows = new List<WorkflowRow>();
        if (!doc.RootElement.TryGetProperty("data", out var data) || data.ValueKind != JsonValueKind.Array)
            return rows;

        foreach (var item in data.EnumerateArray())
        {
            rows.Add(new WorkflowRow(
                Get(item, "id"),
                Get(item, "name"),
                Get(item, "state"),
                Get(item, "created_at")));
        }
        return rows;
    }

    public async Task CancelWorkflowAsync(string id)
    {
        using var body = new StringContent("{}", Encoding.UTF8, "application/json");
        using var response = await _http.PostAsync($"/v1/workflows/{Uri.EscapeDataString(id)}/cancel", body);
        response.EnsureSuccessStatusCode();
    }

    public async Task<string> SubmitWorkflowAsync(string json)
    {
        using var body = new StringContent(json, Encoding.UTF8, "application/json");
        using var response = await _http.PostAsync("/v1/workflows", body);
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        return doc.RootElement.GetProperty("data").GetProperty("id").GetString()
            ?? throw new InvalidOperationException("Coordinator returnerede ikke et workflow-id.");
    }

    public async Task<string> GetWorkflowStateAsync(string id)
    {
        using var response = await _http.GetAsync($"/v1/workflows/{Uri.EscapeDataString(id)}");
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        return doc.RootElement.GetProperty("data").GetProperty("state").GetString() ?? "?";
    }

    private static string Get(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) ? value.ToString() : "";
}
