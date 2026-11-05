package ai.jeff.sdk;

import java.math.BigInteger;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/** Whether Python 3.13's re.compile accepts a pattern, decided by a port of the syntax checks of CPython's
 * re/_parser.py (and the one compile-time check this SDK can meet, the repetition limit), working on code points as
 * Python does. It never builds a java.util.regex.Pattern: Java accepts some patterns Python refuses (\p{L}, \e, \Q..\E,
 * (?&lt;name&gt;x), \k&lt;n&gt;) and refuses some Python accepts (\_, (?P&lt;name&gt;x), a{,3}).
 *
 * <p>Followed exactly: literals and every escape (the hex escapes backslash x, u and U, octal, the categories \d \D \s \S \w \W, the anchors
 * \A \b \B \Z, unknown ASCII-letter escapes refused), character classes and ranges, the quantifiers * + ? {m} {m,}
 * {,n} {m,n} with their lazy (?) and possessive (+) forms, ., ^, $, alternation, capturing groups, named groups
 * (?P&lt;name&gt;...) with ASCII names, (?:...), (?&gt;...), look-ahead (?=...) and (?!...), comments (?#...),
 * references to groups (\1, (?P=name)), and every error those raise, with Python's message and position.
 *
 * <p>Not followed, and refused with {@link Unchecked}: conditionals (?(1)...); look-behind (?&lt;=...), (?&lt;!...),
 * whose fixed-width rule needs the widths of everything inside; inline flags (?i), (?x:...), which change how the
 * rest is read; group names that are not ASCII (Python checks them against Unicode's identifier tables); \N{name},
 * which needs Unicode's character name table; and groups nested more than {@link #MAX_NESTING} deep (Python's own
 * parser runs out of recursion near 490 levels, raising RecursionError rather than re.error). */
final class PythonReSyntax {
    /** _sre.MAXREPEAT. */
    private static final BigInteger MAX_REPEAT = BigInteger.valueOf(4294967295L);
    /** The deepest groups inside one another this check follows. */
    static final int MAX_NESTING = 400;

    private static final String DIGITS = "0123456789";
    private static final String OCT_DIGITS = "01234567";
    private static final String HEX_DIGITS = "0123456789abcdefABCDEF";

    /** Python's re.compile refuses the pattern; the message is Python's re.error text, position included
     * ("unterminated character set at position 0"). */
    static final class Refused extends RuntimeException {
        Refused(String message) {
            super(message);
        }
    }

    /** The pattern uses a construct this check does not follow (named in the message), so whether Python accepts it
     * is not decided here. */
    static final class Unchecked extends RuntimeException {
        Unchecked(String construct) {
            super(construct);
        }
    }

    private enum Item { AT, REPEAT, OTHER }

    private PythonReSyntax() {}

    /** Returns when Python accepts `pattern`; throws Refused with Python's message when it refuses it, Unchecked when
     * the pattern uses a construct this check does not follow. Runs on a large stack (see Nesting) when the pattern
     * could nest groups deeper than Nesting.INLINE_DEPTH. */
    static void check(String pattern) {
        long opened = pattern.chars().filter(character -> character == '(').count();
        Nesting.run(opened <= Nesting.INLINE_DEPTH, () -> {
            Tokenizer source = new Tokenizer(pattern);
            State state = new State();
            parseSub(source, state, 0);
            if (source.next != null) throw source.error("unbalanced parenthesis", 0);
            return null;
        });
    }

    private static final class State {
        int groups = 1; // Python's state.groups: the groups opened so far, plus group 0
        final Set<Integer> closed = new HashSet<>();
        final Map<String, Integer> names = new HashMap<>();

        int open(Tokenizer source, String name) {
            int id = groups++;
            if (name == null) return id;
            Integer earlier = names.get(name);
            if (earlier != null) {
                throw source.error("redefinition of group name " + PyFormat.str(name) + " as group " + id + "; was group "
                    + earlier, length(name) + 1);
            }
            names.put(name, id);
            return id;
        }
    }

    private static final class Tokenizer {
        private final int[] points;
        private final boolean multiline;
        private int index;
        /** The next token: one code point, or a backslash and the code point after it; null at the end. */
        String next;

        Tokenizer(String pattern) {
            points = pattern.codePoints().toArray();
            multiline = pattern.indexOf('\n') >= 0;
            advance();
        }

        private void advance() {
            int at = index;
            if (at >= points.length) {
                next = null;
                return;
            }
            StringBuilder token = new StringBuilder().appendCodePoint(points[at]);
            if (points[at] == '\\') {
                at++;
                if (at >= points.length) throw errorAt("bad escape (end of pattern)", points.length - 1);
                token.appendCodePoint(points[at]);
            }
            index = at + 1;
            next = token.toString();
        }

        boolean match(String token) {
            if (!token.equals(next)) return false;
            advance();
            return true;
        }

        String get() {
            String token = next;
            advance();
            return token;
        }

        /** Python's tell(): the position, in code points, of the next token. */
        int tell() {
            return index - (next == null ? 0 : length(next));
        }

        void seek(int position) {
            index = position;
            advance();
        }

        /** Up to `count` tokens, each one character of `charset`. */
        String getWhile(int count, String charset) {
            StringBuilder result = new StringBuilder();
            for (int done = 0; done < count; done++) {
                if (!in(next, charset)) break;
                result.append(next);
                advance();
            }
            return result.toString();
        }

        /** Python's getuntil: the tokens up to `terminator`, which is consumed. */
        String getUntil(String terminator, String name) {
            StringBuilder result = new StringBuilder();
            while (true) {
                String token = next;
                advance();
                if (token == null) {
                    if (result.isEmpty()) throw error("missing " + name, 0);
                    throw error("missing " + terminator + ", unterminated name", length(result.toString()));
                }
                if (token.equals(terminator)) {
                    if (result.isEmpty()) throw error("missing " + name, 1);
                    return result.toString();
                }
                result.append(token);
            }
        }

        /** Python's checkgroupname for a str pattern: the name must be an identifier. Only ASCII names are decided
         * here (Python's isidentifier follows Unicode's identifier tables for the others). */
        void checkGroupName(String name, int offset) {
            if (name.chars().anyMatch(character -> character > 0x7f)) {
                throw new Unchecked("the group name " + PyFormat.str(name) + ", which is not ASCII");
            }
            boolean identifier = !name.isEmpty() && (asciiLetter(name.charAt(0)) || name.charAt(0) == '_')
                && name.chars().allMatch(character -> asciiLetter(character) || in(character, DIGITS) || character == '_');
            if (!identifier) throw error("bad character in group name " + PyFormat.str(name), length(name) + offset);
        }

        Refused error(String message, int offset) {
            return errorAt(message, tell() - offset);
        }

        /** re.error's text: the message, " at position N", and the line and column when the pattern has a newline. */
        private Refused errorAt(String message, int position) {
            String text = message + " at position " + position;
            if (multiline) {
                int line = 1;
                int lastNewline = -1;
                for (int at = 0; at < position && at < points.length; at++) {
                    if (points[at] != '\n') continue;
                    line++;
                    lastNewline = at;
                }
                text += " (line " + line + ", column " + (position - lastNewline) + ")";
            }
            return new Refused(text);
        }
    }

    private static int length(String text) {
        return text.codePointCount(0, text.length());
    }

    private static boolean asciiLetter(int character) {
        return (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z');
    }

    private static boolean in(int character, String charset) {
        return charset.indexOf(character) >= 0;
    }

    /** Whether a token is one character of `charset` (Python's `token in frozenset(charset)`; null is in none). */
    private static boolean in(String token, String charset) {
        return token != null && token.length() == 1 && in(token.charAt(0), charset);
    }

    /** The code point a one-code-point token holds. */
    private static int codePoint(String token) {
        return token.codePointAt(0);
    }

    private static boolean isEscape(String token) {
        return token.codePointAt(0) == '\\' && length(token) == 2;
    }

    private static int escapeValue(String escape) {
        return switch (escape) {
            case "\\a" -> 7;
            case "\\b" -> 8;
            case "\\f" -> 12;
            case "\\n" -> 10;
            case "\\r" -> 13;
            case "\\t" -> 9;
            case "\\v" -> 11;
            default -> '\\';
        };
    }

    private static int hexEscape(Tokenizer source, String escape, String c) {
        int digits = switch (c) {
            case "x" -> 2;
            case "u" -> 4;
            default -> 8;
        };
        escape += source.getWhile(digits, HEX_DIGITS);
        if (escape.length() != digits + 2) throw source.error("incomplete escape " + escape, escape.length());
        long value = Long.parseLong(escape.substring(2), 16);
        // chr() refuses a value past U+10FFFF, which Python reports as a bad escape
        if (value > 0x10FFFF) throw source.error("bad escape " + escape, escape.length());
        return (int) value;
    }

    /** An escape inside a class: its code point, or null for a category (\d and the like). */
    private static Integer classEscape(Tokenizer source, String escape) {
        switch (escape) {
            case "\\a", "\\b", "\\f", "\\n", "\\r", "\\t", "\\v", "\\\\" -> {
                return escapeValue(escape);
            }
            case "\\d", "\\D", "\\s", "\\S", "\\w", "\\W" -> {
                return null;
            }
            default -> { }
        }
        String c = escape.substring(1);
        if (c.equals("N")) throw new Unchecked("\\N{...} (a character by its Unicode name)");
        if (c.equals("x") || c.equals("u") || c.equals("U")) return hexEscape(source, escape, c);
        if (in(c, OCT_DIGITS)) {
            escape += source.getWhile(2, OCT_DIGITS);
            int value = Integer.parseInt(escape.substring(1), 8);
            if (value > 0xFF) {
                throw source.error("octal escape value " + escape + " outside of range 0-0o377", length(escape));
            }
            return value;
        }
        if (in(c, DIGITS) || (c.length() == 1 && asciiLetter(c.charAt(0)))) {
            throw source.error("bad escape " + escape, length(escape));
        }
        return codePoint(c);
    }

    /** An escape outside a class: the kind of item it is. */
    private static Item escape(Tokenizer source, String escape, State state) {
        switch (escape) {
            case "\\A", "\\b", "\\B", "\\Z" -> {
                return Item.AT;
            }
            case "\\d", "\\D", "\\s", "\\S", "\\w", "\\W", "\\a", "\\f", "\\n", "\\r", "\\t", "\\v", "\\\\" -> {
                return Item.OTHER;
            }
            default -> { }
        }
        String c = escape.substring(1);
        if (c.equals("N")) throw new Unchecked("\\N{...} (a character by its Unicode name)");
        if (c.equals("x") || c.equals("u") || c.equals("U")) {
            hexEscape(source, escape, c);
            return Item.OTHER;
        }
        if (c.equals("0")) {
            source.getWhile(2, OCT_DIGITS);
            return Item.OTHER;
        }
        if (in(c, DIGITS)) {
            // an octal escape or a group reference
            if (in(source.next, DIGITS)) {
                escape += source.get();
                if (in(escape.charAt(1), OCT_DIGITS) && in(escape.charAt(2), OCT_DIGITS) && in(source.next, OCT_DIGITS)) {
                    escape += source.get();
                    int value = Integer.parseInt(escape.substring(1), 8);
                    if (value > 0xFF) {
                        throw source.error("octal escape value " + escape + " outside of range 0-0o377", escape.length());
                    }
                    return Item.OTHER;
                }
            }
            int group = Integer.parseInt(escape.substring(1));
            if (group < state.groups) {
                if (!state.closed.contains(group)) throw source.error("cannot refer to an open group", escape.length());
                return Item.OTHER;
            }
            throw source.error("invalid group reference " + group, escape.length() - 1);
        }
        if (c.length() == 1 && asciiLetter(c.charAt(0))) throw source.error("bad escape " + escape, length(escape));
        return Item.OTHER;
    }

    private static void parseSub(Tokenizer source, State state, int nesting) {
        if (nesting > MAX_NESTING) {
            throw new Unchecked("groups nested more than " + MAX_NESTING + " deep (Python's own parser runs out of "
                + "recursion near 490)");
        }
        while (true) {
            parse(source, state, nesting);
            if (!source.match("|")) break;
        }
    }

    private static void parse(Tokenizer source, State state, int nesting) {
        Item last = null;
        while (true) {
            String token = source.next;
            if (token == null || token.equals("|") || token.equals(")")) break;
            source.get();
            if (isEscape(token)) {
                last = escape(source, token, state);
            } else if (token.equals("[")) {
                characterClass(source);
                last = Item.OTHER;
            } else if (token.equals("*") || token.equals("+") || token.equals("?") || token.equals("{")) {
                last = repeat(source, token, last) ? Item.REPEAT : Item.OTHER; // OTHER: a { that is a literal
            } else if (token.equals("(")) {
                if (group(source, state, nesting)) last = Item.OTHER;
            } else if (token.equals("^") || token.equals("$")) {
                last = Item.AT;
            } else {
                last = Item.OTHER;
            }
        }
    }

    /** A quantifier after `last`; false when a { turns out to be a literal. */
    private static boolean repeat(Tokenizer source, String token, Item last) {
        int here = source.tell();
        if (token.equals("{")) {
            if ("}".equals(source.next)) return false;
            StringBuilder lo = new StringBuilder();
            StringBuilder hi = new StringBuilder();
            while (in(source.next, DIGITS)) lo.append(source.get());
            if (source.match(",")) {
                while (in(source.next, DIGITS)) hi.append(source.get());
            } else {
                hi.append(lo);
            }
            if (!source.match("}")) {
                source.seek(here);
                return false;
            }
            BigInteger min = BigInteger.ZERO;
            if (!lo.isEmpty()) {
                min = new BigInteger(lo.toString());
                // Python raises OverflowError here, not re.error
                if (min.compareTo(MAX_REPEAT) >= 0) throw new Refused("the repetition number is too large");
            }
            if (!hi.isEmpty()) {
                BigInteger max = new BigInteger(hi.toString());
                if (max.compareTo(MAX_REPEAT) >= 0) throw new Refused("the repetition number is too large");
                if (max.compareTo(min) < 0) throw source.error("min repeat greater than max repeat", source.tell() - here);
            }
        }
        if (last == null || last == Item.AT) throw source.error("nothing to repeat", source.tell() - here + 1);
        if (last == Item.REPEAT) throw source.error("multiple repeat", source.tell() - here + 1);
        if (!source.match("?")) source.match("+");
        return true;
    }

    private static void characterClass(Tokenizer source) {
        int here = source.tell() - 1;
        source.match("^");
        int count = 0;
        while (true) {
            String token = source.get();
            if (token == null) throw source.error("unterminated character set", source.tell() - here);
            if (token.equals("]") && count > 0) break;
            Integer first = isEscape(token) ? classEscape(source, token) : Integer.valueOf(codePoint(token));
            if (source.match("-")) {
                String that = source.get();
                if (that == null) throw source.error("unterminated character set", source.tell() - here);
                if (that.equals("]")) break;
                Integer second = isEscape(that) ? classEscape(source, that) : Integer.valueOf(codePoint(that));
                if (first == null || second == null || second < first) {
                    throw source.error("bad character range " + token + "-" + that, length(token) + 1 + length(that));
                }
            }
            count++;
        }
    }

    /** A group after "(": true when it adds an item (a comment adds none). */
    private static boolean group(Tokenizer source, State state, int nesting) {
        int start = source.tell() - 1;
        boolean capture = true;
        String name = null;
        if (source.match("?")) {
            String kind = source.get();
            if (kind == null) throw source.error("unexpected end of pattern", 0);
            switch (kind) {
                case "P" -> {
                    if (source.match("<")) {
                        name = source.getUntil(">", "group name");
                        source.checkGroupName(name, 1);
                    } else if (source.match("=")) {
                        String reference = source.getUntil(")", "group name");
                        source.checkGroupName(reference, 1);
                        Integer id = state.names.get(reference);
                        if (id == null) {
                            throw source.error("unknown group name " + PyFormat.str(reference), length(reference) + 1);
                        }
                        if (!state.closed.contains(id)) {
                            throw source.error("cannot refer to an open group", length(reference) + 1);
                        }
                        return true;
                    } else {
                        String next = source.get();
                        if (next == null) throw source.error("unexpected end of pattern", 0);
                        throw source.error("unknown extension ?P" + next, length(next) + 2);
                    }
                }
                case ":", ">" -> capture = false;
                case "#" -> {
                    while (true) {
                        if (source.next == null) throw source.error("missing ), unterminated comment", source.tell() - start);
                        if (source.get().equals(")")) break;
                    }
                    return false;
                }
                case "=", "!" -> {
                    parseSub(source, state, nesting + 1);
                    if (!source.match(")")) throw source.error("missing ), unterminated subpattern", source.tell() - start);
                    return true;
                }
                case "<" -> {
                    String after = source.get();
                    if (after == null) throw source.error("unexpected end of pattern", 0);
                    if (!after.equals("=") && !after.equals("!")) {
                        throw source.error("unknown extension ?<" + after, length(after) + 2);
                    }
                    throw new Unchecked("a look-behind (?<" + after + "...)");
                }
                case "(" -> throw new Unchecked("a conditional (?(...)...)");
                case "i", "L", "m", "s", "x", "a", "u", "-" -> throw new Unchecked("inline flags (?" + kind + "...)");
                default -> throw source.error("unknown extension ?" + kind, length(kind) + 1);
            }
        }
        Integer opened = capture ? state.open(source, name) : null;
        parseSub(source, state, nesting + 1);
        if (!source.match(")")) throw source.error("missing ), unterminated subpattern", source.tell() - start);
        if (opened != null) state.closed.add(opened);
        return true;
    }
}
