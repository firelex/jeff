use jeff_sdk::{BoxError, CallOptions, Jeff, RemoteOptions, TokenSource};
use serde_json::json;

// Called before every request, so a token refreshed here (read again from the environment, or fetched from your
// secret store) is used from the next request on.
async fn current_token() -> Result<String, BoxError> {
    std::env::var("JEFF_API_KEY").map_err(|_| BoxError::from("set JEFF_API_KEY"))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let jeff = Jeff::remote("{{origin}}", TokenSource::function(current_token), RemoteOptions::new(30_000)).await?;

    let options = CallOptions {
        context: Some(serde_json::from_value(json!({
            "application": "A support chat assistant for an online shop.",
            "source": "user message",
        }))?),
        ..CallOptions::default()
    };

    let guard = jeff.function("guard.attempt", options).await?;

    println!("{:?}", guard.call(json!("What are your opening hours?")).await?.value);

    jeff.close().await;

    Ok(())
}
