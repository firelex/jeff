// offline
use jeff_sdk::Catalogue;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let catalogue = Catalogue::shipped()?;

    for interface in ["classify", "route", "rate", "detect", "is_supported", "equivalent", "requires_escalation"] {
        let functions: Vec<String> = catalogue.implementing(interface).iter().map(|function| function.id.clone()).collect();

        println!("{interface}: {} functions, for example {}", functions.len(), functions[0]);
    }

    Ok(())
}
