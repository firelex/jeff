using System.Runtime.CompilerServices;

namespace Jeff.Sdk.Tests;

/// <summary>Runs once when the test assembly loads: every transport a test builds without an explicit environment
/// reads proxies from an empty one, so HTTP_PROXY, HTTPS_PROXY, ALL_PROXY or NO_PROXY set on the machine never change a
/// test's route. ProxyTests pass their own environments (RemoteOptions.Environment and the internal overloads).</summary>
internal static class ProxyIsolation
{
    [ModuleInitializer]
    internal static void PinNoProxies() => PyProxies.PinnedEnvironment = [];
}
