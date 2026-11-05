# offline
from pathlib import Path

import jeff

graph = jeff.load_graph(Path("graphs-message-check.yaml"))

print(graph.id, graph.version, graph.params)

print(graph.order)  # every node after the nodes it depends on

# A graph that refers to a node it does not have is refused when it is loaded.
broken = Path("graphs-broken.yaml")

broken.write_text(Path("graphs-message-check.yaml").read_text().replace("after: [attack]", "after: [atack]"))

try:
    jeff.load_graph(broken)
except jeff.GraphError as error:
    print(error)
finally:
    broken.unlink()
