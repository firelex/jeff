package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.fail;

import java.io.IOException;
import java.lang.reflect.Constructor;
import java.lang.reflect.Executable;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.Type;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/** The package's public surface is pinned in src/test/resources/public-api.txt: every public type, and every public or
 * protected constructor, method and field of it (a protected member counts only where a caller can subclass the type).
 * A change to the surface fails here until the file is updated with it, so no helper becomes public by mistake and
 * nothing public disappears unnoticed. */
class SurfaceTest {
    private static final Path PINNED = Path.of("src/test/resources/public-api.txt");
    private static final Path ACTUAL = Path.of("build/public-api.actual.txt");
    private static final Pattern SDK_TYPE = Pattern.compile("ai\\.jeff\\.sdk\\.[A-Za-z0-9_.$]+");

    @Test
    void thePackageExportsExactlyThePinnedSurface() throws IOException, ClassNotFoundException, URISyntaxException {
        List<String> actual = surface();
        List<String> expected = Files.readAllLines(PINNED, StandardCharsets.UTF_8).stream().filter(line -> !line.isEmpty()).toList();
        if (actual.equals(expected)) return;
        Files.createDirectories(ACTUAL.getParent());
        Files.write(ACTUAL, actual, StandardCharsets.UTF_8);
        List<String> added = actual.stream().filter(line -> !expected.contains(line)).toList();
        List<String> removed = expected.stream().filter(line -> !actual.contains(line)).toList();
        fail("The public surface differs from " + PINNED + ".\nAdded:\n  " + String.join("\n  ", added) + "\nRemoved:\n  "
            + String.join("\n  ", removed) + "\nThe whole surface as built is in " + ACTUAL
            + "; copy it over the pinned file if the change is deliberate.");
    }

    /** No public signature names a type a caller cannot see, so nothing public leaks an internal helper. */
    @Test
    void noPublicMemberNamesAPackagePrivateType() throws IOException, ClassNotFoundException, URISyntaxException {
        Set<String> publicTypes = new LinkedHashSet<>();
        for (Class<?> type : publicTypes()) publicTypes.add(type.getName());
        List<String> leaks = new ArrayList<>();
        for (String line : surface()) {
            Matcher matcher = SDK_TYPE.matcher(line);
            while (matcher.find()) {
                String name = matcher.group();
                if (!publicTypes.contains(name) && !isMemberReference(name, publicTypes)) leaks.add(line + "   <- " + name);
            }
        }
        assertEquals(List.of(), leaks);
    }

    /** The helpers named in the final reviews of the other SDKs stay out of the surface, the tests' proxy pin
     * included (it lives in Proxies, package-private). */
    @Test
    void theInternalHelpersAreNotPublic() throws ClassNotFoundException {
        for (String name : List.of("PyFormat", "PyText", "PyBase64", "MsgPack", "Protocol", "Yaml", "Json", "Values", "Nesting",
            "Proxies", "Transport", "ChannelTransport", "WebSocketConnection", "HttpConnection", "Http1Client",
            "HttpBody", "HttpOptions", "ConnectionPool", "ServerUrl", "Contract", "Shipped", "Binding", "Escalation",
            "Routing", "PhaseTimer", "PythonReSyntax", "ServedFunctions")) {
            Class<?> type = Class.forName("ai.jeff.sdk." + name);
            assertFalse(Modifier.isPublic(type.getModifiers()), name + " is public");
        }
        assertEquals("jeff.core.router", Jeff.DEFAULT_ROUTER);
    }

    private static boolean isMemberReference(String name, Set<String> publicTypes) {
        // `ai.jeff.sdk.Jeff.local` in a method line is the method's own name, after its owner type
        int dot = name.lastIndexOf('.');
        return dot > 0 && publicTypes.contains(name.substring(0, dot));
    }

    /** Every public type of ai.jeff.sdk and ai.jeff.sdk.testing whose enclosing types are public too. */
    static List<Class<?>> publicTypes() throws IOException, ClassNotFoundException, URISyntaxException {
        Path root = Path.of(Jeff.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        List<String> names;
        try (Stream<Path> files = Files.walk(root)) {
            names = files.filter(file -> file.toString().endsWith(".class"))
                .map(file -> root.relativize(file).toString().replace('/', '.').replaceAll("\\.class$", ""))
                .filter(name -> name.startsWith("ai.jeff.sdk.")).sorted().toList();
        }
        List<Class<?>> out = new ArrayList<>();
        for (String name : names) {
            Class<?> type = Class.forName(name, false, SurfaceTest.class.getClassLoader());
            if (type.isAnonymousClass() || type.isLocalClass() || type.isSynthetic()) continue;
            boolean visible = true;
            for (Class<?> at = type; at != null; at = at.getEnclosingClass()) visible &= Modifier.isPublic(at.getModifiers());
            if (visible) out.add(type);
        }
        out.sort((a, b) -> a.getName().compareTo(b.getName()));
        return out;
    }

    /** One line per public type, then one per public or protected member of it, each sorted. */
    static List<String> surface() throws IOException, ClassNotFoundException, URISyntaxException {
        List<String> lines = new ArrayList<>();
        for (Class<?> type : publicTypes()) {
            lines.add(head(type));
            boolean subclassable = !Modifier.isFinal(type.getModifiers()) && !type.isSealed() && !type.isRecord() && !type.isEnum()
                && Arrays.stream(type.getDeclaredConstructors()).anyMatch(SurfaceTest::reachable);
            List<String> members = new ArrayList<>();
            for (Constructor<?> constructor : type.getDeclaredConstructors()) {
                if (shown(constructor, subclassable)) members.add("  " + constructor.toGenericString());
            }
            for (Method method : type.getDeclaredMethods()) {
                if (!method.isSynthetic() && !method.isBridge() && shown(method, subclassable)) members.add("  " + method.toGenericString());
            }
            for (Field field : type.getDeclaredFields()) {
                if (!field.isSynthetic() && shown(field.getModifiers(), subclassable)) members.add("  " + field.toGenericString());
            }
            members.sort(null);
            lines.addAll(members);
        }
        return lines;
    }

    private static boolean reachable(Executable executable) {
        return Modifier.isPublic(executable.getModifiers()) || Modifier.isProtected(executable.getModifiers());
    }

    private static boolean shown(Executable executable, boolean subclassable) {
        return shown(executable.getModifiers(), subclassable);
    }

    private static boolean shown(int modifiers, boolean subclassable) {
        return Modifier.isPublic(modifiers) || subclassable && Modifier.isProtected(modifiers);
    }

    private static String head(Class<?> type) {
        StringBuilder out = new StringBuilder(type.isInterface() ? "interface " : type.isRecord() ? "record " : type.isEnum() ? "enum " : "class ");
        out.append(type.getName());
        if (type.isSealed()) out.append(" sealed");
        else if (Modifier.isFinal(type.getModifiers()) && !type.isRecord() && !type.isEnum()) out.append(" final");
        else if (Modifier.isAbstract(type.getModifiers()) && !type.isInterface()) out.append(" abstract");
        Type parent = type.getGenericSuperclass();
        if (parent != null && parent != Object.class && parent != Record.class && !type.isEnum()) out.append(" extends ").append(parent.getTypeName());
        List<String> interfaces = Arrays.stream(type.getGenericInterfaces()).map(Type::getTypeName).sorted().toList();
        if (!interfaces.isEmpty()) out.append(type.isInterface() ? " extends " : " implements ").append(String.join(", ", interfaces));
        return out.toString();
    }
}
