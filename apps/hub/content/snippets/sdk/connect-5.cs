// offline
using Jeff.Sdk;

// Both mistakes are refused at once, before any network call or connection.
try
{
    await JeffClient.RemoteAsync("{{origin}}", null, new RemoteOptions { Timeout = TimeSpan.Zero });
}
catch (InvalidRequest error)
{
    Console.WriteLine(error.Message);
}

try
{
    await JeffClient.LocalAsync("/tmp/jeff.sock", new LocalOptions { Timeout = TimeSpan.Zero });
}
catch (InvalidRequest error)
{
    Console.WriteLine(error.Message);
}
