package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;
import java.util.Set;
import java.util.regex.Pattern;

/** A function bound to its context and labels (port of jeff/binding.py). Context and labels come before the input,
 * so one binding gives one fixed prompt prefix the engine can reuse. `criteria` (choice functions) and the state keep
 * the trained order. */
record Binding(FunctionSpec function, Manifest manifest, SequencedMap<String, Object> context,
                      SequencedMap<String, Object> criteria, SequencedMap<String, String> wireToCaller,
                      Calibration calibration) {
    /** The characters Python's \s matches in a str pattern (str.isspace). */
    private static final String SPACE = "[\\t\\n\\u000b\\f\\r\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029"
        + "\\u202f\\u205f\\u3000]";
    /** Python's NUMBER_LIKE (\d is any decimal digit in a str pattern); UNIX_LINES makes `$` Python's `$`. */
    private static final Pattern NUMBER_LIKE = Pattern.compile("^" + SPACE + "*[+-]?(\\p{Nd}+\\.?\\p{Nd}*|\\.\\p{Nd}+)"
        + "([eE][+-]?\\p{Nd}+)?" + SPACE + "*$", Pattern.UNIX_LINES);

    private record Criteria(SequencedMap<String, Object> criteria, SequencedMap<String, String> wireToCaller) {}

    /** The caller's context, given by code name, keyed by each field's exact key, in the order of the function's fields. */
    private static SequencedMap<String, Object> contextOf(FunctionSpec fn, Map<String, ?> given) {
        Map<String, ?> values = given == null ? Map.of() : given;
        List<String> names = new ArrayList<>(values.keySet());
        if (fn.input().form().equals("text") && !names.isEmpty()) {
            throw new JeffError.UnknownContext(fn.id() + " takes plain text only (its input " + fn.input().codeName() + " is the "
                + "whole state the adapter sees), so it has no context fields; do not pass context (got "
                + String.join(", ", PyFormat.sorted(names)) + ")");
        }
        Set<String> known = new LinkedHashSet<>();
        for (ContextField field : fn.context()) known.add(field.codeName());
        List<String> unknown = PyFormat.sorted(names.stream().filter(name -> !known.contains(name)).toList());
        if (!unknown.isEmpty()) {
            Map<String, String> codeOf = new LinkedHashMap<>();
            for (ContextField field : fn.context()) codeOf.put(field.key(), field.codeName());
            List<String> exact = unknown.stream().filter(codeOf::containsKey).map(key -> PyFormat.str(key) + " as " + codeOf.get(key)).toList();
            throw new JeffError.UnknownContext(fn.id() + " has no context field(s) " + String.join(", ", unknown) + "; its fields "
                + "are " + (known.isEmpty() ? "none" : String.join(", ", known))
                + (exact.isEmpty() ? "" : "; context is given by code name, so pass " + String.join(", ", exact)));
        }
        List<String> missing = fn.context().stream().filter(field -> field.required() && !values.containsKey(field.codeName()))
            .map(ContextField::codeName).toList();
        if (!missing.isEmpty()) throw JeffError.missingContext(fn.id(), missing);
        Map<String, Object> bound = new LinkedHashMap<>();
        for (ContextField field : fn.context()) {
            if (values.containsKey(field.codeName())) bound.put(field.key(), values.get(field.codeName()));
        }
        return Values.frozenMap(bound);
    }

    /** labels= given as a list of key strings; anything else is InvalidLabels with `refusal`. */
    private static List<String> labelKeys(Object labels, String refusal) {
        if (!(labels instanceof List<?> list) || !list.stream().allMatch(key -> key instanceof String)) {
            throw new JeffError.InvalidLabels(refusal);
        }
        return list.stream().map(key -> (String) key).toList();
    }

    /** A caller's map of labels as entries in its order; refused when it keeps no order or has a key that is not a
     * string. */
    private static List<Map.Entry<String, Object>> entriesOf(FunctionSpec fn, Map<?, ?> given) {
        if (given.size() > 1 && !(given instanceof SequencedMap<?, ?>)) {
            throw new JeffError.InvalidLabels(fn.id() + ": pass labels= as a LinkedHashMap (or another map that keeps its "
                + "order): the caller's labels become the options in their order, and a " + given.getClass().getName()
                + " keeps none");
        }
        List<Map.Entry<String, Object>> entries = new ArrayList<>();
        for (Map.Entry<?, ?> entry : given.entrySet()) {
            if (!(entry.getKey() instanceof String key)) {
                throw new JeffError.InvalidLabels(fn.id() + ": label key " + PyFormat.repr(entry.getKey()) + " is not a string");
            }
            entries.add(new java.util.AbstractMap.SimpleImmutableEntry<>(key, entry.getValue()));
        }
        return entries;
    }

    private static SequencedMap<String, String> identity(Iterable<String> keys) {
        Map<String, String> out = new LinkedHashMap<>();
        for (String key : keys) out.put(key, key);
        return Values.frozenMap(out);
    }

    private static List<String> repeated(List<String> keys) {
        Set<String> seen = new LinkedHashSet<>();
        Set<String> twice = new LinkedHashSet<>();
        for (String key : keys) if (!seen.add(key)) twice.add(key);
        return PyFormat.sorted(twice);
    }

    private static Criteria subsetCriteria(FunctionSpec fn, Object labels) {
        LabelSpec spec = fn.labels();
        String fixed = String.join(", ", spec.fixed().keySet());
        List<String> keys = labelKeys(labels, fn.id() + " offers a choice of its own labels; pass labels= as a list of keys from " + fixed);
        if (keys.isEmpty()) throw JeffError.missingLabels(fn.id());
        List<String> twice = repeated(keys);
        if (!twice.isEmpty()) throw new JeffError.InvalidLabels(fn.id() + ": label(s) " + String.join(", ", twice) + " given more than once");
        List<String> unknown = keys.stream().filter(key -> !spec.fixed().containsKey(key)).toList();
        if (!unknown.isEmpty()) {
            throw new JeffError.InvalidLabels(fn.id() + ": label(s) " + String.join(", ", unknown) + " are not among its labels " + fixed);
        }
        List<String> missing = spec.required().stream().filter(key -> !keys.contains(key)).toList();
        if (!missing.isEmpty()) {
            throw new JeffError.InvalidLabels(fn.id() + ": label(s) " + String.join(", ", missing) + " must always be offered");
        }
        Map<String, Object> criteria = new LinkedHashMap<>();
        for (String key : keys) criteria.put(key, spec.fixed().get(key));
        return new Criteria(Values.frozenMap(criteria), identity(keys));
    }

    /** Fixed labels with subset true: the caller's keys in the manifest's order, each with its fixed description. */
    private static Criteria fixedSubsetCriteria(FunctionSpec fn, Object labels) {
        LabelSpec spec = fn.labels();
        String fixed = String.join(", ", spec.fixed().keySet());
        List<String> keys = labelKeys(labels, fn.id() + " has fixed labels and may offer some of them; pass labels= as a list "
            + "of keys from " + fixed + ", or no labels= to offer them all");
        if (keys.isEmpty()) {
            throw new JeffError.InvalidLabels(fn.id() + ": labels= is an empty list; pass at least one key from " + fixed
                + ", or no labels= to offer them all");
        }
        List<String> twice = repeated(keys);
        if (!twice.isEmpty()) throw new JeffError.InvalidLabels(fn.id() + ": label(s) " + String.join(", ", twice) + " given more than once");
        List<String> unknown = keys.stream().filter(key -> !spec.fixed().containsKey(key)).toList();
        if (!unknown.isEmpty()) {
            throw new JeffError.InvalidLabels(fn.id() + ": label(s) " + String.join(", ", unknown) + " are not among its labels " + fixed);
        }
        Map<String, Object> criteria = new LinkedHashMap<>();
        for (Map.Entry<String, String> entry : spec.fixed().entrySet()) {
            if (keys.contains(entry.getKey())) criteria.put(entry.getKey(), entry.getValue());
        }
        return new Criteria(Values.frozenMap(criteria), identity(criteria.keySet()));
    }

    private static Criteria describeCriteria(FunctionSpec fn, Object labels) {
        LabelSpec spec = fn.labels();
        if (!(labels instanceof Map<?, ?> given)) {
            throw new JeffError.InvalidLabels(fn.id() + ": pass labels= as an object with exactly the keys "
                + String.join(", ", spec.keys()) + ", each with its description");
        }
        Map<String, Object> described = new LinkedHashMap<>();
        for (Map.Entry<?, ?> entry : given.entrySet()) {
            if (!(entry.getKey() instanceof String key)) {
                throw new JeffError.InvalidLabels(fn.id() + ": label key " + PyFormat.repr(entry.getKey()) + " is not a string");
            }
            described.put(key, entry.getValue());
        }
        List<String> extra = described.keySet().stream().filter(key -> !spec.keys().contains(key)).toList();
        List<String> missing = spec.keys().stream().filter(key -> !described.containsKey(key)).toList();
        if (!extra.isEmpty() || !missing.isEmpty()) {
            throw new JeffError.InvalidLabels(fn.id() + " takes exactly the labels " + String.join(", ", spec.keys())
                + (extra.isEmpty() ? "" : "; unknown: " + String.join(", ", extra))
                + (missing.isEmpty() ? "" : "; missing: " + String.join(", ", missing)));
        }
        Map<String, Object> criteria = new LinkedHashMap<>();
        for (String key : spec.keys()) criteria.put(key, described.get(key));
        return new Criteria(Values.frozenMap(criteria), identity(spec.keys()));
    }

    private static Criteria choiceCriteria(FunctionSpec fn, Object labels) {
        LabelSpec spec = fn.labels();
        if (spec.source().equals("fixed")) {
            if (labels == null) return new Criteria(Values.frozenMap(new LinkedHashMap<String, Object>(spec.fixed())), identity(spec.fixed().keySet()));
            if (!Boolean.TRUE.equals(spec.subset())) {
                throw new JeffError.InvalidLabels(fn.id() + " has fixed labels (" + String.join(", ", spec.fixed().keySet())
                    + "); do not pass labels=");
            }
            return fixedSubsetCriteria(fn, labels);
        }
        if (labels == null) throw JeffError.missingLabels(fn.id());
        if (spec.source().equals("subset")) return subsetCriteria(fn, labels);
        if (spec.source().equals("describe")) return describeCriteria(fn, labels);
        if (!(labels instanceof Map<?, ?> given)) throw new JeffError.InvalidLabels(fn.id() + ": pass labels= as an object of key -> description");
        List<Map.Entry<String, Object>> caller = entriesOf(fn, given);
        if (caller.isEmpty()) throw JeffError.missingLabels(fn.id());
        if (spec.max() != null && caller.size() > spec.max()) {
            throw new JeffError.TooManyLabels(fn.id() + " got " + caller.size() + " labels; it was trained on at most " + spec.max());
        }
        for (Map.Entry<String, Object> entry : caller) {
            String key = entry.getKey();
            if (spec.fixedFirst().containsKey(key)) {
                throw new JeffError.InvalidLabels(fn.id() + ": label " + PyFormat.str(key) + " is reserved (always listed first: "
                    + String.join(", ", spec.fixedFirst().keySet()) + "); rename yours");
            }
            if (NUMBER_LIKE.matcher(key).find()) {
                throw new JeffError.InvalidLabels(fn.id() + ": label key " + PyFormat.str(key) + " looks like a number; use a word");
            }
        }
        Map<String, Object> criteria = new LinkedHashMap<>(spec.fixedFirst());
        Map<String, String> wireToCaller = new LinkedHashMap<>(identity(spec.fixedFirst().keySet()));
        int index = 1;
        for (Map.Entry<String, Object> entry : caller) {
            String key = entry.getKey();
            String wire = spec.callerKeys() != null && !spec.callerKeys().isEmpty()
                ? spec.callerKeys().replace("{n}", String.valueOf(index)) : key;
            if (criteria.containsKey(wire)) {
                throw new JeffError.InvalidLabels(fn.id() + ": label " + PyFormat.str(key) + " collides with option " + PyFormat.str(wire));
            }
            criteria.put(wire, entry.getValue());
            wireToCaller.put(wire, key);
            index += 1;
        }
        return new Criteria(Values.frozenMap(criteria), Values.frozenMap(wireToCaller));
    }

    /** calibration: the block the function decides with on the engine's base format (Manifests.calibrationFor),
     * resolved before binding, or null when it is uncalibrated there. */
    public static Binding bind(Manifest manifest, FunctionSpec fn, Map<String, ?> context, Object labels, Calibration calibration) {
        SequencedMap<String, Object> boundContext = contextOf(fn, context);
        if (fn.type().equals("choice")) {
            Criteria criteria = choiceCriteria(fn, labels);
            return new Binding(fn, manifest, boundContext, criteria.criteria(), criteria.wireToCaller(), calibration);
        }
        if (labels != null) throw new JeffError.InvalidLabels(fn.id() + " is a " + fn.type() + " function; it takes no labels=");
        return new Binding(fn, manifest, boundContext, null, identity(optionKeysFor(fn)), calibration);
    }

    public static List<String> optionKeysFor(FunctionSpec fn) {
        if (fn.type().equals("noul")) return List.of("false", "true");
        if (fn.type().equals("score")) {
            List<String> keys = new ArrayList<>();
            for (int index = 0; index < fn.labels().levels().size(); index++) keys.add(String.valueOf(index));
            return List.copyOf(keys);
        }
        throw new IllegalStateException("choice function " + fn.id() + " gets its option keys from a binding");
    }

    public static List<String> optionKeys(Binding binding) {
        return binding.criteria() != null ? List.copyOf(binding.criteria().keySet()) : optionKeysFor(binding.function());
    }

    /** The state the model reads: form fields, the bound context then the input under its key; form text, the input. */
    public static Object requestState(String functionId, InputField spec, SequencedMap<String, Object> context, Object input) {
        if (spec.form().equals("fields")) {
            Map<String, Object> state = new LinkedHashMap<>(context);
            state.put(spec.key(), input);
            return Values.frozenMap(state);
        }
        if (!(input instanceof String text)) {
            throw new JeffError.InvalidRequest(functionId + " takes plain text: its input " + spec.codeName() + " must be a "
                + "string, got " + PyFormat.typeName(input));
        }
        return text;
    }

    public static Row buildRow(Binding binding, Object input) {
        FunctionSpec fn = binding.function();
        Object state = requestState(fn.id(), fn.input(), binding.context(), input);
        Object criteria = null;
        if (fn.type().equals("choice")) {
            if (binding.criteria() == null) throw new IllegalStateException("the binding of choice function " + fn.id() + " has no criteria");
            criteria = binding.criteria();
        } else if (fn.type().equals("score")) {
            criteria = fn.labels().levels();
        } else if (!fn.labels().fixed().isEmpty()) {
            criteria = Values.frozenMap(new LinkedHashMap<String, Object>(fn.labels().fixed()));
        }
        return new Row(state, new RowQuestion(fn.type(), fn.instructions(), criteria));
    }
}
