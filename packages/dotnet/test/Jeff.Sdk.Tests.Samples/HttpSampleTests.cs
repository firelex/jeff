using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Jeff.Sdk;

namespace Jeff.Sdk.Tests.Samples;

/// <summary>The shape of C# the website generates for a worked example of a proprietary function, sent over HTTP
/// (apps/hub/app/lib/sdk-code.server.ts, csharpHttp). This project has no InternalsVisibleTo from Jeff.Sdk, so it
/// reaches only the same public surface the generated sample does: it must compile and run.</summary>
public class HttpSampleTests
{
    private const string DecisionBody = "{\"decisions\": [{\"value\": \"yes\", \"confidence\": 0.9, \"abstained\": false, "
        + "\"calibrated\": true, \"interface\": \"jeff.binary\", \"function\": \"jeff.test.answer\", \"version\": \"1.0.0\", "
        + "\"probabilities\": {\"yes\": 0.9, \"no\": 0.1}, \"score\": null, \"route\": [], \"decided_by\": \"jeff.test.answer\", "
        + "\"escalations\": [], \"timing\": {}, \"evidence_refs\": []}]}";

    /// <summary>A loopback port nothing else is bound to yet.</summary>
    private static int FreePort()
    {
        using var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
        socket.Bind(new IPEndPoint(IPAddress.Loopback, 0));
        return ((IPEndPoint)socket.LocalEndPoint!).Port;
    }

    [Fact]
    public async Task TheWebsitesHttpSampleCompilesAgainstThePublicSurfaceAndRuns()
    {
        string origin = $"http://127.0.0.1:{FreePort()}";
        using var listener = new HttpListener();
        listener.Prefixes.Add($"{origin}/");
        listener.Start();
        string? sentBody = null;
        var serving = Task.Run(async () =>
        {
            var context = await listener.GetContextAsync();
            using var reader = new StreamReader(context.Request.InputStream, Encoding.UTF8);
            sentBody = await reader.ReadToEndAsync();
            byte[] body = Encoding.UTF8.GetBytes(DecisionBody);
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = body.Length;
            await context.Response.OutputStream.WriteAsync(body);
            context.Response.OutputStream.Close();
        });

        Environment.SetEnvironmentVariable("JEFF_API_KEY", "key");
        string valueAndConfidence;
        string reportedUrl;
        try
        {
            // ---- from here, exactly the statements csharpHttp generates (apiOrigin = origin) ----
            var token = Environment.GetEnvironmentVariable("JEFF_API_KEY")
                ?? throw new InvalidOperationException("Set JEFF_API_KEY");

            await using var transport = new RestTransport(origin, token, TimeSpan.FromSeconds(30));

            var call = new JsonObject
            {
                ["function"] = "jeff.test.answer",
                ["input"] = "Is the sky blue?",
            };

            var answers = await transport.DecideCallsAsync(new JsonArray(call), "return", CancellationToken.None);

            var decision = Decision.FromJson(answers[0]!.AsObject());

            valueAndConfidence = $"{decision.Value} {decision.Confidence}";
            reportedUrl = transport.Url;
            // ---- to here ----
        }
        finally
        {
            Environment.SetEnvironmentVariable("JEFF_API_KEY", null);
            await serving;
            listener.Stop();
        }
        Assert.Equal("yes 0.9", valueAndConfidence);
        Assert.Equal(origin, reportedUrl);
        Assert.Equal("{\"calls\":[{\"function\":\"jeff.test.answer\",\"input\":\"Is the sky blue?\"}],\"on_low_confidence\":\"return\"}", sentBody);
    }

    [Fact]
    public async Task ACallHoldingAnOrdinaryIntIsSentAsAWholeNumber()
    {
        // An int literal (2025) in a JsonObject is backed by Int32, not the SDK's canonical long: DecideCallsAsync copies
        // the caller's calls into canonical form before writing them, as every other path to the wire does.
        string origin = $"http://127.0.0.1:{FreePort()}";
        using var listener = new HttpListener();
        listener.Prefixes.Add($"{origin}/");
        listener.Start();
        string? sentBody = null;
        var serving = Task.Run(async () =>
        {
            var context = await listener.GetContextAsync();
            using var reader = new StreamReader(context.Request.InputStream, Encoding.UTF8);
            sentBody = await reader.ReadToEndAsync();
            byte[] body = Encoding.UTF8.GetBytes(DecisionBody);
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = body.Length;
            await context.Response.OutputStream.WriteAsync(body);
            context.Response.OutputStream.Close();
        });
        try
        {
            await using var transport = new RestTransport(origin, "key", TimeSpan.FromSeconds(30));
            var call = new JsonObject
            {
                ["function"] = "jeff.test.answer",
                ["input"] = new JsonObject { ["year"] = 2025, ["text"] = "Is the sky blue?" },
            };
            var answers = await transport.DecideCallsAsync(new JsonArray(call), "return", CancellationToken.None);
            Assert.Equal("yes", Decision.FromJson(answers[0]!.AsObject()).Value?.ToString());
        }
        finally
        {
            await serving;
            listener.Stop();
        }
        Assert.Contains("\"year\":2025", sentBody!.Replace(" ", ""));
    }
}
