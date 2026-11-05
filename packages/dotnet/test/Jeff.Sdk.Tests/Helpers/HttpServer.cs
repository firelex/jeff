using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

namespace Jeff.Sdk.Tests.Helpers;

/// <summary>A local HTTP server (Kestrel on 127.0.0.1, a free port) whose every request goes to `handler` with the
/// request body read as text.</summary>
internal sealed class HttpServer : IAsyncDisposable
{
    private readonly WebApplication app;

    private HttpServer(WebApplication app, string url)
    {
        this.app = app;
        Url = url;
    }

    public string Url { get; }

    public static async Task<HttpServer> StartAsync(Func<HttpContext, string, Task> handler, Action<WebApplication>? configure = null)
    {
        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        var app = builder.Build();
        configure?.Invoke(app);
        app.Run(async context =>
        {
            using var reader = new StreamReader(context.Request.Body);
            string body = await reader.ReadToEndAsync();
            await handler(context, body);
        });
        await app.StartAsync();
        string url = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
        return new HttpServer(app, url.TrimEnd('/'));
    }

    /// <summary>Writes a JSON answer with the given status and request id.</summary>
    public static Task Json(HttpContext context, int status, string body, string requestId = "rid-1")
    {
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json";
        context.Response.Headers["x-request-id"] = requestId;
        return context.Response.WriteAsync(body);
    }

    public async ValueTask DisposeAsync()
    {
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        await app.StopAsync(stop.Token);
        await app.DisposeAsync();
    }
}
