// offline
use jeff_sdk::{ErrorType, Jeff, LocalOptions, RemoteOptions, TokenSource};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Both mistakes are refused at once, before any network call or connection.
    match Jeff::remote("{{origin}}", TokenSource::None, RemoteOptions::new(0)).await {
        Err(error) if error.kind() == ErrorType::InvalidRequest => println!("{error}"),
        Err(error) => return Err(error.into()),
        Ok(_) => return Err("a timeout of 0 was accepted".into()),
    }

    match Jeff::local("/tmp/jeff.sock", LocalOptions::new(0)).await {
        Err(error) if error.kind() == ErrorType::InvalidRequest => println!("{error}"),
        Err(error) => return Err(error.into()),
        Ok(_) => return Err("a timeout of 0 was accepted".into()),
    }

    Ok(())
}
