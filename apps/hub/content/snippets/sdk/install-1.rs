// offline
use jeff_sdk::Catalogue;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let catalogue = Catalogue::shipped()?;

    println!("{} adapters in the catalogue shipped with jeff-sdk", catalogue.manifests().len());

    Ok(())
}
