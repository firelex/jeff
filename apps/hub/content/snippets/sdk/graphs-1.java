// offline
import ai.jeff.sdk.*;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

public class LoadGraph {
    public static void main(String[] args) throws IOException {
        Graph graph = Graphs.load(Path.of("graphs-message-check.yaml"));

        System.out.println(graph.id() + " " + graph.version() + " " + graph.params());

        System.out.println(graph.order()); // every node after the nodes it depends on

        // A graph that refers to a node it does not have is refused when it is loaded.
        Path broken = Path.of("graphs-broken.yaml");

        Files.writeString(broken, Files.readString(Path.of("graphs-message-check.yaml")).replace("after: [attack]", "after: [atack]"));

        try {
            Graphs.load(broken);
        } catch (JeffError.GraphError error) {
            System.out.println(error.getMessage());
        } finally {
            Files.delete(broken);
        }
    }
}
