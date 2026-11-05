using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class ErrorsTests
{
    private static readonly string[] Names =
    [
        "JeffError", "ContractError", "ManifestError", "InvalidReference", "UnknownFunction", "NotInstalled", "MissingContext",
        "UnknownContext", "MissingLabels", "InvalidLabels", "TooManyLabels", "TooManyHandles", "InterfaceMismatch", "LowConfidence",
        "EscalationFailed", "EscalationBudgetExceeded", "InvalidPolicy", "GraphError", "RuntimeMissing", "ProprietaryWeights",
        "CalibrationError", "EngineError", "InputTooLong", "BackendUnavailable", "Busy", "DeadlineExceeded", "ProtocolMismatch",
        "NotReady", "Unauthorised", "NotLicensed", "RateLimited", "UsageLimitReached", "InvalidRequest", "Conflict", "FactoryOff",
    ];

    [Fact]
    public void ThereIsOneClassPerContractErrorEachAJeffErrorNamedAsItsEntry()
    {
        Assert.Equal(Names.OrderBy(name => name, StringComparer.Ordinal), Errors.Classes.Keys.OrderBy(name => name, StringComparer.Ordinal));
        foreach (var (name, make) in Errors.Classes)
        {
            var error = make("m", null);
            Assert.IsAssignableFrom<JeffError>(error);
            Assert.Equal(name, error.Type);
            Assert.Equal(name, error.GetType().Name);
        }
    }

    [Fact]
    public void TheTypedConstructorsWriteThePythonMessages()
    {
        var error = Errors.NotInstalledError("jeff.support.triage.urgency", "it is an escalation step");
        Assert.Equal("jeff.support.triage.urgency is not installed: it is an escalation step. Install it with: jeff pull jeff.support.triage",
            error.Message);
        JsonAssert.Equal(JsonAssert.Parse("{\"function_id\": \"jeff.support.triage.urgency\"}"), error.Details);
        Assert.Equal("jeff.core.router is not installed. Install it with: jeff pull jeff.core.router",
            Errors.NotInstalledError("jeff.core.router").Message);
        var missing = Errors.MissingContextError("spam.x", ["channel", "region"]);
        Assert.Equal("spam.x needs context field(s) channel, region; pass them in context=", missing.Message);
        Assert.Equal(new[] { "channel", "region" }, missing.Fields);
        JsonAssert.Equal(JsonAssert.Parse("{\"function_id\": \"spam.x\", \"fields\": [\"channel\", \"region\"]}"), missing.Details);
    }

    [Fact]
    public void AnErrorOnTheWireBecomesTheSameClassWithTheSameMessageAndDetails()
    {
        var error = Errors.FromWire("Busy", "full", JsonAssert.Parse("{\"retry_after_s\": 2.5}"));
        var busy = Assert.IsType<Busy>(error);
        Assert.Equal(2.5, busy.RetryAfterS);
        JsonAssert.Equal(JsonAssert.Parse("{\"type\": \"Busy\", \"message\": \"full\", \"details\": {\"retry_after_s\": 2.5}}"), Errors.ToWire(error));
    }

    [Fact]
    public void AnUnknownTypeOnTheWireIsAnEngineErrorNamingIt()
    {
        var error = Assert.IsType<EngineError>(Errors.FromWire("Weird", "Something odd", null));
        Assert.Equal("unknown error type 'Weird' from the engine: Something odd", error.Message);
        Assert.Empty(error.Details);
    }

    [Fact]
    public void ATypedPropertyThrowsNamingTheMissingFieldInsteadOfInventingAValue()
    {
        var broken = new NotInstalled("boom");
        var error = Assert.Throws<ContractError>(() => broken.FunctionId);
        Assert.Equal("NotInstalled.details is missing its 'function_id' string (got None)", error.Message);
    }

    [Fact]
    public void CopyGivesEachWaitingCallerItsOwnInstanceWithTheSameNotesAndRequestId()
    {
        var original = Errors.NotInstalledError("jeff.core.router");
        original.RequestId = "req-1";
        original.Notes.Add("also tried another router");
        var copy = Errors.Copy(original);
        Assert.NotSame(original, copy);
        Assert.IsType<NotInstalled>(copy);
        Assert.Equal(original.Message, copy.Message);
        JsonAssert.Equal(original.Details, copy.Details);
        Assert.Equal("req-1", copy.RequestId);
        copy.Notes.Add("only on the copy");
        Assert.Equal(new[] { "also tried another router" }, original.Notes);
    }

    [Fact]
    public void DetailsAreCopiedSoTheCallersObjectIsNotAttached()
    {
        var details = new JsonObject { ["step"] = "llm" };
        var error = new EscalationFailed("x", details);
        details["step"] = "human";
        Assert.Equal("llm", error.Step);
    }

    [Fact]
    public void TheClassesAreExactlyTheEntriesOfErrorsYaml()
    {
        Assert.Equal(Contract.ErrorCatalogue().Keys.OrderBy(name => name, StringComparer.Ordinal),
            Errors.Classes.Keys.OrderBy(name => name, StringComparer.Ordinal));
        Assert.Equal(424L, Contract.ErrorCatalogue()["NotInstalled"].Status);
        Assert.Equal(402L, Contract.ErrorCatalogue()["UsageLimitReached"].Status);
    }

    [Fact]
    public void UsageLimitReachedReadsItsFourDetails()
    {
        var error = Errors.FromWire("UsageLimitReached",
            "This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then",
            JsonAssert.Parse("{\"account\": \"u_1\", \"used\": 50000123, \"allowance\": 50000000, \"period_end\": \"2026-12-03T10:00:00Z\"}"));
        var limit = Assert.IsType<UsageLimitReached>(error);
        Assert.Equal(("u_1", 50_000_123L, 50_000_000L, "2026-12-03T10:00:00Z"), (limit.Account, limit.Used, limit.Allowance, limit.PeriodEnd));
        Assert.Equal("UsageLimitReached.details is missing its 'period_end' string (got None)",
            Assert.Throws<ContractError>(() => new UsageLimitReached("m").PeriodEnd).Message);
    }
}
