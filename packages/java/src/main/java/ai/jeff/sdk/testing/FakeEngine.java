package ai.jeff.sdk.testing;

import ai.jeff.sdk.BaseFormat;
import ai.jeff.sdk.Engine;
import ai.jeff.sdk.EngineResult;
import ai.jeff.sdk.JeffError;
import ai.jeff.sdk.Row;
import java.util.AbstractMap;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;

/** An engine whose answers come from a function you supply (port of jeff.testing.FakeEngine): for tests of code
 * that uses the SDK, with no model. Give it to {@link ai.jeff.sdk.Jeff#create}. */
public final class FakeEngine implements Engine {
    /** Your function that answers each row. */
    @FunctionalInterface
    public interface Answers {
        /** The probabilities for one row.
         *
         * @param adapterId the adapter the row was sent to
         * @param row the row, with its state and question
         * @return one probability per option, in option order */
        double[] answer(String adapterId, Row row);
    }

    private final Answers answers;
    private final Set<String> served;
    private final BaseFormat baseFormat;
    private final List<Map.Entry<String, List<Row>>> calls = new CopyOnWriteArrayList<>();

    /** A fake engine on a bfloat16 base.
     *
     * @param answers your function that answers each row
     * @param installed the adapter ids it serves */
    public FakeEngine(Answers answers, Collection<String> installed) {
        this(answers, installed, BaseFormat.bf16());
    }

    /** A fake engine on the base format given.
     *
     * @param answers your function that answers each row
     * @param installed the adapter ids it serves
     * @param baseFormat the base's weight format it reports (pass {@code new BaseFormat("int4-g64", "<format id>")} to
     *     act as an INT4 base)
     * @throws JeffError.EngineError when the base format is malformed, as a real engine's base folder would be
     *     refused */
    public FakeEngine(Answers answers, Collection<String> installed, BaseFormat baseFormat) {
        BaseFormat.formatIdOf(baseFormat);
        this.answers = answers;
        this.served = Set.copyOf(installed);
        this.baseFormat = baseFormat;
    }

    /** The base format it was made with.
     *
     * @return the format */
    @Override
    public BaseFormat baseFormat() {
        return baseFormat;
    }

    /** The adapters it was made with.
     *
     * @param refresh ignored: the list never changes
     * @return the adapter ids */
    @Override
    public Set<String> installed(boolean refresh) {
        return served;
    }

    /** Answers each row with your function and records the call. The timing is {@code {"path": "fake",
     * "model_ms": 0.0}} and the base format's fields; each row's token count is one per word of its JSON, as
     * Python's {@code fake_tokens} counts them.
     *
     * @param adapterId the adapter
     * @param rows the rows
     * @return your function's answers
     * @throws JeffError.NotInstalled when it does not serve the adapter (the call is not recorded) */
    @Override
    public EngineResult predict(String adapterId, List<Row> rows) {
        if (!served.contains(adapterId)) throw JeffError.notInstalled(adapterId, "FakeEngine has not loaded it");
        calls.add(new AbstractMap.SimpleImmutableEntry<>(adapterId, List.copyOf(rows)));
        List<double[]> probabilities = new ArrayList<>();
        List<Long> tokens = new ArrayList<>();
        for (Row row : rows) {
            probabilities.add(answers.answer(adapterId, row));
            tokens.add((long) fakeTokens(row));
        }
        LinkedHashMap<String, Object> timing = new LinkedHashMap<>();
        timing.put("path", "fake");
        timing.put("model_ms", 0.0);
        timing.putAll(baseFormat.toMap());
        return new EngineResult(probabilities, timing, tokens);
    }

    /** Every predict made, in order.
     *
     * @return each predict's adapter and rows */
    public List<Map.Entry<String, List<Row>>> calls() {
        return List.copyOf(calls);
    }

    /** A stand-in for a row's prompt length, as Python's fake_tokens: one token per whitespace-separated word of the
     * row's JSON as Python's json.dumps writes it (", " and ": " separators). json.dumps escapes every whitespace
     * character inside a string except the space, and escaping turns one character into one or more characters that
     * are not spaces; so the words are the same as in the text written with each string's characters as they are, and
     * that is the text counted here. */
    static int fakeTokens(Row row) {
        int count = 0;
        for (String word : dumps(row.toWire()).split(" ")) if (!word.isEmpty()) count += 1;
        return count;
    }

    private static String dumps(Object value) {
        if (value instanceof List<?> list) {
            List<String> items = new ArrayList<>();
            for (Object item : list) items.add(dumps(item));
            return "[" + String.join(", ", items) + "]";
        }
        if (value instanceof Map<?, ?> map) {
            List<String> items = new ArrayList<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) items.add(dumps(entry.getKey()) + ": " + dumps(entry.getValue()));
            return "{" + String.join(", ", items) + "}";
        }
        // a string, number, boolean or null: no JSON form of these holds a space a string's own text does not
        return value instanceof String text ? "\"" + text + "\"" : String.valueOf(value);
    }
}
