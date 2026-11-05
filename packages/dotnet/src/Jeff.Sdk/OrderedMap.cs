using System.Collections;
using System.Diagnostics.CodeAnalysis;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>A map from string keys to values that keeps the order keys were first added in, whatever they look like
/// (a key "10" stays where it was put, as in a Python dict). Setting an existing key replaces its value in place.</summary>
public sealed class OrderedMap<TValue> : IReadOnlyList<KeyValuePair<string, TValue>>
{
    private readonly List<KeyValuePair<string, TValue>> entries = new();
    private readonly Dictionary<string, int> positions = new(StringComparer.Ordinal);

    /// <summary>An empty map.</summary>
    public OrderedMap()
    {
    }

    /// <summary>A map of these pairs in their order; a repeated key keeps its first place and its last value.</summary>
    public OrderedMap(IEnumerable<KeyValuePair<string, TValue>> items)
    {
        foreach (var item in items) Set(item.Key, item.Value);
    }

    /// <summary>How many keys the map has.</summary>
    public int Count => entries.Count;

    /// <summary>The pair at this position, in key order.</summary>
    public KeyValuePair<string, TValue> this[int position] => entries[position];

    /// <summary>The value of `key`; KeyNotFoundException naming the key and the keys the map has when it is not there.</summary>
    public TValue this[string key] => positions.TryGetValue(key, out var position)
        ? entries[position].Value
        : throw new KeyNotFoundException($"No key {PyFormat.Str(key)} in this map; its keys are {string.Join(", ", Keys)}");

    /// <summary>The keys, in the order they were first added (a new list each call).</summary>
    public IReadOnlyList<string> Keys => entries.ConvertAll(entry => entry.Key);

    /// <summary>The values, in key order (a new list each call).</summary>
    public IReadOnlyList<TValue> Values => entries.ConvertAll(entry => entry.Value);

    /// <summary>Whether the map has `key`.</summary>
    public bool ContainsKey(string key) => positions.ContainsKey(key);

    /// <summary>The value of `key` when the map has it; false otherwise.</summary>
    public bool TryGetValue(string key, [MaybeNullWhen(false)] out TValue value)
    {
        if (positions.TryGetValue(key, out var position))
        {
            value = entries[position].Value;
            return true;
        }
        value = default;
        return false;
    }

    internal void Set(string key, TValue value)
    {
        if (positions.TryGetValue(key, out var position)) entries[position] = new(key, value);
        else
        {
            positions[key] = entries.Count;
            entries.Add(new(key, value));
        }
    }

    internal void Add(string key, TValue value)
    {
        if (positions.ContainsKey(key)) throw new ArgumentException($"The key {PyFormat.Str(key)} is already in this map");
        Set(key, value);
    }

    /// <summary>A copy of the map (the values themselves are not copied).</summary>
    public OrderedMap<TValue> Copy() => new(entries);

    /// <summary>The pairs, in key order.</summary>
    public IEnumerator<KeyValuePair<string, TValue>> GetEnumerator() => entries.GetEnumerator();

    IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
}

/// <summary>Read-only copies for values a shared manifest hands out.</summary>
internal static class ReadOnly
{
    /// <summary>The items as a list that cannot be cast back to one that changes.</summary>
    public static IReadOnlyList<T> List<T>(IEnumerable<T> items) => Array.AsReadOnly(items.ToArray());

    /// <summary>A copy of the map whose JSON values are copies too.</summary>
    public static OrderedMap<JsonNode?> Copy(OrderedMap<JsonNode?> map) =>
        new(map.Select(pair => new KeyValuePair<string, JsonNode?>(pair.Key, Json.Canonical(pair.Value))));
}
