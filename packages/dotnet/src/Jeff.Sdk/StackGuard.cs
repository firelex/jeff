using System.Runtime.CompilerServices;

namespace Jeff.Sdk;

/// <summary>The check every recursive walk of a value makes at each level (JSON reading, writing, copying and
/// comparing, repr, MessagePack, YAML, schema validation): a fixed depth cap alone does not protect a thread with a
/// small stack (a thread started with a set maxStackSize, a deep caller), and a stack overflow cannot be caught and
/// ends the process. When the stack runs low the walk throws its module's named error with this wording instead.</summary>
internal static class StackGuard
{
    public const string TooDeep = "is nested too deeply for the stack this thread has left";

    public static bool Exhausted => !RuntimeHelpers.TryEnsureSufficientExecutionStack();
}
