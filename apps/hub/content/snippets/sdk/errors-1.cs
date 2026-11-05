// offline
using Jeff.Sdk;

var catalogue = Catalogue.Shipped();

foreach (var reference in new[] { "guard.atempt", "guard.attempt.extra", "guard" })
{
    try
    {
        catalogue.Function(reference);
    }
    catch (Exception error) when (error is UnknownFunction or InvalidReference)
    {
        Console.WriteLine($"{error.GetType().Name} - {error.Message}");
    }
}

// Every Jeff error is a JeffError, so one catch clause catches them all; RequestId is null for an error raised in this
// program (a server's error carries the id of the request that failed).
try
{
    catalogue.Function("guard.atempt");
}
catch (JeffError error)
{
    Console.WriteLine($"{error.Type} {error.RequestId ?? "null"}");
}
