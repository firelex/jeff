package ai.jeff.sdk;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.SequencedMap;
import java.util.Set;
import java.util.regex.Pattern;

/** Adapter manifests: reading and checking them (port of jeff/manifest.py; every check and message is the Python
 * SDK's), code names, and the calibration a function decides with on a base format. Most callers never need these:
 * {@link Catalogue#fromDir} reads a whole catalogue folder. */
public final class Manifests {
    /** What a code name looks like: a lower-case letter, then lower-case letters, digits and underscores. Match it
     * with {@code matcher(name).matches()} (as Python's fullmatch), never {@code find()}, whose {@code $} would also
     * match before a final line break. */
    public static final Pattern CODE_NAME = Pattern.compile("^[a-z][a-z0-9_]*$");
    private static final Pattern NOT_CODE = Pattern.compile("[^a-z0-9]+");
    private static final Pattern VERSION_PART = Pattern.compile("^v[0-9]+$");

    private Manifests() {}

    /** The indexes of the segments of a name that are a version part (v and digits, the whole segment). */
    private static List<Integer> versionIndexes(List<String> segments) {
        List<Integer> versions = new ArrayList<>();
        for (int index = 0; index < segments.size(); index++) {
            if (VERSION_PART.matcher(segments.get(index)).matches()) versions.add(index);
        }
        return versions;
    }

    private static List<String> nameSegments(String adapterId) {
        return new ArrayList<>(List.of(adapterId.substring(adapterId.lastIndexOf('.') + 1).split("-", -1)));
    }

    /** The adapter id without its version part (contract/schemas/manifest.schema.json, aliases): the one
     * hyphen-separated segment of its name (the part after the second dot) that is v and digits, removed with one
     * hyphen, as jeff.routing.router-open-v14 to jeff.routing.router-open.
     *
     * @param adapterId the adapter id
     * @return the id without its version part, or null when the name has no such segment, more than one, or nothing
     *     besides it */
    public static String unversionedId(String adapterId) {
        List<String> segments = nameSegments(adapterId);
        List<Integer> versions = versionIndexes(segments);
        if (versions.size() != 1 || segments.size() == 1) return null;
        segments.remove((int) versions.get(0));
        return adapterId.substring(0, adapterId.lastIndexOf('.') + 1) + String.join("-", segments);
    }

    /** No alias is listed twice, and an alias of the id form (the schema allows it as jeff.&lt;domain&gt;.&lt;name&gt;)
     * must be the adapter's unversioned id. */
    private static void checkAliases(String where, String adapterId, List<String> aliases) {
        Set<String> seen = new HashSet<>();
        for (String alias : aliases) {
            if (!seen.add(alias)) throw new JeffError.ManifestError(where + ": the alias " + alias + " is listed twice");
            if (!alias.startsWith("jeff.")) continue;
            List<String> segments = nameSegments(adapterId);
            List<Integer> versions = versionIndexes(segments);
            String form = where + ": the alias " + alias + " has the form of an adapter ID, which only the adapter's ID "
                + "without its version part may have, and ";
            if (versions.isEmpty()) {
                throw new JeffError.ManifestError(form + adapterId + " has no version part (one segment v<digits> in its "
                    + "name besides others, as in jeff.routing.router-open-v14)");
            }
            if (versions.size() > 1) {
                List<String> parts = new ArrayList<>();
                for (int index : versions) parts.add(segments.get(index));
                throw new JeffError.ManifestError(form + adapterId + " has more than one version part ("
                    + String.join(", ", parts) + "), so it has no ID without one");
            }
            if (segments.size() == 1) {
                throw new JeffError.ManifestError(form + "the name of " + adapterId
                    + " is only a version part, so it has no ID without one");
            }
            String expected = unversionedId(adapterId);
            if (!alias.equals(expected)) {
                throw new JeffError.ManifestError(where + ": the alias " + alias + " has the form of an adapter ID, so it "
                    + "must be " + expected + ", the ID " + adapterId + " without its version part");
            }
        }
    }

    /** The code name of a context field or input key, as the Python SDK makes it: the key itself when it is one,
     * else the key in lower case with every run of characters other than a to z and 0 to 9 turned into one
     * underscore, and the underscores at either end removed.
     *
     * @param key the key
     * @return its code name (empty when the key holds no letter or digit) */
    public static String codeNameOf(String key) {
        if (CODE_NAME.matcher(key).matches()) return key;
        String code = NOT_CODE.matcher(key.toLowerCase(Locale.ROOT)).replaceAll("_");
        int start = 0;
        int end = code.length();
        while (start < end && code.charAt(start) == '_') start++;
        while (end > start && code.charAt(end - 1) == '_') end--;
        return code.substring(start, end);
    }

    private static List<String> optionalStrings(Object value) {
        return value == null ? List.of() : Values.strings(value);
    }

    private static SequencedMap<String, String> descriptions(Object value) {
        Map<String, String> out = new LinkedHashMap<>();
        if (value != null) for (Map.Entry<String, Object> entry : Values.map(value).entrySet()) out.put(entry.getKey(), (String) entry.getValue());
        return Values.frozenMap(out);
    }

    private static LabelSpec labelsOf(Map<String, Object> raw) {
        return new LabelSpec((String) raw.get("source"), descriptions(raw.get("fixed")), optionalStrings(raw.get("levels")),
            descriptions(raw.get("fixed_first")), (String) raw.get("caller_keys"), (Long) raw.get("max"),
            optionalStrings(raw.get("required")), optionalStrings(raw.get("keys")), (Boolean) raw.get("subset"));
    }

    /** Refuses the first number in the manifest, in document order, that is not finite (NaN or an infinity, which YAML's
     * .nan and .inf give), by its place (a JSON pointer) and value, as the Python SDK does (contract/conformance/
     * manifests.json). JSON has no such number, and a calibration threshold of NaN would turn calibration off without a
     * word: {@code confidence < NaN} is always false. The walk keeps its own stack, so no depth can overflow it. */
    static void checkFinite(Object document, String where) {
        java.util.ArrayDeque<Object[]> pending = new java.util.ArrayDeque<>();
        pending.push(new Object[] {document, ""});
        while (!pending.isEmpty()) {
            Object[] next = pending.pop();
            Object item = next[0];
            String at = (String) next[1];
            if ((item instanceof Double number && !Double.isFinite(number)) || (item instanceof Float single && !Float.isFinite(single))) {
                throw new JeffError.ManifestError(where + ": " + (at.isEmpty() ? "/" : at) + " is "
                    + PyFormat.floatRepr(((Number) item).doubleValue()) + ", not a finite number, which JSON cannot hold; write a finite number");
            }
            List<Object[]> children = new ArrayList<>();
            if (item instanceof Map<?, ?> map) {
                for (Map.Entry<?, ?> entry : map.entrySet()) children.add(new Object[] {entry.getValue(), at + "/" + entry.getKey()});
            } else if (item instanceof List<?> list) {
                for (int index = 0; index < list.size(); index++) children.add(new Object[] {list.get(index), at + "/" + index});
            }
            for (int index = children.size() - 1; index >= 0; index--) pending.push(children.get(index));
        }
    }

    private static String codeNameFor(String where, Map<String, Object> field) {
        if (field.containsKey("code_name")) return (String) field.get("code_name");
        String key = (String) field.get("key");
        String code = codeNameOf(key);
        if (!CODE_NAME.matcher(code).matches()) {
            throw new JeffError.ManifestError(where + ": field " + PyFormat.str(key) + " gives no code name by the rule "
                + "(lower-case, other characters to '_': " + PyFormat.str(code) + ", which is not of the form "
                + CODE_NAME.pattern() + "); declare its code_name");
        }
        return code;
    }

    private static void checkCodeNames(String where, List<List<String>> fields) {
        Map<String, List<String>> byCode = new LinkedHashMap<>();
        for (List<String> field : fields) byCode.computeIfAbsent(field.get(1), code -> new ArrayList<>()).add(field.get(0));
        List<String> shared = new ArrayList<>();
        for (Map.Entry<String, List<String>> entry : byCode.entrySet()) {
            if (entry.getValue().size() > 1) {
                shared.add(String.join(", ", entry.getValue().stream().map(PyFormat::str).toList()) + " -> " + entry.getKey());
            }
        }
        if (!shared.isEmpty()) {
            throw new JeffError.ManifestError(where + ": fields share a code name, which callers could not tell apart: "
                + String.join("; ", shared) + "; declare a distinct code_name for each");
        }
    }

    private record Fields(List<ContextField> context, InputField input) {}

    private static Fields requestFields(String where, List<Object> rawContext, Map<String, Object> rawInput) {
        List<ContextField> context = new ArrayList<>();
        for (Object item : rawContext) {
            Map<String, Object> field = Values.map(item);
            context.add(new ContextField((String) field.get("key"), (String) field.get("description"),
                (Boolean) field.get("required"), codeNameFor(where, field)));
        }
        List<String> keys = context.stream().map(ContextField::key).toList();
        String inputKey = (String) rawInput.get("key");
        if (new HashSet<>(keys).size() != keys.size() || keys.contains(inputKey)) {
            throw new JeffError.ManifestError(where + ": context keys must be unique and differ from the input key ("
                + PyFormat.repr(keys) + ", input " + inputKey + ")");
        }
        if ("text".equals(rawInput.get("form")) && !context.isEmpty()) {
            throw new JeffError.ManifestError(where + ": input " + PyFormat.str(inputKey) + " has form text (the state is the "
                + "plain input string), so there can be no context fields, but it has " + PyFormat.repr(keys));
        }
        InputField input = new InputField(inputKey, (String) rawInput.get("description"), codeNameFor(where, rawInput),
            (String) rawInput.get("form"));
        List<List<String>> codes = new ArrayList<>();
        for (ContextField field : context) codes.add(List.of(field.key(), field.codeName()));
        codes.add(List.of(input.key(), input.codeName()));
        checkCodeNames(where, codes);
        return new Fields(Collections.unmodifiableList(context), input);
    }

    private static boolean given(String text) {
        return text != null && !text.isEmpty();
    }

    private static void checkFunction(FunctionSpec spec, long maxOptions, String where) {
        SequencedMap<String, Contract.InterfaceSpec> known = Contract.interfaces();
        String name = where + ": function " + spec.key();
        Contract.InterfaceSpec iface = known.get(spec.interfaceName());
        if (iface == null) {
            throw new JeffError.ManifestError(name + " names interface " + PyFormat.str(spec.interfaceName()) + ", which is "
                + "not in the interface catalogue (" + String.join(", ", known.keySet()) + ")");
        }
        if (!spec.type().equals(iface.questionType())) {
            throw new JeffError.ManifestError(name + " has type " + spec.type() + " but interface " + spec.interfaceName()
                + " compiles to " + iface.questionType());
        }
        LabelSpec labels = spec.labels();
        String source = labels.source();
        if (!spec.type().equals("choice") && (!labels.required().isEmpty() || !labels.keys().isEmpty())) {
            throw new JeffError.ManifestError(name + ": only choice functions have 'required' or 'keys'");
        }
        if (spec.type().equals("choice")) {
            if (!labels.levels().isEmpty()) throw new JeffError.ManifestError(name + ": choice functions have no levels");
            if (source.equals("fixed") && labels.fixed().isEmpty()) {
                throw new JeffError.ManifestError(name + ": fixed labels need a non-empty 'fixed'");
            }
            if (source.equals("caller") && labels.max() == null) {
                throw new JeffError.ManifestError(name + ": caller labels need 'max', the most labels the adapter was trained on");
            }
            if (!labels.required().isEmpty() && !source.equals("subset")) {
                throw new JeffError.ManifestError(name + ": only subset labels have 'required'");
            }
            if (!labels.keys().isEmpty() && !source.equals("describe")) {
                throw new JeffError.ManifestError(name + ": only describe labels have 'keys'");
            }
            if (source.equals("subset")) {
                if (labels.fixed().isEmpty() || !labels.fixedFirst().isEmpty() || given(labels.callerKeys()) || labels.max() != null) {
                    throw new JeffError.ManifestError(name + ": subset labels need a non-empty 'fixed' (every allowed label) "
                        + "and no fixed_first, caller_keys or max");
                }
                List<String> stray = labels.required().stream().filter(key -> !labels.fixed().containsKey(key)).toList();
                if (!stray.isEmpty()) {
                    throw new JeffError.ManifestError(name + ": required labels " + PyFormat.repr(stray) + " are not in 'fixed' ("
                        + PyFormat.repr(List.copyOf(labels.fixed().keySet())) + ")");
                }
            }
            if (source.equals("describe")) {
                if (labels.keys().isEmpty() || !labels.fixed().isEmpty() || !labels.fixedFirst().isEmpty()
                    || given(labels.callerKeys()) || labels.max() != null) {
                    throw new JeffError.ManifestError(name + ": describe labels need non-empty 'keys' and no fixed, fixed_first, "
                        + "caller_keys or max");
                }
                if (new HashSet<>(labels.keys()).size() != labels.keys().size()) {
                    throw new JeffError.ManifestError(name + ": describe keys " + PyFormat.repr(labels.keys()) + " repeat a key");
                }
            }
            long count = labels.fixed().size() + labels.fixedFirst().size() + (labels.max() == null ? 0 : labels.max())
                + labels.keys().size();
            if (count > maxOptions) {
                throw new JeffError.ManifestError(name + " allows " + count + " options, more than the base's max_options " + maxOptions);
            }
            List<String> fixedKeys = List.copyOf(labels.fixed().keySet());
            if (iface.values() != null && !fixedKeys.equals(iface.values())) {
                throw new JeffError.ManifestError(name + ": interface " + spec.interfaceName() + " requires labels "
                    + PyFormat.repr(iface.values()) + ", got " + PyFormat.repr(fixedKeys));
            }
        } else if (spec.type().equals("score")) {
            if (labels.levels().size() < 2 || !labels.fixed().isEmpty() || !source.equals("fixed")) {
                throw new JeffError.ManifestError(name + ": score functions need fixed 'levels' (2 to 10) and nothing else");
            }
        } else if (!labels.levels().isEmpty() || !source.equals("fixed")
            || labels.fixed().keySet().stream().anyMatch(key -> !key.equals("true") && !key.equals("false"))) {
            throw new JeffError.ManifestError(name + ": noul functions may only describe 'true' and 'false' in 'fixed'");
        }
        Set<String> knownLabels = new LinkedHashSet<>(labels.fixed().keySet());
        knownLabels.addAll(labels.fixedFirst().keySet());
        knownLabels.addAll(labels.keys());
        List<String> unknown = spec.abstainLabels().stream().filter(label -> !knownLabels.contains(label)).toList();
        if (!unknown.isEmpty()) {
            throw new JeffError.ManifestError(name + ": abstain_labels " + PyFormat.repr(unknown) + " are not among its labels "
                + PyFormat.repr(PyFormat.sorted(knownLabels)));
        }
    }

    /** tests.function_by as its list of criteria (a single value is the list holding only it). The schema has
     * already checked its shape; anything else is refused by name rather than cast. */
    private static List<String> criteria(String where, Object functionBy) {
        if (functionBy instanceof String text) return List.of(text);
        if (functionBy instanceof List<?> items && items.stream().allMatch(item -> item instanceof String)) {
            return Values.strings(items);
        }
        throw new JeffError.ManifestError(where + ": tests.function_by is " + PyFormat.repr(functionBy) + ", which is "
            + "neither a criterion nor a list of criteria");
    }

    private static void checkTests(String where, Map<String, Object> tests, SequencedMap<String, FunctionSpec> functions) {
        if (!tests.containsKey("function_by")) throw new JeffError.ManifestError(where + ": tests has no function_by");
        Object functionBy = tests.get("function_by");
        List<String> criteria = criteria(where, functionBy);
        List<String> keys = List.copyOf(functions.keySet());
        if ("question_type".equals(functionBy)) {
            List<String> types = functions.values().stream().map(FunctionSpec::type).toList();
            if (new HashSet<>(types).size() != types.size()) {
                throw new JeffError.ManifestError(where + ": tests.function_by question_type needs one function per question "
                    + "type; the functions have types " + PyFormat.repr(types));
            }
        }
        if (tests.containsKey("kinds")) {
            if (!criteria.contains("source_kind")) {
                throw new JeffError.ManifestError(where + ": tests.kinds maps source.kind values to functions, so "
                    + "tests.function_by must use source_kind (it is " + PyFormat.repr(functionBy) + ")");
            }
            Map<String, Object> kinds = Values.map(tests.get("kinds"));
            Set<String> named = new LinkedHashSet<>();
            List<String> several = new ArrayList<>();
            for (Map.Entry<String, Object> kind : kinds.entrySet()) {
                if (kind.getValue() instanceof String one) named.add(one);
                else {
                    named.addAll(Values.strings(kind.getValue()));
                    several.add(kind.getKey());
                }
            }
            List<String> stray = PyFormat.sorted(named.stream().filter(key -> !functions.containsKey(key)).toList());
            if (!stray.isEmpty()) {
                throw new JeffError.ManifestError(where + ": tests.kinds names " + PyFormat.repr(stray) + ", which are not its "
                    + "functions (" + PyFormat.repr(keys) + ")");
            }
            if (!several.isEmpty() && criteria.equals(List.of("source_kind"))) {
                throw new JeffError.ManifestError(where + ": tests.kinds maps " + PyFormat.repr(several) + " to several functions "
                    + "each, but tests.function_by is source_kind alone, so no other criterion can leave one of them: every "
                    + "row of those kinds would match several functions");
            }
        }
        boolean uses = criteria.contains("id_pattern");
        if (!tests.containsKey("id_patterns")) {
            if (uses) {
                throw new JeffError.ManifestError(where + ": tests.function_by uses id_pattern, so tests.id_patterns must give "
                    + "every function's row id pattern (" + String.join(", ", keys) + ")");
            }
            return;
        }
        if (!uses) {
            throw new JeffError.ManifestError(where + ": tests.id_patterns is given, but tests.function_by does not use "
                + "id_pattern (it is " + PyFormat.repr(functionBy) + ")");
        }
        Map<String, Object> idPatterns = Values.map(tests.get("id_patterns"));
        List<String> named = List.copyOf(idPatterns.keySet());
        if (!named.equals(keys)) {
            throw new JeffError.ManifestError(where + ": tests.id_patterns must name every function, in the functions' order ("
                + PyFormat.repr(keys) + "); it names " + PyFormat.repr(named));
        }
        // The patterns only map test rows, which this SDK never does, so a pattern is never run here: each is checked
        // for being one Python's re.compile accepts, by a port of Python's own syntax checks (PythonReSyntax), with
        // Python's message. A construct that port does not follow is refused, never guessed at.
        for (Map.Entry<String, Object> entry : idPatterns.entrySet()) {
            String pattern = (String) entry.getValue();
            try {
                PythonReSyntax.check(pattern);
            } catch (PythonReSyntax.Refused error) {
                throw new JeffError.ManifestError(where + ": tests.id_patterns " + entry.getKey() + ": "
                    + PyFormat.str(pattern) + " is not a regular expression (" + error.getMessage() + ")");
            } catch (PythonReSyntax.Unchecked refused) {
                throw new JeffError.ManifestError(where + ": tests.id_patterns " + entry.getKey() + ": "
                    + PyFormat.str(pattern) + " uses " + refused.getMessage() + ", which the Java SDK does not check as "
                    + "Python's re module would; write the pattern without it");
            }
        }
    }

    /** A manifest from its document (parsed YAML or JSON), checked against the schema and every rule.
     *
     * @param raw the document
     * @param path where it came from, named in every error
     * @return the manifest
     * @throws JeffError.ContractError when it does not match contract/schemas/manifest.schema.json
     * @throws JeffError.ManifestError when it breaks one of the other rules */
    public static Manifest fromMap(Object raw, String path) {
        Map<String, Object> plain = Values.map(Values.plain(raw));
        checkFinite(plain, path);
        Contract.validate("manifest", plain, path);
        String adapterId = (String) plain.get("id");
        checkAliases(path, adapterId, Values.strings(plain.get("aliases")));
        Fields fields = requestFields(path, Values.list(plain.get("context")), Values.map(plain.get("input")));
        Map<String, Object> base = Values.map(plain.get("base"));
        long maxOptions = (Long) base.get("max_options");
        Map<String, FunctionSpec> functions = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : Values.map(plain.get("functions")).entrySet()) {
            String key = entry.getKey();
            Map<String, Object> body = Values.map(entry.getValue());
            Calibration calibration = body.get("calibration") == null ? null : Calibration.fromWire(Values.map(body.get("calibration")));
            Map<String, Calibration> byFormat = new LinkedHashMap<>();
            if (body.get("calibration_by_format") instanceof Map<?, ?> entries) {
                for (Map.Entry<?, ?> formatEntry : entries.entrySet()) {
                    byFormat.put((String) formatEntry.getKey(), Calibration.fromWire(Values.map(formatEntry.getValue())));
                }
            }
            if (!byFormat.isEmpty() && calibration == null) {
                throw new JeffError.ManifestError(path + ": function " + key + " has calibration_by_format entries ("
                    + String.join(", ", byFormat.keySet()) + ") but calibration null; a function uncalibrated on the bfloat16 "
                    + "base stays uncalibrated in every format");
            }
            Fields own = body.containsKey("input")
                ? requestFields(path + ": function " + key, Values.list(body.get("context")), Values.map(body.get("input")))
                : fields;
            FunctionSpec spec = new FunctionSpec(adapterId, key, adapterId + "." + key, (String) body.get("interface"),
                (String) body.get("type"), (String) body.get("instructions"), labelsOf(Values.map(body.get("labels"))),
                Values.strings(body.get("abstain_labels")), calibration, own.context(), own.input(), Values.frozenMap(byFormat));
            checkFunction(spec, maxOptions, path);
            functions.put(key, spec);
        }
        SequencedMap<String, FunctionSpec> frozen = Values.frozenMap(functions);
        checkTests(path, Values.map(plain.get("tests")), frozen);
        List<String> routesTo = Values.strings(plain.get("routes_to"));
        if (!routesTo.isEmpty()) {
            FunctionSpec first = frozen.isEmpty() ? null : frozen.firstEntry().getValue();
            List<String> fixed = first == null ? List.of() : List.copyOf(first.labels().fixed().keySet());
            if (frozen.size() != 1 || !first.interfaceName().equals("route") || !first.labels().source().equals("subset")
                || !fixed.equals(routesTo)) {
                throw new JeffError.ManifestError(path + ": a router has exactly one route function with subset labels whose "
                    + "keys equal routes_to " + PyFormat.repr(routesTo) + " (dispatch offers the targets that lead to the "
                    + "requested interface)");
            }
            if (!first.labels().required().isEmpty()) {
                throw new JeffError.ManifestError(path + ": a router's labels have no required keys; dispatch offers only the "
                    + "targets that lead to the requested interface");
            }
        }
        Map<String, Object> licence = Values.map(plain.get("licence"));
        // deep read-only, so a caller of a shared catalogue (the cached shipped one) can change nothing another sees
        Map<String, Object> read = Values.map(Values.frozen(plain));
        return new Manifest(adapterId, (String) plain.get("version"), Values.strings(plain.get("aliases")),
            (String) licence.get("kind"), Values.map(read.get("weights")), (String) base.get("id"),
            (String) base.get("version"), maxOptions, fields.context(), fields.input(), frozen, routesTo,
            Values.map(read.get("tests")), read, path, !routesTo.isEmpty());
    }

    /** Reads and checks a manifest file.
     *
     * @param path the manifest.yaml (or JSON) file
     * @return the manifest
     * @throws JeffError.ManifestError when it is not valid YAML or breaks a rule
     * @throws JeffError.ContractError when it does not match contract/schemas/manifest.schema.json
     * @throws java.io.UncheckedIOException when it cannot be read */
    public static Manifest load(Path path) {
        return fromMap(Yaml.read(path), path.toString());
    }

    /** The calibration a function decides with: its bfloat16 one when formatId is null (or it is uncalibrated), else
     * its entry for that INT4 base; a ManifestError naming the command that fits one when there is none. */
    static Calibration resolveCalibration(String functionId, Calibration calibration,
                                                 SequencedMap<String, Calibration> byFormat, String formatId) {
        if (formatId == null || calibration == null) return calibration;
        Calibration entry = byFormat.get(formatId);
        if (entry == null) {
            String calibratedOn = byFormat.isEmpty() ? "no INT4 base" : String.join(", ", byFormat.keySet());
            throw new JeffError.ManifestError(functionId + " has no calibration for the INT4 base this engine serves ("
                + formatId + "); it is calibrated on the bfloat16 base and on " + calibratedOn + ". Fit its threshold on "
                + "this base with `jeff calibrate " + functionId + " --base <the INT4 base folder>` (for an adapter served "
                + "from an adapter folder, `jeff calibrate <that folder> --base <the INT4 base folder>`, which writes into "
                + "the folder's manifest.yaml)");
        }
        return entry;
    }

    /** The calibration a function decides with on a base format (Python's calibration_for).
     *
     * @param fn the function
     * @param formatId the engine's {@link BaseFormat#formatIdOf}: null for bfloat16
     * @return its bfloat16 calibration when {@code formatId} is null or it is uncalibrated (null), else its entry for
     *     that INT4 base
     * @throws JeffError.ManifestError when a calibrated function has no entry for that INT4 base, naming the command
     *     that fits one */
    public static Calibration calibrationFor(FunctionSpec fn, String formatId) {
        return resolveCalibration(fn.id(), fn.calibration(), fn.calibrationByFormat(), formatId);
    }
}
