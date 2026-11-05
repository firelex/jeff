// offline
using Jeff.Sdk;

var catalogue = Catalogue.Shipped();

foreach (var iface in new[] { "classify", "route", "rate", "detect", "is_supported", "equivalent", "requires_escalation" })
{
    var functions = catalogue.Implementing(iface).Select(function => function.Id).ToList();

    Console.WriteLine($"{iface}: {functions.Count} functions, for example {functions[0]}");
}
