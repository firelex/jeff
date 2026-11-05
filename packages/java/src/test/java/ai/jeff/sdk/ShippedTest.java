package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

class ShippedTest {
    @Test
    void theShippedCopyMatchesTheRepository() throws IOException {
        Path root = Shipped.repositoryRoot();
        List<String> expected = new ArrayList<>();
        for (String item : Shipped.ITEMS) {
            try (Stream<Path> files = Files.walk(root.resolve(item))) {
                files.filter(Files::isRegularFile)
                    .forEach(file -> expected.add(root.relativize(file).toString().replace('\\', '/')));
            }
        }
        Collections.sort(expected);
        assertEquals(expected, Shipped.files(), "the shipped copy is stale; run: ./gradlew syncShipped");
        for (String name : expected) {
            assertArrayEquals(Files.readAllBytes(root.resolve(name)), Shipped.bytes(name), name);
        }
    }

    @Test
    void aMissingShippedFileNamesItselfAndTheSyncTask() {
        IllegalStateException error = org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class,
            () -> Shipped.bytes("contract/no-such-file.yaml"));
        assertEquals("The shipped file jeff-shipped/contract/no-such-file.yaml is missing from the class path; run "
            + "./gradlew syncShipped", error.getMessage());
    }
}
