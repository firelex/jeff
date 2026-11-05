package ai.jeff.sdk;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;

/** What the package carries from the repository: the contract's schemas, interfaces.yaml and errors.yaml, and the
 * catalogue, copied into src/main/resources/jeff-shipped by `./gradlew syncShipped`, with index.txt listing every
 * file (a class path cannot be listed like a folder). */
final class Shipped {
    /** What is copied, as paths relative to the repository root. */
    public static final List<String> ITEMS =
        List.of("contract/schemas", "contract/interfaces.yaml", "contract/errors.yaml", "catalogue");
    /** Where the copy sits on the class path. */
    public static final String ROOT = "jeff-shipped/";

    private Shipped() {}

    /** Every shipped file's path relative to the repository root, sorted. */
    public static List<String> files() {
        return text("index.txt").lines().filter(line -> !line.isEmpty()).toList();
    }

    public static byte[] bytes(String relative) {
        String name = ROOT + relative;
        try (InputStream in = Shipped.class.getClassLoader().getResourceAsStream(name)) {
            if (in == null) {
                throw new IllegalStateException("The shipped file " + name + " is missing from the class path; run "
                    + "./gradlew syncShipped");
            }
            return in.readAllBytes();
        } catch (IOException error) {
            throw new UncheckedIOException("Reading the shipped file " + name + " failed", error);
        }
    }

    public static String text(String relative) {
        return new String(bytes(relative), StandardCharsets.UTF_8);
    }

    /** The repository root, for the tests (build.gradle.kts sets the system property jeff.repository); an
     * installed package never uses it. */
    public static Path repositoryRoot() {
        String root = System.getProperty("jeff.repository");
        if (root == null) {
            throw new IllegalStateException("The system property jeff.repository is not set; run the tests with "
                + "./gradlew test, which sets it to the repository root");
        }
        return Path.of(root);
    }
}
