package ai.jeff.sdk;

import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.JsonGenerator;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.core.StreamWriteConstraints;
import com.fasterxml.jackson.core.json.JsonReadFeature;
import com.fasterxml.jackson.core.json.JsonWriteFeature;
import java.io.IOException;
import java.io.StringWriter;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** JSON as Python's json module reads and writes it: a whole number as a Long when it fits in 64 signed bits, a
 * BigInteger when it does not (json.loads has no integer size limit); objects as LinkedHashMap in document order (a
 * repeated key keeps its first place and its last value); NaN and Infinity allowed on read and written as the bare
 * words NaN/Infinity/-Infinity (not quoted strings); a finite double written with Python's own repr() digits
 * (PyFormat.floatRepr), not Jackson's. */
final class Json {
    /** Nesting deeper than Python's json goes is refused by name, reading and writing (Nesting.PYTHON_DEPTH). */
    private static final JsonFactory FACTORY = JsonFactory.builder()
        .enable(JsonReadFeature.ALLOW_NON_NUMERIC_NUMBERS)
        .disable(JsonWriteFeature.WRITE_NAN_AS_STRINGS)
        .streamReadConstraints(StreamReadConstraints.builder().maxNestingDepth(Nesting.PYTHON_DEPTH).build())
        .streamWriteConstraints(StreamWriteConstraints.builder().maxNestingDepth(Nesting.PYTHON_DEPTH).build())
        .build();

    /** A value json.loads would refuse, found while reading (the parser's own failures are JsonProcessingException). */
    private static final class Refused extends Exception {
        Refused(String message) {
            super(message);
        }
    }

    private Json() {}

    /** JSON text as Python's json.loads reads it; deep text is read on a large-stack thread (see Nesting). */
    public static Object parse(String text) {
        return Nesting.run(Nesting.shallowJson(text), () -> {
            try (JsonParser parser = FACTORY.createParser(text)) {
                if (parser.nextToken() == null) throw new Refused("No content to map due to end-of-input");
                // as ObjectMapper.readValue did: one value, anything after it not read
                return value(parser);
            } catch (JsonProcessingException error) {
                throw new IllegalArgumentException("Not JSON: " + error.getOriginalMessage(), error);
            } catch (Refused error) {
                throw new IllegalArgumentException("Not JSON: " + error.getMessage(), error);
            } catch (IOException error) {
                throw new UncheckedIOException("Reading JSON from a string failed", error);
            }
        });
    }

    /** The value at the parser's current token, as Json's own types (Long/BigInteger/Double/String/Boolean/null/
     * LinkedHashMap/ArrayList): a small whole number a Long, one past 64 bits a BigInteger. */
    private static Object value(JsonParser parser) throws IOException, Refused {
        JsonToken token = parser.currentToken();
        return switch (token) {
            case VALUE_NULL -> null;
            case VALUE_TRUE -> Boolean.TRUE;
            case VALUE_FALSE -> Boolean.FALSE;
            case VALUE_STRING -> parser.getText();
            case VALUE_NUMBER_INT -> parser.getNumberType() == JsonParser.NumberType.BIG_INTEGER
                ? parser.getBigIntegerValue() : parser.getLongValue();
            case VALUE_NUMBER_FLOAT -> {
                // ALLOW_NON_NUMERIC_NUMBERS (needed for Python's NaN/Infinity/-Infinity) also accepts "+Infinity",
                // which json.loads refuses ("Expecting value"); no feature flag accepts the one without the other
                if ("+Infinity".equals(parser.getText())) throw new Refused("+Infinity is not valid JSON (a leading '+' is not allowed)");
                yield parser.getDoubleValue();
            }
            case START_OBJECT -> {
                Map<String, Object> out = new LinkedHashMap<>();
                while (parser.nextToken() != JsonToken.END_OBJECT) {
                    String key = parser.currentName();
                    parser.nextToken();
                    out.put(key, value(parser));
                }
                yield out;
            }
            case START_ARRAY -> {
                List<Object> out = new ArrayList<>();
                while (parser.nextToken() != JsonToken.END_ARRAY) out.add(value(parser));
                yield out;
            }
            default -> throw new Refused("unexpected token " + token + " reading a JSON value");
        };
    }

    /** A value as json.dumps writes it; a deep value is written on a large-stack thread (see Nesting). A Double is
     * written as Python writes a float: NaN/Infinity/-Infinity as those bare words, any finite value with
     * PyFormat.floatRepr's digits (Jackson's Double.toString-shaped default disagrees with Python outside roughly
     * [1e-3, 1e7): it writes Double.MIN_VALUE as 4.9E-324, Python as 5e-324). */
    public static String write(Object value) {
        return Nesting.run(Nesting.shallow(value), () -> {
            StringWriter out = new StringWriter();
            try (JsonGenerator generator = FACTORY.createGenerator(out)) {
                write(value, generator);
            } catch (JsonProcessingException error) {
                throw new IllegalArgumentException("Cannot write as JSON: " + error.getOriginalMessage(), error);
            } catch (IOException error) {
                throw new UncheckedIOException("Writing JSON to a string failed", error);
            }
            return out.toString();
        });
    }

    private static void write(Object value, JsonGenerator generator) throws IOException {
        switch (value) {
            case null -> generator.writeNull();
            case String text -> generator.writeString(text);
            case Boolean flag -> generator.writeBoolean(flag);
            case Long number -> generator.writeNumber(number);
            case Integer number -> generator.writeNumber(number);
            case Short number -> generator.writeNumber(number);
            case Byte number -> generator.writeNumber(number);
            case BigInteger number -> generator.writeNumber(number);
            case BigDecimal number -> generator.writeNumber(number);
            case Float number -> generator.writeNumber(number);
            case Double number -> {
                double exact = number;
                if (Double.isNaN(exact)) generator.writeRawValue("NaN");
                else if (Double.isInfinite(exact)) generator.writeRawValue(exact > 0 ? "Infinity" : "-Infinity");
                else generator.writeRawValue(PyFormat.floatRepr(exact));
            }
            case byte[] bytes -> generator.writeBinary(bytes);
            case Map<?, ?> map -> {
                generator.writeStartObject();
                for (Map.Entry<?, ?> entry : map.entrySet()) {
                    if (entry.getKey() == null) {
                        throw new IllegalArgumentException("Cannot write as JSON: a mapping has a null key, which JSON cannot carry");
                    }
                    generator.writeFieldName(entry.getKey().toString());
                    write(entry.getValue(), generator);
                }
                generator.writeEndObject();
            }
            case Iterable<?> items -> {
                generator.writeStartArray();
                for (Object item : items) write(item, generator);
                generator.writeEndArray();
            }
            case Object[] items -> {
                generator.writeStartArray();
                for (Object item : items) write(item, generator);
                generator.writeEndArray();
            }
            default -> throw new IllegalArgumentException("Cannot write as JSON: a " + value.getClass().getName()
                + " is not a JSON value");
        }
    }
}
