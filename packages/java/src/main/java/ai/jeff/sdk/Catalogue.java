package ai.jeff.sdk;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.locks.ReentrantLock;
import java.util.stream.Stream;

/** Every known adapter manifest, looked up by id, alias, version or interface (port of jeff/catalogue.py). A
 * reference is an adapter id or alias, optionally followed by a dot and a function key, and optionally by
 * {@code @version} ({@code jeff.safety.spam.is_spam}, {@code spam.is_spam@1.4}). */
public final class Catalogue {
    private static final ReentrantLock SHIPPED_LOCK = new ReentrantLock();
    private static Catalogue shipped;

    private final Map<String, Manifest> byId = new LinkedHashMap<>();
    private final Map<String, String> byAlias = new LinkedHashMap<>();
    private final Map<String, Object> bases;

    /** A catalogue of these manifests and bases.
     *
     * @param manifests the manifests
     * @param bases bases.yaml's content: each base id, its versions, and each version's settings
     * @throws JeffError.ManifestError when two manifests share an id or alias */
    public Catalogue(Collection<Manifest> manifests, Map<String, Object> bases) {
        for (Manifest manifest : manifests) {
            List<String> names = new ArrayList<>();
            names.add(manifest.id());
            names.addAll(manifest.aliases());
            for (String name : names) {
                if (byId.containsKey(name) || byAlias.containsKey(name)) {
                    throw new JeffError.ManifestError(manifest.path() + ": the name " + name + " is already used by another adapter");
                }
            }
            byId.put(manifest.id(), manifest);
            for (String alias : manifest.aliases()) byAlias.put(alias, manifest.id());
        }
        this.bases = Values.map(Values.frozen(Values.plain(bases)));
    }

    /** A catalogue folder: bases.yaml and one folder per adapter holding its manifest.yaml, read in code-point order
     * of the folder names (Python's sorted glob of the manifest.yaml one folder down, which orders by folder name). As in Python, a
     * hidden folder counts, a folder reached through a link is followed, an entry that is not a folder (or a link to
     * nothing) or a folder without a manifest.yaml is passed over, and a manifest.yaml that is a folder or a link to
     * nothing is read, and fails. A folder that cannot be looked into is a ManifestError naming it (Python's glob passes
     * it over silently, which would hide an adapter).
     *
     * @param path the catalogue folder
     * @return the catalogue
     * @throws JeffError.ManifestError when bases.yaml is missing, a folder cannot be looked into, or a manifest breaks
     *     a rule
     * @throws JeffError.ContractError when a manifest does not match its schema */
    public static Catalogue fromDir(Path path) {
        Path basesFile = path.resolve("bases.yaml");
        if (!Files.exists(basesFile)) throw new JeffError.ManifestError(basesFile + " is missing");
        List<String> names = new ArrayList<>();
        try (Stream<Path> entries = Files.list(path)) {
            entries.forEach(entry -> names.add(entry.getFileName().toString()));
        } catch (IOException error) {
            throw new UncheckedIOException("Listing " + path + " failed", error);
        }
        List<Manifest> manifests = new ArrayList<>();
        for (String name : PyFormat.sorted(names)) {
            Path folder = path.resolve(name);
            Path file = folder.resolve("manifest.yaml");
            if (isFolder(folder) && holds(folder, file)) manifests.add(Manifests.load(file));
        }
        return new Catalogue(manifests, basesOf(Yaml.read(basesFile), basesFile.toString()));
    }

    /** Whether the entry is a folder, following a link; a link to nothing is not one. */
    private static boolean isFolder(Path entry) {
        try {
            return Files.readAttributes(entry, BasicFileAttributes.class).isDirectory();
        } catch (NoSuchFileException absent) {
            return false;
        } catch (IOException error) {
            throw new JeffError.ManifestError(entry + " cannot be read, so whether it is an adapter's folder is unknown ("
                + error + ")");
        }
    }

    /** Whether {@code file} is there in {@code folder} (as anything, a link to nothing included, as Python's glob lists it). */
    private static boolean holds(Path folder, Path file) {
        try {
            Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            return true;
        } catch (NoSuchFileException absent) {
            return false;
        } catch (IOException error) {
            throw new JeffError.ManifestError(folder + " cannot be read, so whether it holds an adapter's manifest.yaml is "
                + "unknown (" + error + ")");
        }
    }

    private static Map<String, Object> basesOf(Object document, String where) {
        if (!(document instanceof Map<?, ?> bases)) {
            throw new JeffError.ManifestError(where + " is not a mapping of base IDs to their versions");
        }
        return Values.map(bases);
    }

    /** The catalogue shipped with the package, read once. Every manifest and the bases are read-only throughout, so
     * no caller can change what another sees.
     *
     * @return the shipped catalogue */
    public static Catalogue shipped() {
        SHIPPED_LOCK.lock();
        try {
            if (shipped == null) {
                List<String> folders = new ArrayList<>();
                for (String file : Shipped.files()) {
                    String[] parts = file.split("/");
                    if (parts.length == 3 && parts[0].equals("catalogue") && parts[2].equals("manifest.yaml")) folders.add(parts[1]);
                }
                List<Manifest> manifests = new ArrayList<>();
                for (String folder : PyFormat.sorted(folders)) {
                    // the shipped manifests sit on the class path with no path on disk: messages name them
                    // jeff-shipped/catalogue/..., where Python prints the installed file's absolute path
                    String relative = "catalogue/" + folder + "/manifest.yaml";
                    manifests.add(Manifests.fromMap(Yaml.parse(Shipped.text(relative), Shipped.ROOT + relative), Shipped.ROOT + relative));
                }
                String bases = Shipped.ROOT + "catalogue/bases.yaml";
                shipped = new Catalogue(manifests, basesOf(Yaml.parse(Shipped.text("catalogue/bases.yaml"), bases), bases));
            }
            return shipped;
        } finally {
            SHIPPED_LOCK.unlock();
        }
    }

    /** A catalogue with the same bases, holding these manifests too, each in place of this catalogue's manifest of
     * the same id.
     *
     * @param manifests the manifests to add or put in place
     * @return the new catalogue
     * @throws JeffError.ManifestError when a name ends up used by two adapters */
    public Catalogue replacing(Collection<Manifest> manifests) {
        Map<String, Manifest> merged = new LinkedHashMap<>(byId);
        for (Manifest manifest : manifests) merged.put(manifest.id(), manifest);
        return new Catalogue(merged.values(), bases);
    }

    /** Every adapter id and alias.
     *
     * @return the names */
    public Set<String> names() {
        Set<String> names = new LinkedHashSet<>(byId.keySet());
        names.addAll(byAlias.keySet());
        return Collections.unmodifiableSet(names);
    }

    /** Every manifest, in catalogue order.
     *
     * @return the manifests */
    public List<Manifest> manifests() {
        return List.copyOf(byId.values());
    }

    /** The adapter a reference names.
     *
     * @param ref an adapter id or alias, optionally with a function key and {@code @version}
     * @return its manifest
     * @throws JeffError.InvalidReference when the reference is malformed
     * @throws JeffError.UnknownFunction when no adapter has the name, or it has another version */
    public Manifest adapter(String ref) {
        FunctionRef parsed = FunctionRef.parse(ref);
        String name = byAlias.containsKey(parsed.adapter()) ? byAlias.get(parsed.adapter()) : parsed.adapter();
        Manifest manifest = byId.get(name);
        if (manifest == null) throw new JeffError.UnknownFunction("No adapter " + PyFormat.str(parsed.adapter()) + " in the catalogue");
        if (parsed.version() != null && !FunctionRef.versionMatches(parsed.version(), manifest.version())) {
            throw new JeffError.UnknownFunction(manifest.id() + "@" + parsed.version() + " is not in the catalogue; it has "
                + manifest.version());
        }
        return manifest;
    }

    /** The function a reference names; an adapter with one function is named by the adapter alone.
     *
     * @param ref a function reference
     * @return the function
     * @throws JeffError.InvalidReference when the reference is malformed
     * @throws JeffError.UnknownFunction when there is no such adapter or function, or the adapter has several
     *     functions and the reference names none */
    public FunctionSpec function(String ref) {
        FunctionRef parsed = FunctionRef.parse(ref);
        Manifest manifest = adapter(ref);
        List<String> keys = List.copyOf(manifest.functions().keySet());
        if (parsed.key() == null) {
            // a manifest read by Manifests.fromMap always has a function (the schema's minProperties); one built by
            // hand may not, where Python's next(iter(...)) would crash
            if (keys.isEmpty()) throw new JeffError.UnknownFunction(manifest.id() + " has no functions");
            if (keys.size() != 1) {
                throw new JeffError.UnknownFunction(manifest.id() + " has " + keys.size() + " functions (" + String.join(", ", keys)
                    + "); name one, e.g. " + manifest.id() + "." + keys.get(0));
            }
            return manifest.functions().get(keys.get(0));
        }
        FunctionSpec found = manifest.functions().get(parsed.key());
        if (found == null) {
            throw new JeffError.UnknownFunction(manifest.id() + " has no function " + PyFormat.str(parsed.key()) + "; its functions "
                + "are " + String.join(", ", keys));
        }
        return found;
    }

    /** The manifest a function belongs to.
     *
     * @param fn the function
     * @return its adapter's manifest
     * @throws JeffError.UnknownFunction when its adapter is not in this catalogue */
    public Manifest manifestOf(FunctionSpec fn) {
        Manifest manifest = byId.get(fn.adapterId());
        if (manifest == null) throw new JeffError.UnknownFunction("No adapter " + PyFormat.str(fn.adapterId()) + " in the catalogue");
        return manifest;
    }

    /** The functions that implement an interface, of every adapter that is not a router, in catalogue order.
     *
     * @param iface the interface ({@code classify}, {@code route}, {@code rate}, ...)
     * @return the functions */
    public List<FunctionSpec> implementing(String iface) {
        List<FunctionSpec> out = new ArrayList<>();
        for (Manifest manifest : byId.values()) {
            if (manifest.isRouter()) continue;
            for (FunctionSpec fn : manifest.functions().values()) if (fn.interfaceName().equals(iface)) out.add(fn);
        }
        return Collections.unmodifiableList(out);
    }

    /** Every base id and version bases.yaml lists.
     *
     * @return {@code [base id, version]} pairs, in bases.yaml's order
     * @throws JeffError.ManifestError when a base does not hold a mapping of its versions */
    public List<List<String>> baseVersions() {
        List<List<String>> out = new ArrayList<>();
        for (Map.Entry<String, Object> base : bases.entrySet()) {
            if (!(base.getValue() instanceof Map<?, ?> versions)) {
                throw new JeffError.ManifestError("bases.yaml: base " + base.getKey() + " holds " + PyFormat.repr(base.getValue())
                    + ", not a mapping of its versions");
            }
            for (Object version : versions.keySet()) out.add(List.of(base.getKey(), (String) version));
        }
        return Collections.unmodifiableList(out);
    }

    /** One base version's settings from bases.yaml.
     *
     * @param baseId the base's id
     * @param version its version
     * @return the settings
     * @throws JeffError.ManifestError when bases.yaml does not list it, or it is not a mapping */
    public Map<String, Object> base(String baseId, String version) {
        if (!(bases.get(baseId) instanceof Map<?, ?> versions) || versions.isEmpty() || !versions.containsKey(version)) {
            throw new JeffError.ManifestError("Base " + baseId + " " + version + " is not in bases.yaml (known: "
                + PyFormat.repr(bases) + ")");
        }
        if (!(versions.get(version) instanceof Map<?, ?> block)) {
            throw new JeffError.ManifestError("bases.yaml: base " + baseId + " " + version + " holds "
                + PyFormat.repr(versions.get(version)) + ", not a mapping");
        }
        return Values.map(block);
    }
}
