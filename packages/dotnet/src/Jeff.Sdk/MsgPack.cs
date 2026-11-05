using System.Buffers;
using System.Buffers.Binary;
using System.Numerics;
using System.Text;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>What cannot be packed or read as the engine protocol's MessagePack.</summary>
internal sealed class MsgPackError(string message) : Exception(message);

/// <summary>MessagePack exactly as the Python SDK packs it (msgpack.packb(value, use_bin_type=True)): maps in their key
/// order, ints in the smallest format (positive ints unsigned, up to 2**64-1; an int outside -2**63 to 2**64-1 is refused
/// with msgpack-python's OverflowError message), floats as float64, strings as fixstr/str8/str16/str32. Reading gives
/// canonical JSON: ints as whole numbers (a uint64 above 2**63-1 included), floats as double, maps in their order (a
/// repeated key keeps its first place and takes the last value, as a Python dict). Binary, extension types and
/// non-string map keys are refused: the protocol carries none.</summary>
internal static class MsgPack
{
    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);
    private const int MaxDepth = 512;

    public static byte[] Encode(JsonNode? value)
    {
        var output = new ArrayBufferWriter<byte>();
        Write(output, value, 0);
        return output.WrittenSpan.ToArray();
    }

    private static void Write(ArrayBufferWriter<byte> output, JsonNode? value, int depth)
    {
        if (depth > MaxDepth) throw new MsgPackError($"The value is nested more than {MaxDepth} levels deep");
        if (StackGuard.Exhausted) throw new MsgPackError($"The value {StackGuard.TooDeep}");
        switch (Json.KindOf(value))
        {
            case JsonKind.Null: Byte(output, 0xc0); break;
            case JsonKind.Bool: Byte(output, value!.GetValue<bool>() ? (byte)0xc3 : (byte)0xc2); break;
            case JsonKind.Int:
                if (Json.TryLong(value, out long whole)) WriteInt(output, whole);
                else if (Json.WholeOf(value) is var big && big.Sign > 0 && big <= ulong.MaxValue) Big(output, 0xcf, (ulong)big, 8);
                // msgpack-python's packb raises OverflowError("Integer value out of range"); the type is part of the
                // message so that a refused frame reads as Python's "cannot be packed with msgpack: OverflowError: ..."
                else throw new MsgPackError("OverflowError: Integer value out of range");
                break;
            case JsonKind.Float:
                Byte(output, 0xcb);
                BinaryPrimitives.WriteInt64BigEndian(output.GetSpan(8), BitConverter.DoubleToInt64Bits(value!.GetValue<double>()));
                output.Advance(8);
                break;
            case JsonKind.String: WriteString(output, value!.GetValue<string>()); break;
            case JsonKind.Array:
                var array = value!.AsArray();
                Header(output, array.Count, 0x90, 16, 0xdc, 0xdd);
                foreach (var item in array) Write(output, item, depth + 1);
                break;
            case JsonKind.Object:
                var obj = value!.AsObject();
                Header(output, obj.Count, 0x80, 16, 0xde, 0xdf);
                foreach (var (key, item) in obj)
                {
                    WriteString(output, key);
                    Write(output, item, depth + 1);
                }
                break;
        }
    }

    private static void Byte(ArrayBufferWriter<byte> output, byte value)
    {
        output.GetSpan(1)[0] = value;
        output.Advance(1);
    }

    private static void Big(ArrayBufferWriter<byte> output, byte code, ulong value, int size)
    {
        Byte(output, code);
        var span = output.GetSpan(size);
        for (int index = 0; index < size; index++) span[index] = (byte)(value >> (8 * (size - 1 - index)));
        output.Advance(size);
    }

    private static void WriteInt(ArrayBufferWriter<byte> output, long value)
    {
        if (value >= 0)
        {
            if (value < 0x80) Byte(output, (byte)value);
            else if (value <= 0xff) Big(output, 0xcc, (ulong)value, 1);
            else if (value <= 0xffff) Big(output, 0xcd, (ulong)value, 2);
            else if (value <= 0xffffffffL) Big(output, 0xce, (ulong)value, 4);
            else Big(output, 0xcf, (ulong)value, 8);
        }
        else if (value >= -32) Byte(output, (byte)(sbyte)value);
        else if (value >= sbyte.MinValue) Big(output, 0xd0, (ulong)value, 1);
        else if (value >= short.MinValue) Big(output, 0xd1, (ulong)value, 2);
        else if (value >= int.MinValue) Big(output, 0xd2, (ulong)value, 4);
        else Big(output, 0xd3, (ulong)value, 8);
    }

    private static void WriteString(ArrayBufferWriter<byte> output, string text)
    {
        byte[] bytes;
        try
        {
            bytes = StrictUtf8.GetBytes(text);
        }
        catch (EncoderFallbackException)
        {
            throw new MsgPackError($"The string {PyFormat.CodePointPrefix(PyFormat.Str(text), 60)} holds a lone surrogate, which UTF-8 cannot carry");
        }
        if (bytes.Length < 32) Byte(output, (byte)(0xa0 | bytes.Length));
        else if (bytes.Length <= 0xff) Big(output, 0xd9, (ulong)bytes.Length, 1);
        else if (bytes.Length <= 0xffff) Big(output, 0xda, (ulong)bytes.Length, 2);
        else Big(output, 0xdb, (ulong)bytes.Length, 4);
        bytes.CopyTo(output.GetSpan(bytes.Length));
        output.Advance(bytes.Length);
    }

    private static void Header(ArrayBufferWriter<byte> output, int count, byte fix, int fixLimit, byte code16, byte code32)
    {
        if (count < fixLimit) Byte(output, (byte)(fix | count));
        else if (count <= 0xffff) Big(output, code16, (ulong)count, 2);
        else Big(output, code32, (ulong)count, 4);
    }

    public static JsonNode? Decode(ReadOnlySpan<byte> data)
    {
        int position = 0;
        var value = Read(data, ref position, 0);
        if (position != data.Length) throw new MsgPackError($"There are {data.Length - position} extra bytes after the value");
        return value;
    }

    private static ReadOnlySpan<byte> Take(ReadOnlySpan<byte> data, ref int position, int count)
    {
        if (count < 0 || data.Length - position < count) throw new MsgPackError($"The data ends inside a value (at byte {position})");
        var span = data.Slice(position, count);
        position += count;
        return span;
    }

    private static ulong Unsigned(ReadOnlySpan<byte> data, ref int position, int size)
    {
        var bytes = Take(data, ref position, size);
        ulong value = 0;
        foreach (byte item in bytes) value = (value << 8) | item;
        return value;
    }

    /// <summary>A str32/array32/map32 length: a MsgPackError (not a raw OverflowException) when it is above
    /// int.MaxValue, which no engine payload reaches but which .NET cannot index or allocate for anyway.</summary>
    private static int Length32(ReadOnlySpan<byte> data, ref int position, int at)
    {
        ulong length = Unsigned(data, ref position, 4);
        if (length > int.MaxValue) throw new MsgPackError($"The length {length} (at byte {at}) is larger than this reader can hold (2**31-1)");
        return (int)length;
    }

    private static JsonNode? Read(ReadOnlySpan<byte> data, ref int position, int depth)
    {
        if (depth > MaxDepth) throw new MsgPackError($"The data is nested more than {MaxDepth} levels deep");
        if (StackGuard.Exhausted) throw new MsgPackError($"The data {StackGuard.TooDeep}");
        int at = position;
        byte code = Take(data, ref position, 1)[0];
        if (code <= 0x7f) return JsonValue.Create((long)code);
        if (code >= 0xe0) return JsonValue.Create((long)(sbyte)code);
        if (code >= 0xa0 && code <= 0xbf) return Text(data, ref position, code & 0x1f);
        if (code >= 0x90 && code <= 0x9f) return Array(data, ref position, code & 0x0f, depth);
        if (code >= 0x80 && code <= 0x8f) return Map(data, ref position, code & 0x0f, depth);
        switch (code)
        {
            case 0xc0: return null;
            case 0xc2: return JsonValue.Create(false);
            case 0xc3: return JsonValue.Create(true);
            case 0xcc: return JsonValue.Create((long)Unsigned(data, ref position, 1));
            case 0xcd: return JsonValue.Create((long)Unsigned(data, ref position, 2));
            case 0xce: return JsonValue.Create((long)Unsigned(data, ref position, 4));
            case 0xcf:
                return Json.Whole(Unsigned(data, ref position, 8));
            case 0xd0: return JsonValue.Create((long)(sbyte)Unsigned(data, ref position, 1));
            case 0xd1: return JsonValue.Create((long)(short)Unsigned(data, ref position, 2));
            case 0xd2: return JsonValue.Create((long)(int)Unsigned(data, ref position, 4));
            case 0xd3: return JsonValue.Create((long)Unsigned(data, ref position, 8));
            case 0xca: return JsonValue.Create((double)BitConverter.Int32BitsToSingle((int)Unsigned(data, ref position, 4)));
            case 0xcb: return JsonValue.Create(BitConverter.Int64BitsToDouble((long)Unsigned(data, ref position, 8)));
            case 0xd9: return Text(data, ref position, (int)Unsigned(data, ref position, 1));
            case 0xda: return Text(data, ref position, (int)Unsigned(data, ref position, 2));
            case 0xdb: return Text(data, ref position, Length32(data, ref position, at));
            case 0xdc: return Array(data, ref position, (int)Unsigned(data, ref position, 2), depth);
            case 0xdd: return Array(data, ref position, Length32(data, ref position, at), depth);
            case 0xde: return Map(data, ref position, (int)Unsigned(data, ref position, 2), depth);
            case 0xdf: return Map(data, ref position, Length32(data, ref position, at), depth);
            case 0xc4 or 0xc5 or 0xc6:
                throw new MsgPackError($"A binary value (at byte {at}) is not part of the engine protocol");
            default:
                throw new MsgPackError($"The type byte 0x{code:x2} (at byte {at}) is not part of the engine protocol");
        }
    }

    private static JsonValue Text(ReadOnlySpan<byte> data, ref int position, int length)
    {
        int at = position;
        var bytes = Take(data, ref position, length);
        try
        {
            return JsonValue.Create(StrictUtf8.GetString(bytes));
        }
        catch (DecoderFallbackException)
        {
            throw new MsgPackError($"The string at byte {at} is not valid UTF-8");
        }
    }

    private static JsonArray Array(ReadOnlySpan<byte> data, ref int position, int count, int depth)
    {
        var array = new JsonArray();
        for (int index = 0; index < count; index++) array.Add(Read(data, ref position, depth + 1));
        return array;
    }

    private static JsonObject Map(ReadOnlySpan<byte> data, ref int position, int count, int depth)
    {
        var map = new JsonObject();
        for (int index = 0; index < count; index++)
        {
            int at = position;
            var key = Read(data, ref position, depth + 1);
            if (Json.KindOf(key) != JsonKind.String) throw new MsgPackError($"A map key (at byte {at}) is not a string: {PyFormat.Repr(key)}");
            map[key!.GetValue<string>()] = Read(data, ref position, depth + 1);
        }
        return map;
    }
}
