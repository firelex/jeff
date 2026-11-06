using System.Reflection;
using System.Runtime.CompilerServices;

namespace Jeff.Sdk.Tests;

/// <summary>The package's public surface is pinned in PublicApi.txt: every exported type and every public or protected
/// member. A change to the surface fails here until the file is updated with it, so no helper becomes public by
/// mistake and nothing public disappears unnoticed.</summary>
public class SurfaceTests
{
    private const BindingFlags Declared = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;

    [Fact]
    public void ThePackageExportsExactlyThePinnedSurface()
    {
        var actual = Surface(typeof(JeffClient).Assembly);
        string pinned = Path.Combine(AppContext.BaseDirectory, "PublicApi.txt");
        var expected = File.ReadAllLines(pinned).Where(line => line.Length > 0).ToList();
        if (actual.SequenceEqual(expected, StringComparer.Ordinal)) return;
        string written = Path.Combine(AppContext.BaseDirectory, "PublicApi.actual.txt");
        File.WriteAllLines(written, actual);
        var added = actual.Except(expected, StringComparer.Ordinal).ToList();
        var removed = expected.Except(actual, StringComparer.Ordinal).ToList();
        Assert.Fail($"The public surface of Jeff.Sdk differs from test/Jeff.Sdk.Tests/PublicApi.txt.\nAdded:\n  {string.Join("\n  ", added)}\n"
            + $"Removed:\n  {string.Join("\n  ", removed)}\nThe whole surface as built is in {written}; copy it over PublicApi.txt if the change is deliberate.");
    }

    [Fact]
    public void ThePackageExportsTheTypesTheReviewKept()
    {
        var types = typeof(JeffClient).Assembly.GetExportedTypes();
        Assert.Equal(94, types.Length);
        Assert.Equal("jeff.core.router", Routing.DefaultRouter);
        Assert.Equal("Jeff.Sdk", typeof(JeffClient).Assembly.GetName().Name);
    }

    [Fact]
    public void TheProxyPinForTestsIsNotPartOfTheSurface()
    {
        var proxies = typeof(JeffClient).Assembly.GetType("Jeff.Sdk.PyProxies");
        Assert.NotNull(proxies);
        Assert.False(proxies.IsPublic, "PyProxies, which holds the tests' proxy pin, is public");
    }

    /// <summary>One line per exported type, then one per public or protected member of it, in ordinal order.</summary>
    internal static List<string> Surface(Assembly assembly)
    {
        var lines = new List<string>();
        foreach (var type in assembly.GetExportedTypes().OrderBy(type => type.FullName, StringComparer.Ordinal))
        {
            string head = $"{Kind(type)} {TypeName(type)}";
            if (type.BaseType is { } baseType && baseType != typeof(object) && baseType != typeof(ValueType) && baseType != typeof(Enum)
                && baseType != typeof(MulticastDelegate))
            {
                head += $" : {TypeName(baseType)}";
            }
            var interfaces = type.GetInterfaces().Select(TypeName).OrderBy(name => name, StringComparer.Ordinal).ToList();
            if (interfaces.Count > 0) head += $" ({string.Join(", ", interfaces)})";
            lines.Add(head);
            var members = new List<string>();
            foreach (var member in type.GetMembers(Declared))
            {
                if (Line(member) is { } line) members.Add($"  {TypeName(type)}: {line}");
            }
            members.Sort(StringComparer.Ordinal);
            lines.AddRange(members);
        }
        return lines;
    }

    private static string Kind(Type type) =>
        type.IsInterface ? "interface" : type.IsEnum ? "enum" : type.IsValueType ? "struct" : typeof(Delegate).IsAssignableFrom(type) ? "delegate"
        : type.IsAbstract && type.IsSealed ? "static class" : type.IsAbstract ? "abstract class" : type.IsSealed ? "sealed class" : "class";

    private static string? Access(MethodBase? method) => method switch
    {
        null => null,
        { IsPublic: true } => "public",
        { IsFamily: true } or { IsFamilyOrAssembly: true } => "protected",
        _ => null,
    };

    private static string? Line(MemberInfo member)
    {
        switch (member)
        {
            case ConstructorInfo constructor:
                return Access(constructor) is { } access && !constructor.IsStatic ? $"{access} .ctor({Parameters(constructor)})" : null;
            case MethodInfo method:
                if (method.IsSpecialName && !method.Name.StartsWith("op_", StringComparison.Ordinal)) return null; // property and event accessors
                if (Access(method) is not { } methodAccess) return null;
                string generic = method.IsGenericMethod ? $"<{string.Join(", ", method.GetGenericArguments().Select(TypeName))}>" : "";
                return $"{methodAccess} {Modifiers(method)}{TypeName(method.ReturnType)} {method.Name}{generic}({Parameters(method)})";
            case PropertyInfo property:
                string? getter = Access(property.GetMethod);
                string? setter = Access(property.SetMethod);
                if (getter is null && setter is null) return null;
                var accessor = (getter is not null ? property.GetMethod : property.SetMethod)!;
                string accessors = (getter is not null ? "get; " : "")
                    + (setter is null ? "" : property.SetMethod!.ReturnParameter.GetRequiredCustomModifiers().Contains(typeof(IsExternalInit)) ? "init; " : "set; ");
                string indexer = property.GetIndexParameters() is { Length: > 0 } index ? $"[{string.Join(", ", index.Select(Parameter))}]" : "";
                return $"{getter ?? setter} {Modifiers(accessor)}{TypeName(property.PropertyType)} {property.Name}{indexer} {{ {accessors}}}";
            case FieldInfo field:
                string? fieldAccess = field.IsPublic ? "public" : field.IsFamily || field.IsFamilyOrAssembly ? "protected" : null;
                if (fieldAccess is null || field.IsSpecialName) return null;
                string kind = field.IsLiteral ? "const " : field.IsStatic ? field.IsInitOnly ? "static readonly " : "static " : field.IsInitOnly ? "readonly " : "";
                return $"{fieldAccess} {kind}{TypeName(field.FieldType)} {field.Name}";
            case EventInfo @event:
                return Access(@event.AddMethod) is { } eventAccess ? $"{eventAccess} event {TypeName(@event.EventHandlerType!)} {@event.Name}" : null;
            default:
                return null; // nested types are listed as types of their own
        }
    }

    private static string Modifiers(MethodInfo method) =>
        method.IsStatic ? "static "
        : method.IsAbstract ? "abstract "
        : method.IsVirtual && !method.IsFinal ? (method.GetBaseDefinition() == method ? "virtual " : "override ")
        : method.IsVirtual && method.GetBaseDefinition() != method ? "sealed override "
        : "";

    private static string Parameters(MethodBase method) => string.Join(", ", method.GetParameters().Select(Parameter));

    private static string Parameter(ParameterInfo parameter)
    {
        string type = TypeName(parameter.ParameterType);
        if (parameter.ParameterType.IsByRef) type = (parameter.IsOut ? "out " : "ref ") + type;
        if (parameter.IsDefined(typeof(ParamArrayAttribute))) type = "params " + type;
        return parameter.HasDefaultValue ? $"{type} {parameter.Name} = {parameter.DefaultValue ?? "null"}" : $"{type} {parameter.Name}";
    }

    private static string TypeName(Type type)
    {
        if (type.IsByRef) return TypeName(type.GetElementType()!);
        if (type.IsArray) return TypeName(type.GetElementType()!) + "[]";
        if (type.IsGenericParameter) return type.Name;
        if (Nullable.GetUnderlyingType(type) is { } underlying) return TypeName(underlying) + "?";
        string name = type.IsNested ? $"{TypeName(type.DeclaringType!)}.{type.Name}"
            : type.Namespace == "Jeff.Sdk" ? type.Name
            : $"{type.Namespace}.{type.Name}";
        if (!type.IsGenericType) return name;
        int tick = name.LastIndexOf('`');
        var arguments = type.GetGenericArguments();
        if (type.IsNested && type.DeclaringType!.IsGenericType) arguments = arguments[type.DeclaringType.GetGenericArguments().Length..];
        return (tick < 0 ? name : name[..tick]) + (arguments.Length > 0 ? $"<{string.Join(", ", arguments.Select(TypeName))}>" : "");
    }
}
