// offline
using Jeff.Sdk;

var graph = Graphs.Load("graphs-message-check.yaml");

Console.WriteLine($"{graph.Id} {graph.Version} {string.Join(", ", graph.Params)}");

Console.WriteLine(string.Join(", ", graph.Order)); // every node after the nodes it depends on

// A graph that refers to a node it does not have is refused when it is loaded.
var broken = "graphs-broken.yaml";

File.WriteAllText(broken, File.ReadAllText("graphs-message-check.yaml").Replace("after: [attack]", "after: [atack]"));

try
{
    Graphs.Load(broken);
}
catch (GraphError error)
{
    Console.WriteLine(error.Message);
}
finally
{
    File.Delete(broken);
}
