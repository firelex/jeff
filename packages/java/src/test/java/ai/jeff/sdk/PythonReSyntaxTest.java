package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.Arrays;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** PythonReSyntax against CPython 3.13.14's re.compile: each pattern with what Python said (recorded with
 * `uv run python -W ignore` in the jeff-dev repository): OK when it compiles, else its re.error text (for the two
 * repetition limits, the text of the OverflowError Python raises there instead). UNCHECKED: a construct this check
 * refuses by name instead of deciding (Python accepts every one of these but the conditional). */
class PythonReSyntaxTest {
    private static final String OK = "<compiles>";
    private static final String UNCHECKED = "<not checked>";

    private static final String[][] CASES = {
            {"pg-[0-9]+-verdict-.+", OK},
            {".*", OK},
            {"[", "unterminated character set at position 0"},
            {"]", OK},
            {"a]", OK},
            {"[]a]", OK},
            {"[^]a]", OK},
            {"[a-", "unterminated character set at position 0"},
            {"[a-]", OK},
            {"[z-a]", "bad character range z-a at position 1"},
            {"[\\d-z]", "bad character range \\d-z at position 1"},
            {"[a-\\d]", "bad character range a-\\d at position 1"},
            {"[\\w]", OK},
            {"(", "missing ), unterminated subpattern at position 0"},
            {")", "unbalanced parenthesis at position 0"},
            {"a)", "unbalanced parenthesis at position 1"},
            {"(a", "missing ), unterminated subpattern at position 0"},
            {"(?", "unexpected end of pattern at position 2"},
            {"(?P", "unexpected end of pattern at position 3"},
            {"(?P<", "missing group name at position 4"},
            {"(?P<>a)", "missing group name at position 4"},
            {"(?P<1a>x)", "bad character in group name '1a' at position 4"},
            {"(?P<a>x)(?P<a>y)", "redefinition of group name 'a' as group 2; was group 1 at position 12"},
            {"(?P<a_1>x)(?P=a_1)", OK},
            {"(?P=a)", "unknown group name 'a' at position 4"},
            {"(?P<a>(?P=a))", "cannot refer to an open group at position 10"},
            {"(?Px)", "unknown extension ?Px at position 1"},
            {"(?:a)", OK},
            {"(?>a+)", OK},
            {"(?=a)b", OK},
            {"(?!a)b", OK},
            {"(?#comment)a", OK},
            {"(?#unterminated", "missing ), unterminated comment at position 0"},
            {"(?<=a)b", UNCHECKED},
            {"(?<!a)b", UNCHECKED},
            {"(?<x)", "unknown extension ?<x at position 1"},
            {"(?(1)a|b)", UNCHECKED},
            {"(?i)abc", UNCHECKED},
            {"(?x: a )", UNCHECKED},
            {"(?-i:a)", UNCHECKED},
            {"(?z)", "unknown extension ?z at position 1"},
            {"*", "nothing to repeat at position 0"},
            {"+a", "nothing to repeat at position 0"},
            {"?", "nothing to repeat at position 0"},
            {"a**", "multiple repeat at position 2"},
            {"a*?", OK},
            {"a*+", OK},
            {"a++", OK},
            {"a?+", OK},
            {"a{2}", OK},
            {"a{2,}", OK},
            {"a{,3}", OK},
            {"a{,}", OK},
            {"a{3,2}", "min repeat greater than max repeat at position 2"},
            {"a{}", OK},
            {"a{x}", OK},
            {"{", OK},
            {"a{2", OK},
            {"a{4294967295}", "the repetition number is too large"},
            {"a{4294967294}", OK},
            {"a{1,4294967295}", "the repetition number is too large"},
            {"^*", "nothing to repeat at position 1"},
            {"$+", "nothing to repeat at position 1"},
            {"\\A*", "nothing to repeat at position 2"},
            {"\\b+", "nothing to repeat at position 2"},
            {"(?:)*", OK},
            {"(?#c)*", "nothing to repeat at position 5"},
            {"a(?#c)*", OK},
            {"\\", "bad escape (end of pattern) at position 0"},
            {"a\\", "bad escape (end of pattern) at position 1"},
            {"\\d\\D\\s\\S\\w\\W", OK},
            {"\\q", "bad escape \\q at position 0"},
            {"\\_", OK},
            {"\\-", OK},
            {"\\e", "bad escape \\e at position 0"},
            {"\\p{L}", "bad escape \\p at position 0"},
            {"\\Q", "bad escape \\Q at position 0"},
            {"\\x4", "incomplete escape \\x4 at position 0"},
            {"\\x41", OK},
            {"\\xg", "incomplete escape \\x at position 0"},
            {"\\u004", "incomplete escape \\u004 at position 0"},
            {"\\u0041", OK},
            {"\\U0010ffff", OK},
            {"\\U00110000", "bad escape \\U00110000 at position 0"},
            {"\\U0001", "incomplete escape \\U0001 at position 0"},
            {"\\0", OK},
            {"\\07", OK},
            {"\\077", OK},
            {"\\0777", OK},
            {"\\400", "octal escape value \\400 outside of range 0-0o377 at position 0"},
            {"\\377", OK},
            {"\\1", "invalid group reference 1 at position 1"},
            {"(a)\\1", OK},
            {"(a\\1)", "cannot refer to an open group at position 2"},
            {"(a)\\2", "invalid group reference 2 at position 4"},
            {"(a)\\10", "invalid group reference 10 at position 4"},
            {"\\8", "invalid group reference 8 at position 1"},
            {"[\\8]", "bad escape \\8 at position 1"},
            {"[\\0]", OK},
            {"[\\400]", "octal escape value \\400 outside of range 0-0o377 at position 1"},
            {"[\\377]", OK},
            {"[\\A]", "bad escape \\A at position 1"},
            {"[\\b]", OK},
            {"[\\x4]", "incomplete escape \\x4 at position 1"},
            {"[\\U00110000]", "bad escape \\U00110000 at position 1"},
            {"[\\q]", "bad escape \\q at position 1"},
            {"[\\-]", OK},
            {"\\N{DIGIT ONE}", UNCHECKED},
            {"[\\N{DIGIT ONE}]", UNCHECKED},
            {"a|b|", OK},
            {"|", OK},
            {"a|*", "nothing to repeat at position 2"},
            {"(?P<\u00e9>x)", UNCHECKED},
            {"\u00e9+", OK},
            {"[\u00e9-a]", "bad character range \u00e9-a at position 1"},
            {"\ud83d\ude00*", OK},
            {"[\ud83d\ude00-a]", "bad character range \ud83d\ude00-a at position 1"},
            {"a\nb(", "missing ), unterminated subpattern at position 3 (line 2, column 2)"},
            {"a\n[", "unterminated character set at position 2 (line 2, column 1)"},
            {"x\ny\n\\", "bad escape (end of pattern) at position 4 (line 3, column 1)"},
            {"\\\ud83d\ude00", OK},
            {"(?=a)*", OK},
            {"(?!)*", OK},
            {"\\b\\B\\Z", OK},
            {"[a-z0-9_.-]+", OK},
            {"^[a-z][a-z0-9_]*$", OK},
            {"(?P<name", "missing >, unterminated name at position 4"},
            {"(?P=name", "missing ), unterminated name at position 4"},
            {"(?P=)", "missing group name at position 4"},
            {"(?P<a>x)(?P=b)", "unknown group name 'b' at position 12"},
            {"a{2}{3}", "multiple repeat at position 4"},
            {"a{2}*", "multiple repeat at position 4"},
            {"(?:a{2})*", OK},
            {"[[]", OK},
            {"[a&&b]", OK},
            {"[a--b]", "bad character range a-- at position 1"},
            {"\\Z+", "nothing to repeat at position 2"},
            {".{1,2}?", OK},
            {"(?=x", "missing ), unterminated subpattern at position 0"},
            {"(?:x", "missing ), unterminated subpattern at position 0"},
            {"(x", "missing ), unterminated subpattern at position 0"},
            {"((((x))))", OK},
            {"a{08}", OK},
            {"a{0x2}", OK},
            {"a{ 2}", OK},
    };

    static Stream<Arguments> cases() {
        return Arrays.stream(CASES).map(row -> Arguments.of(PyFormat.str(row[0]), row[0], row[1]));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void aPatternIsJudgedAsPythonJudgesIt(String shown, String pattern, String python) {
        if (python.equals(OK)) assertDoesNotThrow(() -> PythonReSyntax.check(pattern));
        else if (python.equals(UNCHECKED)) assertThrows(PythonReSyntax.Unchecked.class, () -> PythonReSyntax.check(pattern));
        else assertEquals(python, assertThrows(PythonReSyntax.Refused.class, () -> PythonReSyntax.check(pattern)).getMessage());
    }

    @Test
    void groupsNestedPastTheLimitAreRefusedByNameOnAnyStack() throws InterruptedException {
        String fine = "(".repeat(PythonReSyntax.MAX_NESTING) + ")".repeat(PythonReSyntax.MAX_NESTING);
        String deep = "(?:".repeat(PythonReSyntax.MAX_NESTING + 1) + ")".repeat(PythonReSyntax.MAX_NESTING + 1);
        Throwable[] failure = new Throwable[1];
        Thread small = Thread.ofPlatform().stackSize(256 * 1024).unstarted(() -> {
            try {
                PythonReSyntax.check(fine);
                String message = assertThrows(PythonReSyntax.Unchecked.class, () -> PythonReSyntax.check(deep)).getMessage();
                assertEquals("groups nested more than 400 deep (Python's own parser runs out of recursion near 490)", message);
            } catch (Throwable thrown) {
                failure[0] = thrown;
            }
        });
        small.start();
        small.join();
        if (failure[0] != null) throw new AssertionError(failure[0]);
    }
}
