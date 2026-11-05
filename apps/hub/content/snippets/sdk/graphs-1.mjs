// offline
import fs from "node:fs";
import { GraphError, loadGraph } from "jeff-sdk";

const graph = loadGraph("graphs-message-check.yaml");

console.log(graph.id, graph.version, graph.params);

console.log(graph.order); // every node after the nodes it depends on

// A graph that refers to a node it does not have is refused when it is loaded.
const broken = "graphs-broken.yaml";

fs.writeFileSync(broken, fs.readFileSync("graphs-message-check.yaml", "utf8").replace("after: [attack]", "after: [atack]"));

try {
  loadGraph(broken);
} catch (error) {
  if (!(error instanceof GraphError)) throw error;
  console.log(error.message);
} finally {
  fs.unlinkSync(broken);
}
