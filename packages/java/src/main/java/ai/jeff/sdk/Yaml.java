package ai.jeff.sdk;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.DateTimeException;
import java.time.LocalDate;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.yaml.snakeyaml.DumperOptions;
import org.yaml.snakeyaml.LoaderOptions;
import org.yaml.snakeyaml.constructor.AbstractConstruct;
import org.yaml.snakeyaml.constructor.SafeConstructor;
import org.yaml.snakeyaml.error.YAMLException;
import org.yaml.snakeyaml.nodes.MappingNode;
import org.yaml.snakeyaml.nodes.Node;
import org.yaml.snakeyaml.nodes.NodeTuple;
import org.yaml.snakeyaml.nodes.ScalarNode;
import org.yaml.snakeyaml.nodes.SequenceNode;
import org.yaml.snakeyaml.nodes.Tag;
import org.yaml.snakeyaml.representer.Representer;
import org.yaml.snakeyaml.resolver.Resolver;

/** YAML as PyYAML's safe_load reads it (see Task 5 of the Java SDK plan): PyYAML's implicit resolvers, a bare date
 * kept as its text, string keys only, every whole number a Long and every mapping a LinkedHashMap. Nesting deeper
 * than 512 levels is refused by name instead of overflowing the stack (the ruling carried from the C# port's
 * review, after deep nesting crashed that reader). The JSON-like shape itself (Long, LinkedHashMap, ArrayList, the
 * terminal type whitelist) is Values.plain()'s walk, not a second copy of it here: this class only translates its
 * two failure cases into the wording this SDK has always raised for a YAML document.
 *
 * <p>Aliases: SnakeYAML shares an anchored node between its aliases, but the copy into plain values expands every alias
 * in full, so a few hundred bytes of nested aliases could build gigabytes. The values the aliases add to one document
 * are counted on the composed node tree before anything is built, and more than 1,000,000 is refused with a
 * ManifestError (the Rust port's limit). SnakeYAML's own guard (at most 50 aliases to collections) is lifted, because
 * it refuses harmless files PyYAML reads, and the count is the limit instead. */
final class Yaml {
    private static final int MAX_NESTING_DEPTH = 512;
    /** The most values that aliases may add to one document. */
    static final long ALIAS_EXPANSION_LIMIT = 1_000_000;

    private Yaml() {}

    static Object read(Path path) {
        String text;
        try {
            text = Files.readString(path);
        } catch (IOException error) {
            throw new UncheckedIOException("Reading " + path + " failed", error);
        }
        return parse(text, path.toString());
    }

    /** YAML text read on a large-stack thread (see Nesting): SnakeYAML's reader recurses once per level, and YAML
     * text cannot be measured cheaply first. Manifests and the contract's YAML files are read rarely. */
    static Object parse(String text, String where) {
        return Nesting.onLargeStack(() -> parseHere(text, where));
    }

    private static Object parseHere(String text, String where) {
        LoaderOptions options = new LoaderOptions();
        options.setAllowDuplicateKeys(true);
        options.setNestingDepthLimit(MAX_NESTING_DEPTH);
        options.setMaxAliasesForCollections(Integer.MAX_VALUE); // the expansion count below is the limit
        DumperOptions dumper = new DumperOptions();
        PyYamlConstructor constructor = new PyYamlConstructor(options);
        org.yaml.snakeyaml.Yaml yaml = new org.yaml.snakeyaml.Yaml(constructor, new Representer(dumper), dumper, options,
            new PyYamlResolver());
        Node document;
        try {
            document = yaml.compose(new java.io.StringReader(text));
        } catch (YAMLException error) {
            throw new JeffError.ManifestError(where + " is not valid YAML: " + error.getMessage());
        }
        if (document != null) new AliasCount(where).size(document);
        Object loaded;
        try {
            loaded = document == null ? null : constructor.build(document);
        } catch (YAMLException error) {
            throw new JeffError.ManifestError(where + " is not valid YAML: " + error.getMessage());
        }
        try {
            return Values.plain(loaded);
        } catch (Values.NotAString error) {
            throw new JeffError.ManifestError(where + ": the mapping key " + PyFormat.repr(error.key) + " is not a string; quote it");
        } catch (Values.NotJson error) {
            throw new JeffError.ManifestError(where + ": YAML gave a " + error.value.getClass().getName()
                + ", which is not a JSON value");
        }
    }

    /** Counts the values a document's aliases add once copied into plain values (each alias adds the whole of the
     * value it names, its own aliases expanded), and refuses past ALIAS_EXPANSION_LIMIT. Mapping keys are not values (as
     * the Rust and C# SDKs count, which count the JSON values a document becomes); an aliased key still adds what it
     * names.
     * The node tree is at most MAX_NESTING_DEPTH deep (the composer refuses deeper), so the recursion is bounded. */
    private static final class AliasCount {
        private final String where;
        private final Map<Node, Long> sizes = new IdentityHashMap<>();
        private final Set<Node> open = Collections.newSetFromMap(new IdentityHashMap<>());
        private long expanded;

        AliasCount(String where) {
            this.where = where;
        }

        /** The number of values `node` stands for, itself included. */
        long size(Node node) {
            Long known = sizes.get(node);
            if (known != null) {
                // a second reach of the same node is an alias: copying it adds all of it again
                expanded += known;
                if (expanded > ALIAS_EXPANSION_LIMIT) {
                    throw new JeffError.ManifestError(where + ": the aliases expand to more than 1,000,000 values;"
                        + " write the repeated parts out or use fewer aliases");
                }
                return known;
            }
            if (!open.add(node)) {
                throw new JeffError.ManifestError(where + ": an alias at line " + (node.getStartMark().getLine() + 1)
                    + " names a collection it is inside, which has no JSON form; write the value out");
            }
            long size = 1;
            if (node instanceof MappingNode mapping) {
                for (NodeTuple tuple : mapping.getValue()) {
                    size(tuple.getKeyNode()); // counts the aliases inside a key, not the key itself
                    size += size(tuple.getValueNode());
                }
            } else if (node instanceof SequenceNode sequence) {
                for (Node item : sequence.getValue()) size += size(item);
            }
            open.remove(node);
            sizes.put(node, size);
            return size;
        }
    }

    /** PyYAML's implicit resolvers (yaml/resolver.py), pattern for pattern. */
    private static final class PyYamlResolver extends Resolver {
        private static final Pattern BOOL = Pattern.compile(
            "^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$");
        private static final Pattern FLOAT = Pattern.compile("^(?:[-+]?(?:[0-9][0-9_]*)\\.[0-9_]*(?:[eE][-+][0-9]+)?"
            + "|\\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\\.[0-9_]*"
            + "|[-+]?\\.(?:inf|Inf|INF)|\\.(?:nan|NaN|NAN))$");
        private static final Pattern INT = Pattern.compile("^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)"
            + "|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$");
        private static final Pattern MERGE = Pattern.compile("^(?:<<)$");
        private static final Pattern NULL = Pattern.compile("^(?:~|null|Null|NULL|)$");
        private static final Pattern TIMESTAMP = Pattern.compile("^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
            + "|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \\t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9]"
            + "(?:\\.[0-9]*)?(?:[ \\t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$");
        private static final Pattern VALUE = Pattern.compile("^(?:=)$");
        static final Tag VALUE_TAG = new Tag("tag:yaml.org,2002:value");

        @Override
        protected void addImplicitResolvers() {
            addImplicitResolver(Tag.BOOL, BOOL, "yYnNtTfFoO");
            addImplicitResolver(Tag.FLOAT, FLOAT, "-+0123456789.");
            addImplicitResolver(Tag.INT, INT, "-+0123456789");
            addImplicitResolver(Tag.MERGE, MERGE, "<");
            addImplicitResolver(Tag.NULL, NULL, "~nN\0");
            addImplicitResolver(Tag.NULL, Pattern.compile("^$"), null);
            addImplicitResolver(Tag.TIMESTAMP, TIMESTAMP, "0123456789");
            addImplicitResolver(VALUE_TAG, VALUE, "=");
        }
    }

    private static final class PyYamlConstructor extends SafeConstructor {
        private static final Pattern DATE_ONLY = Pattern.compile("^([0-9]{4})-([0-9]{2})-([0-9]{2})$");

        PyYamlConstructor(LoaderOptions options) {
            super(options);
            yamlConstructors.put(Tag.TIMESTAMP, new DateOnly());
        }

        /** The value of a document already composed (and its aliases counted). */
        Object build(Node document) {
            return constructDocument(document);
        }

        /** SnakeYAML removes the earlier of two repeated keys from the node before building the map, which moves the
         * key to its last place; PyYAML keeps its first place (a dict assignment). Leaving every pair in place and
         * letting LinkedHashMap.put build the map gives PyYAML's result: the first place and the last value.
         * flattenMapping calls the two-argument overload directly, so that is the one to override: overriding the
         * one-argument processDuplicateKeys(MappingNode) is never reached and silently leaves the default removal
         * in place. */
        @Override
        protected void processDuplicateKeys(MappingNode node, boolean forceStringKeys) {
        }

        /** A bare YYYY-MM-DD as its own text (what PyYAML's datetime.date.isoformat() gives back for it); anything
         * with a time part, or a day not on the calendar, refused. */
        private final class DateOnly extends AbstractConstruct {
            @Override
            public Object construct(Node node) {
                String source = constructScalar((ScalarNode) node);
                Matcher match = DATE_ONLY.matcher(source);
                if (!match.matches()) {
                    throw new YAMLException(PyFormat.str(source) + " is a YAML timestamp with a time part; this SDK reads a "
                        + "date only as a bare YYYY-MM-DD, so quote it to keep it a plain string");
                }
                int year = Integer.parseInt(match.group(1));
                int month = Integer.parseInt(match.group(2));
                int day = Integer.parseInt(match.group(3));
                String refused = PyFormat.str(source) + " is not a real calendar date; quote it to keep it a plain string";
                if (year < 1) throw new YAMLException(refused);
                try {
                    LocalDate.of(year, month, day);
                } catch (DateTimeException error) {
                    throw new YAMLException(refused, error);
                }
                return source;
            }
        }
    }
}
