using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

/// <summary>Every recursive walk of a value, run on a thread with a small stack (256 KB, as a thread started with a
/// set maxStackSize may have), stops with its module's named error when the stack runs low: a stack overflow cannot
/// be caught and would end the process.</summary>
public class StackTests
{
    private const int SmallStack = 256 * 1024;

    private static Exception? OnSmallStack(Action action, int stack = SmallStack)
    {
        Exception? caught = null;
        var thread = new Thread(() =>
        {
            try
            {
                action();
            }
            catch (Exception error)
            {
                caught = error;
            }
        }, stack);
        thread.Start();
        thread.Join();
        return caught;
    }

    private static T Fails<T>(Action action, int stack = SmallStack) where T : Exception
    {
        var error = Assert.IsType<T>(OnSmallStack(action, stack));
        Assert.Contains(StackGuard.TooDeep, error.Message);
        return error;
    }

    private static JsonObject Condition(int levels)
    {
        var condition = new JsonObject { ["node"] = "a", ["is"] = 1.5 };
        for (int level = 0; level < levels; level++) condition = new JsonObject { ["not"] = condition };
        return condition;
    }

    private static JsonNode Nested(int levels)
    {
        JsonNode value = new JsonArray();
        for (int level = 1; level < levels; level++) value = new JsonArray(value);
        return value;
    }

    [Fact]
    public void ValidatingADeepConditionIsAContractError()
    {
        Fails<ContractError>(() => Contract.FirstProblem("graph", Condition(165), "condition"));
        Fails<ContractError>(() => Contract.IsValid("graph", Condition(400), "condition"));
        Fails<ContractError>(() => Contract.Validate("graph", Condition(400), "a condition", "condition"));
    }

    [Fact]
    public void QuotingADeepValueIsAContractError()
    {
        Fails<ContractError>(() => PyFormat.Repr(Nested(999)));
        // the root type error quotes the whole document, which the depth cap of the descent never sees
        Fails<ContractError>(() => Contract.FirstProblem("decision", Nested(999)));
    }

    [Fact]
    public void TheJsonWalksStopWithTheirNamedErrors()
    {
        string text = new string('[', 999) + new string(']', 999);
        var parse = Fails<ContractError>(() => Json.Parse(text, "a deep document"));
        Assert.StartsWith("a deep document is not valid JSON: the value is nested too deeply", parse.Message);
        var deep = Nested(5000);
        Fails<ContractError>(() => Json.Write(deep));
        Fails<InvalidRequest>(() => Json.Canonical(deep));
        Fails<ContractError>(() => Json.StrictEquals(deep, deep));
        Fails<ContractError>(() => PyFormat.PyEquals(deep, deep));
        Fails<ContractError>(() => SchemaRegistry.SchemaEqual(deep, deep));
    }

    [Fact]
    public void MessagePackAndYamlStopWithTheirNamedErrors()
    {
        // below their 512-level caps, so only the stack check can stop them on a thread this small
        const int tiny = 160 * 1024;
        Fails<MsgPackError>(() => MsgPack.Encode(Nested(500)), tiny);
        var packed = Enumerable.Repeat((byte)0x91, 499).Append((byte)0x90).ToArray();
        Fails<MsgPackError>(() => MsgPack.Decode(packed), tiny);
        var yaml = new string('[', 500) + new string(']', 500);
        Fails<ManifestError>(() => PyYaml.Load(yaml, "deep.yaml"), tiny);
    }

    [Fact]
    public void ShallowValuesStillWorkOnASmallStack()
    {
        Assert.Null(OnSmallStack(() =>
        {
            Assert.Equal("1.5 is not of type 'string', 'boolean', 'integer' (at /not/not/is)", Contract.FirstProblem("graph", Condition(2), "condition"));
            Assert.Equal("[[[]]]", PyFormat.Repr(Nested(3)));
        }));
    }
}
