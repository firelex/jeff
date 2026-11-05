// Connects to the engine at args[0], makes one predict, prints its probabilities as JSON and returns without disposing
// the engine: the process then ends only if nothing the SDK holds keeps it alive.
using System.Text.Json.Nodes;
using Jeff.Sdk;

var engine = await SocketEngine.ConnectAsync(args[0], TimeSpan.FromSeconds(5));
var result = await engine.PredictAsync("jeff.safety.spam", [new Row(JsonValue.Create("x"), new RowQuestion(QuestionType.Noul, "?", null))]);
Console.WriteLine(Json.Write(new JsonArray(result.Probabilities
    .Select(row => (JsonNode?)new JsonArray(row.Select(p => (JsonNode?)JsonValue.Create(p)).ToArray())).ToArray())));
