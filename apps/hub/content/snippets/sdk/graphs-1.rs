// offline
use std::path::Path;

use jeff_sdk::ErrorType;
use jeff_sdk::graph::load_graph;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let graph = load_graph(Path::new("graphs-message-check.yaml"))?;

    println!("{} {} {:?}", graph.id, graph.version, graph.params);

    println!("{:?}", graph.order); // every node after the nodes it depends on

    // A graph that refers to a node it does not have is refused when it is loaded.
    let broken = Path::new("graphs-broken.yaml");

    std::fs::write(broken, std::fs::read_to_string("graphs-message-check.yaml")?.replace("after: [attack]", "after: [atack]"))?;

    let loaded = load_graph(broken);

    std::fs::remove_file(broken)?;

    match loaded {
        Err(error) if error.kind() == ErrorType::GraphError => println!("{error}"),
        Err(error) => return Err(error.into()),
        Ok(_) => return Err("the broken graph was accepted".into()),
    }

    Ok(())
}
