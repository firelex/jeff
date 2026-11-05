use jeff_sdk::{Jeff, Labels, LocalOptions, ShorthandOptions};
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let shop: jeff_sdk::Context = serde_json::from_value(json!({"company": "A furniture shop.", "channel": "email"}))?;

    let message = json!("My wardrobe arrived with a broken door and I need it replaced before Friday.");

    let jeff = Jeff::local("/tmp/jeff.sock", LocalOptions::new(30_000)).await?;

    // Names the function to call, with the shop's context.
    let with = |function: &str| ShorthandOptions {
        function: Some(function.to_string()),
        context: Some(shop.clone()),
        ..ShorthandOptions::default()
    };

    let team = jeff.route(message.clone(), ShorthandOptions {
        labels: Some(Labels::from_json(&json!({
            "deliveries": "Deliveries and damaged goods",
            "refunds": "Refunds and returns",
        }))?),
        ..with("triage.route")
    }).await?;

    let urgency = jeff.rate(message.clone(), with("triage.urgency")).await?;

    let needs_human = jeff.requires_escalation(message.clone(), with("triage.needs_human")).await?;

    let kind = jeff.classify(message, ShorthandOptions {
        function: Some("guard.kind".to_string()),
        context: Some(serde_json::from_value(json!({
            "application": "A support assistant for a furniture shop.",
            "source": "email body",
        }))?),
        ..ShorthandOptions::default()
    }).await?;

    println!("team: {:?}", team.value);

    println!("urgency level: {:?} expected level: {:?}", urgency.value, urgency.score);

    println!("a person must act now: {:?}", needs_human.value);

    println!("kind of text: {:?}", kind.value);

    jeff.close().await;

    Ok(())
}
