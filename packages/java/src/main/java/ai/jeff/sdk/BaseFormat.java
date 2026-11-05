package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/** The base's weight format an engine serves: {@code {format: "bf16"}}, or an INT4 format with the {@code format_id}
 * naming its exact weights (port of jeff/engine.py's base_format and format_id_of).
 *
 * @param format the format: {@code "bf16"} or an INT4 format such as {@code "int4-g64"}
 * @param formatId the exact weights of an INT4 format, or null when absent */
public record BaseFormat(String format, String formatId) {
    /** The bfloat16 format, the base the manifests' {@code calibration} blocks were measured on. */
    public static final String BF16 = "bf16";

    /** Checks that a format is given.
     *
     * @param format the format
     * @param formatId the exact weights of an INT4 format, or null
     * @throws NullPointerException when the format is null */
    public BaseFormat {
        Objects.requireNonNull(format, "format");
    }

    /** The bfloat16 base format.
     *
     * @return {@code {format: "bf16"}} */
    public static BaseFormat bf16() {
        return new BaseFormat(BF16, null);
    }

    /** Reads a base format from its wire form, as an engine's hello sends it.
     *
     * @param wire {@code {"format": ..., "format_id": ...}}, {@code format_id} optional
     * @return the format
     * @throws JeffError.EngineError when {@code format} is not a string, or a {@code format_id} given is not one */
    public static BaseFormat fromWire(Map<String, ?> wire) {
        if (!(wire.get("format") instanceof String format)) {
            throw new JeffError.EngineError("A base format needs its 'format' string (got " + PyFormat.repr(wire.get("format")) + ")");
        }
        if (!wire.containsKey("format_id")) return new BaseFormat(format, null);
        if (!(wire.get("format_id") instanceof String formatId)) {
            throw new JeffError.EngineError("The base format " + PyFormat.repr(new LinkedHashMap<>(wire)) + " names a format_id "
                + "that is not a string");
        }
        return new BaseFormat(format, formatId);
    }

    /** The format in its wire form.
     *
     * @return {@code {"format": ...}}, with {@code "format_id"} when there is one */
    public Map<String, Object> toMap() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("format", format);
        if (formatId != null) out.put("format_id", formatId);
        return out;
    }

    /** Which calibration functions decide with on this base (Python's format_id_of).
     *
     * @param base the engine's base format
     * @return null for a bfloat16 base (functions decide with their {@code calibration}); for any other format, its
     *     {@code format_id} (they decide with their {@code calibration_by_format} entry for it)
     * @throws JeffError.EngineError when a bfloat16 base carries a {@code format_id}, or another format has none */
    public static String formatIdOf(BaseFormat base) {
        if (base.format().equals(BF16)) {
            if (base.formatId() != null) {
                throw new JeffError.EngineError("The engine's base format " + PyFormat.repr(base.toMap()) + " is bfloat16 but "
                    + "carries a format_id");
            }
            return null;
        }
        if (base.formatId() == null) {
            throw new JeffError.EngineError("The engine's base format " + PyFormat.repr(base.toMap()) + " names no format_id; "
                + "every base format other than " + BF16 + " is one exact set of weights, named by its format_id");
        }
        return base.formatId();
    }
}
