using System.Globalization;
using System.Numerics;
using System.Text;

namespace Jeff.Sdk;

/// <summary>Python's re.compile refused the pattern; the message is Python's own re.error text, position included
/// ("unterminated character set at position 0").</summary>
internal sealed class PythonReError(string message) : Exception(message);

/// <summary>The pattern uses a construct this check does not follow (named in the message), so whether Python accepts
/// it is not decided here.</summary>
internal sealed class PythonReUnchecked(string construct) : Exception(construct);

/// <summary>Whether Python 3.13's re.compile accepts a pattern, decided by a port of the syntax checks of CPython's
/// re/_parser.py (and the one compile-time check, the repetition limit), working on code points as Python does. It
/// never builds a .NET Regex: .NET accepts some patterns Python refuses (\p{L}, \e, (?&lt;name&gt;x), \k&lt;n&gt;,
/// variable-width look-behind) and refuses some Python accepts (\_, possessive a*+, (?P&lt;name&gt;x)).
/// <para>Followed exactly: literals and every escape (\x, \u, \U, octal, the categories \d \D \s \S \w \W, the anchors
/// \A \b \B \Z, unknown ASCII-letter escapes refused), character classes and ranges, the quantifiers * + ? {m} {m,}
/// {,n} {m,n} with their lazy (?) and possessive (+) forms, ., ^, $, alternation, capturing groups, named groups
/// (?P&lt;name&gt;...) with ASCII names, (?:...), (?&gt;...), look-ahead (?=...) and (?!...), comments (?#...), references
/// to groups (\1, (?P=name)), and every error those raise, with Python's message and position.</para>
/// <para>Not followed, and refused with PythonReUnchecked: conditionals (?(1)...); look-behind (?&lt;=...), (?&lt;!...),
/// whose fixed-width rule needs the widths of everything inside; inline flags (?i), (?x:...), which change how the rest
/// is read; group names that are not ASCII (Python checks them against Unicode's identifier tables); and \N{name}, which
/// needs Unicode's character name table.</para></summary>
internal static class PythonReSyntax
{
    private const long MaxRepeat = 4294967295; // _sre.MAXREPEAT

    private enum Item { At, Repeat, Other }

    /// <summary>Returns when Python accepts `pattern`; throws PythonReError with Python's message when it refuses it,
    /// PythonReUnchecked when the pattern uses a construct this check does not follow.</summary>
    public static void Check(string pattern)
    {
        var source = new Tokenizer(pattern);
        var state = new State();
        ParseSub(source, state);
        if (source.Next is not null) throw source.Error("unbalanced parenthesis");
    }

    private sealed class State
    {
        public int Groups = 1; // Python's state.groups: the groups opened so far, plus group 0
        public readonly HashSet<int> Closed = new();
        public readonly Dictionary<string, int> Names = new(StringComparer.Ordinal);

        public int Open(Tokenizer source, string? name)
        {
            int id = Groups++;
            if (name is null) return id;
            if (Names.TryGetValue(name, out int earlier))
            {
                throw source.Error($"redefinition of group name {PyFormat.Str(name)} as group {id.ToString(CultureInfo.InvariantCulture)}; "
                    + $"was group {earlier.ToString(CultureInfo.InvariantCulture)}", CodePointLength(name) + 1);
            }
            Names[name] = id;
            return id;
        }
    }

    private sealed class Tokenizer
    {
        private readonly string[] units; // the pattern's code points, each as a string
        private readonly bool multiline;
        private int index;

        public Tokenizer(string pattern)
        {
            var points = new List<string>();
            for (int at = 0; at < pattern.Length; at++)
            {
                if (char.IsHighSurrogate(pattern[at]) && at + 1 < pattern.Length && char.IsLowSurrogate(pattern[at + 1]))
                {
                    points.Add(pattern.Substring(at, 2));
                    at++;
                }
                else points.Add(pattern[at].ToString());
            }
            units = points.ToArray();
            multiline = units.Contains("\n");
            Advance();
        }

        /// <summary>The next token: one code point, or a backslash and the code point after it; null at the end.</summary>
        public string? Next { get; private set; }

        /// <summary>The token's length in code points (Python's len of it).</summary>
        public int NextLength { get; private set; }

        public int Tell => index - NextLength;

        private void Advance()
        {
            int at = index;
            if (at >= units.Length)
            {
                Next = null;
                NextLength = 0;
                return;
            }
            string token = units[at];
            int length = 1;
            if (token == "\\")
            {
                at++;
                if (at >= units.Length) throw ErrorAt("bad escape (end of pattern)", units.Length - 1);
                token += units[at];
                length = 2;
            }
            index = at + 1;
            Next = token;
            NextLength = length;
        }

        public bool Match(string token)
        {
            if (Next != token) return false;
            Advance();
            return true;
        }

        public string? Get()
        {
            string? token = Next;
            Advance();
            return token;
        }

        public string GetWhile(int count, string charset)
        {
            var result = new StringBuilder();
            for (int done = 0; done < count; done++)
            {
                if (Next is not { Length: 1 } token || !charset.Contains(token[0])) break;
                result.Append(token);
                Advance();
            }
            return result.ToString();
        }

        public void Seek(int position)
        {
            index = position;
            Advance();
        }

        /// <summary>Python's getuntil: the tokens up to `terminator`, which is consumed.</summary>
        public string GetUntil(string terminator, string name)
        {
            var result = new StringBuilder();
            int length = 0;
            while (true)
            {
                string? token = Next;
                Advance();
                if (token is null)
                {
                    if (length == 0) throw Error("missing " + name);
                    throw Error($"missing {terminator}, unterminated name", length);
                }
                if (token == terminator)
                {
                    if (length == 0) throw Error("missing " + name, 1);
                    return result.ToString();
                }
                result.Append(token);
                length += PyFormat.CodePointLength(token);
            }
        }

        /// <summary>Python's checkgroupname for a str pattern: the name must be an identifier. Only ASCII names are
        /// decided here (Python's isidentifier follows Unicode's identifier tables for the others).</summary>
        public void CheckGroupName(string name, int offset)
        {
            if (name.Any(character => character > 0x7F)) throw new PythonReUnchecked($"the group name {PyFormat.Str(name)}, which is not ASCII");
            bool identifier = name.Length > 0 && (char.IsAsciiLetter(name[0]) || name[0] == '_')
                && name.All(character => char.IsAsciiLetterOrDigit(character) || character == '_');
            if (!identifier) throw Error($"bad character in group name {PyFormat.Str(name)}", PyFormat.CodePointLength(name) + offset);
        }

        public PythonReError Error(string message, int offset = 0) => ErrorAt(message, Tell - offset);

        /// <summary>re.error's text: the message, " at position N", and the line and column when the pattern has a
        /// newline.</summary>
        private PythonReError ErrorAt(string message, int position)
        {
            string text = $"{message} at position {position.ToString(CultureInfo.InvariantCulture)}";
            if (multiline)
            {
                int line = 1;
                int lastNewline = -1;
                for (int at = 0; at < position && at < units.Length; at++)
                {
                    if (units[at] != "\n") continue;
                    line++;
                    lastNewline = at;
                }
                text += $" (line {line.ToString(CultureInfo.InvariantCulture)}, column {(position - lastNewline).ToString(CultureInfo.InvariantCulture)})";
            }
            return new PythonReError(text);
        }
    }

    private const string Digits = "0123456789";
    private const string OctDigits = "01234567";
    private const string HexDigits = "0123456789abcdefABCDEF";

    private static bool IsAsciiLetter(string text) => text.Length == 1 && char.IsAsciiLetter(text[0]);

    private static int CodePointLength(string text) => PyFormat.CodePointLength(text);

    /// <summary>The code point a one-code-point string holds (a lone surrogate is its own code point, as in Python).</summary>
    private static int CodePoint(string text) => text.Length == 2 ? char.ConvertToUtf32(text[0], text[1]) : text[0];

    /// <summary>A literal's code point, or null for a category (\d and the like) inside a class.</summary>
    private static int? ClassEscape(Tokenizer source, string escape)
    {
        switch (escape)
        {
            case "\\a" or "\\b" or "\\f" or "\\n" or "\\r" or "\\t" or "\\v" or "\\\\":
                return EscapeValue(escape);
            case "\\d" or "\\D" or "\\s" or "\\S" or "\\w" or "\\W":
                return null;
        }
        string c = escape[1..];
        if (c == "N") throw new PythonReUnchecked("\\N{...} (a character by its Unicode name)");
        if (c is "x" or "u" or "U") return HexEscape(source, escape, c);
        if (c.Length == 1 && OctDigits.Contains(c[0]))
        {
            escape += source.GetWhile(2, OctDigits);
            int value = Convert.ToInt32(escape[1..], 8);
            if (value > 0xFF) throw source.Error($"octal escape value {escape} outside of range 0-0o377", CodePointLength(escape));
            return value;
        }
        if (c.Length == 1 && Digits.Contains(c[0])) throw source.Error($"bad escape {escape}", CodePointLength(escape));
        if (IsAsciiLetter(c)) throw source.Error($"bad escape {escape}", CodePointLength(escape));
        return CodePoint(c);
    }

    private static int EscapeValue(string escape) => escape switch
    {
        "\\a" => 7,
        "\\b" => 8,
        "\\f" => 12,
        "\\n" => 10,
        "\\r" => 13,
        "\\t" => 9,
        "\\v" => 11,
        _ => '\\',
    };

    private static int HexEscape(Tokenizer source, string escape, string c)
    {
        int digits = c switch { "x" => 2, "u" => 4, _ => 8 };
        escape += source.GetWhile(digits, HexDigits);
        if (escape.Length != digits + 2) throw source.Error($"incomplete escape {escape}", escape.Length);
        long value = long.Parse(escape[2..], NumberStyles.HexNumber, CultureInfo.InvariantCulture);
        // chr() refuses a value past U+10FFFF, which Python reports as a bad escape
        if (value > 0x10FFFF) throw source.Error($"bad escape {escape}", escape.Length);
        return (int)value;
    }

    /// <summary>An escape outside a class: the kind of item it is.</summary>
    private static Item Escape(Tokenizer source, string escape, State state)
    {
        switch (escape)
        {
            case "\\A" or "\\b" or "\\B" or "\\Z":
                return Item.At;
            case "\\d" or "\\D" or "\\s" or "\\S" or "\\w" or "\\W":
            case "\\a" or "\\f" or "\\n" or "\\r" or "\\t" or "\\v" or "\\\\":
                return Item.Other;
        }
        string c = escape[1..];
        if (c == "N") throw new PythonReUnchecked("\\N{...} (a character by its Unicode name)");
        if (c is "x" or "u" or "U")
        {
            HexEscape(source, escape, c);
            return Item.Other;
        }
        if (c == "0")
        {
            source.GetWhile(2, OctDigits);
            return Item.Other;
        }
        if (c.Length == 1 && Digits.Contains(c[0]))
        {
            // an octal escape or a group reference
            if (source.Next is { Length: 1 } second && Digits.Contains(second[0]))
            {
                escape += source.Get();
                if (OctDigits.Contains(escape[1]) && OctDigits.Contains(escape[2]) && source.Next is { Length: 1 } third && OctDigits.Contains(third[0]))
                {
                    escape += source.Get();
                    int value = Convert.ToInt32(escape[1..], 8);
                    if (value > 0xFF) throw source.Error($"octal escape value {escape} outside of range 0-0o377", escape.Length);
                    return Item.Other;
                }
            }
            int group = int.Parse(escape[1..], CultureInfo.InvariantCulture);
            if (group < state.Groups)
            {
                if (!state.Closed.Contains(group)) throw source.Error("cannot refer to an open group", escape.Length);
                return Item.Other;
            }
            throw source.Error($"invalid group reference {group.ToString(CultureInfo.InvariantCulture)}", escape.Length - 1);
        }
        if (IsAsciiLetter(c)) throw source.Error($"bad escape {escape}", CodePointLength(escape));
        return Item.Other;
    }

    private static void ParseSub(Tokenizer source, State state)
    {
        if (StackGuard.Exhausted) throw new PythonReUnchecked("groups " + StackGuard.TooDeep);
        while (true)
        {
            Parse(source, state);
            if (!source.Match("|")) break;
        }
    }

    private static void Parse(Tokenizer source, State state)
    {
        Item? last = null;
        while (true)
        {
            string? token = source.Next;
            if (token is null || token == "|" || token == ")") break;
            source.Get();
            if (token[0] == '\\' && token.Length > 1)
            {
                last = Escape(source, token, state);
            }
            else if (token == "[")
            {
                CharacterClass(source);
                last = Item.Other;
            }
            else if (token is "*" or "+" or "?" or "{")
            {
                if (!Repeat(source, token, last)) last = Item.Other; // a { that is a literal
                else last = Item.Repeat;
            }
            else if (token == ".")
            {
                last = Item.Other;
            }
            else if (token == "(")
            {
                if (Group(source, state)) last = Item.Other;
            }
            else if (token is "^" or "$")
            {
                last = Item.At;
            }
            else
            {
                last = Item.Other;
            }
        }
    }

    /// <summary>A quantifier after `last`; false when a { turns out to be a literal.</summary>
    private static bool Repeat(Tokenizer source, string token, Item? last)
    {
        int here = source.Tell;
        if (token == "{")
        {
            if (source.Next == "}") return false;
            var lo = new StringBuilder();
            var hi = new StringBuilder();
            while (source.Next is { Length: 1 } digit && Digits.Contains(digit[0])) lo.Append(source.Get());
            if (source.Match(","))
            {
                while (source.Next is { Length: 1 } digit && Digits.Contains(digit[0])) hi.Append(source.Get());
            }
            else hi.Append(lo);
            if (!source.Match("}"))
            {
                source.Seek(here);
                return false;
            }
            BigInteger min = 0;
            if (lo.Length > 0)
            {
                min = BigInteger.Parse(lo.ToString(), CultureInfo.InvariantCulture);
                // Python raises OverflowError here, not re.error
                if (min >= MaxRepeat) throw new PythonReError("the repetition number is too large");
            }
            if (hi.Length > 0)
            {
                var max = BigInteger.Parse(hi.ToString(), CultureInfo.InvariantCulture);
                if (max >= MaxRepeat) throw new PythonReError("the repetition number is too large");
                if (max < min) throw source.Error("min repeat greater than max repeat", source.Tell - here);
            }
        }
        if (last is null or Item.At) throw source.Error("nothing to repeat", source.Tell - here + 1);
        if (last == Item.Repeat) throw source.Error("multiple repeat", source.Tell - here + 1);
        if (!source.Match("?")) source.Match("+");
        return true;
    }

    private static void CharacterClass(Tokenizer source)
    {
        int here = source.Tell - 1;
        source.Match("^");
        int count = 0;
        while (true)
        {
            string? token = source.Get();
            if (token is null) throw source.Error("unterminated character set", source.Tell - here);
            if (token == "]" && count > 0) break;
            int? first = token[0] == '\\' && token.Length > 1 ? ClassEscape(source, token) : CodePoint(token);
            if (source.Match("-"))
            {
                string? that = source.Get();
                if (that is null) throw source.Error("unterminated character set", source.Tell - here);
                if (that == "]")
                {
                    count += 2;
                    break;
                }
                int? second = that[0] == '\\' && that.Length > 1 ? ClassEscape(source, that) : CodePoint(that);
                if (first is null || second is null || second < first)
                {
                    throw source.Error($"bad character range {token}-{that}", CodePointLength(token) + 1 + CodePointLength(that));
                }
            }
            count++;
        }
    }

    /// <summary>A group after "(": true when it adds an item (a comment adds none).</summary>
    private static bool Group(Tokenizer source, State state)
    {
        int start = source.Tell - 1;
        bool capture = true;
        string? name = null;
        if (source.Match("?"))
        {
            string? kind = source.Get();
            if (kind is null) throw source.Error("unexpected end of pattern");
            switch (kind)
            {
                case "P":
                    if (source.Match("<"))
                    {
                        name = source.GetUntil(">", "group name");
                        source.CheckGroupName(name, 1);
                        break;
                    }
                    if (source.Match("="))
                    {
                        string reference = source.GetUntil(")", "group name");
                        source.CheckGroupName(reference, 1);
                        if (!state.Names.TryGetValue(reference, out int id))
                        {
                            throw source.Error($"unknown group name {PyFormat.Str(reference)}", CodePointLength(reference) + 1);
                        }
                        if (!state.Closed.Contains(id)) throw source.Error("cannot refer to an open group", CodePointLength(reference) + 1);
                        return true;
                    }
                    string? next = source.Get();
                    if (next is null) throw source.Error("unexpected end of pattern");
                    throw source.Error("unknown extension ?P" + next, CodePointLength(next) + 2);
                case ":":
                case ">":
                    capture = false;
                    break;
                case "#":
                    while (true)
                    {
                        if (source.Next is null) throw source.Error("missing ), unterminated comment", source.Tell - start);
                        if (source.Get() == ")") break;
                    }
                    return false;
                case "=" or "!":
                    ParseSub(source, state);
                    if (!source.Match(")")) throw source.Error("missing ), unterminated subpattern", source.Tell - start);
                    return true;
                case "<":
                    string? after = source.Get();
                    if (after is null) throw source.Error("unexpected end of pattern");
                    if (after is not ("=" or "!")) throw source.Error("unknown extension ?<" + after, CodePointLength(after) + 2);
                    throw new PythonReUnchecked("a look-behind (?<" + after + "...)");
                case "(":
                    throw new PythonReUnchecked("a conditional (?(...)...)");
                case "i" or "L" or "m" or "s" or "x" or "a" or "u" or "-":
                    throw new PythonReUnchecked("inline flags (?" + kind + "...)");
                default:
                    throw source.Error("unknown extension ?" + kind, CodePointLength(kind) + 1);
            }
        }
        int? group = capture ? state.Open(source, name) : null;
        ParseSub(source, state);
        if (!source.Match(")")) throw source.Error("missing ), unterminated subpattern", source.Tell - start);
        if (group is int closed) state.Closed.Add(closed);
        return true;
    }
}
