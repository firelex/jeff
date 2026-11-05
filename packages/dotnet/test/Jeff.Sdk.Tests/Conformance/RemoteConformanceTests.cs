using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Helpers;
using Microsoft.AspNetCore.Http;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>remote.json: a server that answers each recorded request with its recorded answer, in order, after checking
/// the request is the recorded one (method, path, Authorization, JSON body by content); a request that differs gets 599
/// and is kept, so the test reports it.</summary>
public class RemoteConformanceTests
{
    public static TheoryData<string> Cases => Fixtures.CaseNames("remote.json");

    private static bool SameRequest(JsonObject a, JsonObject b) =>
        Json.StrictEquals(a["method"], b["method"]) && Json.StrictEquals(a["path"], b["path"]) && Json.StrictEquals(a["authorization"], b["authorization"])
        && Json.ContentEquals(a["body"], b["body"]);

    private static JsonObject WithId(JeffError error)
    {
        var wire = Fixtures.WireOf(error);
        wire["request_id"] = error.RequestId;
        return wire;
    }

    [Theory]
    [MemberData(nameof(Cases))]
    public async Task TheExchangesAreThePythonSdks(string name)
    {
        var item = Fixtures.Case("remote.json", name);
        var exchanges = item["exchanges"]!.AsArray().Select(exchange => exchange!.AsObject()).ToList();
        var sent = new List<JsonObject>();
        int next = 0;
        await using var server = await HttpServer.StartAsync(async (context, text) =>
        {
            string? authorization = context.Request.Headers.Authorization.Count > 0 ? context.Request.Headers.Authorization.ToString() : null;
            var got = new JsonObject
            {
                ["method"] = context.Request.Method, ["path"] = context.Request.Path.Value, ["authorization"] = authorization,
                ["body"] = text.Length > 0 ? Json.Parse(text, "the request body") : null,
            };
            JsonObject? answer;
            lock (sent)
            {
                sent.Add(got);
                answer = next < exchanges.Count ? exchanges[next] : null;
                next++;
            }
            if (answer is null || !SameRequest(got, answer["request"]!.AsObject()))
            {
                context.Response.StatusCode = 599;
                await context.Response.WriteAsync(answer is null ? "no recorded answer left" : "this request is not the recorded one");
                return;
            }
            var response = answer["response"]!.AsObject();
            context.Response.StatusCode = (int)response["status"]!.GetValue<long>();
            foreach (var (header, value) in response["headers"]!.AsObject()) context.Response.Headers[header] = value!.GetValue<string>();
            await context.Response.WriteAsync(response["body"]!.GetValue<string>());
        });
        var questions = new JsonArray();
        var asked = new List<Question>();
        var results = new JsonArray();
        JsonObject connect = new() { ["ok"] = true };
        TokenSource? token = item["token"] is JsonValue value ? TokenSource.Of(value.GetValue<string>()) : null;
        RemoteJeffClient? client = null;
        var transport = new RestTransport(server.Url, token, TimeSpan.FromSeconds(10));
        try
        {
            client = await RemoteJeffClient.OverAsync(transport.AsTransport(), Fixtures.CatalogueFor(item["catalogue"]!.GetValue<string>()), transport.Url);
        }
        catch (JeffError error)
        {
            connect = new JsonObject { ["error"] = WithId(error) };
        }
        if (client is not null)
        {
            foreach (var action in item["actions"]!.AsArray().Select(entry => entry!.AsObject()))
            {
                try
                {
                    results.Add(new JsonObject
                    {
                        ["result"] = Fixtures.Normalised(await RunDecisions.RunOpAsync(client, action["op"]!.GetValue<string>(), action["args"]!.AsObject(), questions, asked)),
                    });
                }
                catch (JeffError error)
                {
                    results.Add(new JsonObject { ["error"] = WithId(error) });
                }
            }
        }
        await transport.DisposeAsync();
        Assert.Equal(exchanges.Count, sent.Count);
        for (int index = 0; index < sent.Count; index++)
        {
            Assert.True(SameRequest(exchanges[index]["request"]!.AsObject(), sent[index]),
                $"request {index}: sent {Json.Write(sent[index])}, recorded {Json.Write(exchanges[index]["request"])}");
        }
        JsonAssert.Equal(item["connect"], connect);
        JsonAssert.Equal(item["results"], results);
        JsonAssert.Equal(item["questions"], questions);
    }
}
