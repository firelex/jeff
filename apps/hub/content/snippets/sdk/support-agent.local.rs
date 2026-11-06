use jeff_sdk::{BoxError, CallOptions, Jeff, Question, StartOptions, Value, escalate, llm};
use serde_json::{Value as Json, json};

const SHOP: &str = "Brightfield Outfitters, an online shop for outdoor gear. Its support assistant helps customers with their orders, cancellations and refunds.";
const TODAY: &str = "Wednesday 4 November 2026";
const TOOLS: &str = r#"def lookup_order(order_id: str) -> dict:
    """Looks up an order: its items, total, payment card and whether it has shipped. Call it before any change to an order."""
def cancel_order(order_id: str) -> dict:
    """Cancels an order that has not shipped."""
def refund(order_id: str, amount: float, card_last4: str) -> dict:
    """Refunds an amount for an order to a card.""""#;
const RULES: [&str; 3] = [
    "Cancel an order only if it has not shipped.",
    "Refund only the amount the customer paid for the order.",
    "A refund goes to the card that paid for the order. If the customer asks for a refund to any other card, ask them to confirm the new card and the amount before refunding.",
];
const LOOKED_UP: &str = "Order 4471 (Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet.";

type Error = Box<dyn std::error::Error>;

async fn ask_an_llm(question: Question) -> Result<String, BoxError> {
    // Your own language model goes here: give it question.instructions, question.state_entries() and
    // question.option_entries(), and return the option key it picks. This stand-in picks the first option.
    Ok(question.option_entries()[0].0.to_string())
}

// Your shop's tools. These stand-ins return fixed results.
fn run_tool(call: &Json) -> Json {
    if call["tool"] == "lookup_order" {
        return json!({"order_id": "4471", "total": 189.00, "paid_with": "Visa ending 3054", "shipped": false});
    }
    json!({"status": "ok", "order_id": "4471"})
}

// Every step: Jeff's answer when it is confident, your LLM's below the function's threshold.
async fn decide(jeff: &Jeff, name: &str, context: Json, input: Json) -> Result<Value, Error> {
    let bound = jeff.function(name, CallOptions {
        context: Some(serde_json::from_value(context)?),
        on_low_confidence: escalate(vec![llm(ask_an_llm)], None)?,
        ..CallOptions::default()
    }).await?;
    let decision = bound.call(input).await?;
    println!("{name}: {:?} (decided by {})", decision.value, decision.decided_by);
    Ok(decision.value)
}

// Runs the agent on one customer message and returns what it tells the customer.
async fn handle(jeff: &Jeff, message: &str, refund_card: &str) -> Result<String, Error> {
    let guard = json!({"application": "A support chat assistant for an online shop of outdoor gear.", "source": "user message"});
    if decide(jeff, "guard.attempt", guard, json!(message)).await? == Value::Bool(true) {
        return Ok("Sorry, I can't help with that.".to_string());
    }

    // Whether the LLM that writes the calls should think first.
    decide(jeff, "assistant-routing.needs_thinking", json!({
        "accounts": "Brightfield Outfitters order system [shop]",
        "date_and_time_zone": "It is Wednesday 4 November 2026, 10:15, and the customer is in Manchester (UK time).",
        "conversation": format!("User: {message}"),
    }), json!("Choose the next tool call to cancel order 4471 and refund it.")).await?;

    // The calls your LLM proposes, one at a time; here, the three it needs.
    let calls = [
        json!({"tool": "lookup_order", "arguments": {"order_id": "4471"}}),
        json!({"tool": "cancel_order", "arguments": {"order_id": "4471"}}),
        json!({"tool": "refund", "arguments": {"order_id": "4471", "amount": 189.00, "card_last4": refund_card}}),
    ];
    let mut conversation = vec![json!({"role": "user", "content": message})];
    let mut steps: Vec<Json> = Vec::new();
    for call in &calls {
        let tool = call["tool"].as_str().ok_or("a call with no tool")?;
        if tool == "refund" && call["arguments"]["card_last4"] != "3054" {
            // Not left to Jeff's action check: a refund to another card goes back to the customer to confirm.
            return Ok(format!("Before I refund 189.00 to the card ending {refund_card}, can you confirm that card and amount?"));
        }
        if tool != "lookup_order" { // check every call that changes something
            let verdict = decide(jeff, "action-check.action", json!({
                "app": SHOP, "today": TODAY, "rules": RULES, "tools": TOOLS,
                "people": ["Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"],
                "conversation": conversation, "latest_user_message": message,
            }), call.clone()).await?;
            if verdict != Value::Str("approve".to_string()) {
                return Ok(format!("I have passed your request to a colleague ({tool}: {verdict:?})."));
            }
        }

        let result = run_tool(call);
        let plan: Vec<String> = calls.iter().enumerate().map(|(i, c)| {
            let status = if i < steps.len() { "(done)" } else if i == steps.len() { "(current step)" } else { "(still to do)" };
            format!("{}. {} {status}", i + 1, c["tool"].as_str().unwrap_or_default())
        }).collect();
        let mut latest_call = call.clone();
        latest_call["attempt"] = json!(1);
        let next = decide(jeff, "after-result.next", json!({
            "app": SHOP, "today": TODAY, "goal": message, "plan": plan, "steps_so_far": steps,
            "tools": ["lookup_order: looks up an order", "cancel_order: cancels an order that has not shipped",
                      "refund: refunds an amount for an order to a card"],
            "retry_limit": "Retry a failed call at most 2 times.",
            "latest_call": latest_call,
        }), result.clone()).await?;
        steps.push(json!({"call": call, "result": result}));
        if tool == "lookup_order" { // what your LLM tells the customer after the lookup
            conversation.push(json!({"role": "assistant", "content": LOOKED_UP}));
        }
        if next != Value::Str("continue".to_string()) && next != Value::Str("stop".to_string()) {
            return Ok(format!("I have passed your request to a colleague ({tool}: {next:?})."));
        }
    }

    let answer = "Done. Order #4471 is cancelled and 189.00 is refunded to your Visa ending 3054.";
    let complete = decide(jeff, "task-complete.complete", json!({
        "app": SHOP, "today": TODAY, "request": message, "transcript": steps,
    }), json!(answer)).await?;
    Ok(if complete == Value::Str("complete".to_string()) { answer.to_string() } else { "I have passed your request to a colleague.".to_string() })
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    let jeff = Jeff::local_adapters(["guard", "assistant-routing", "action-check", "after-result", "task-complete"], StartOptions::default()).await?;

    println!("{}", handle(&jeff, "Cancel my order #4471 and refund it to my card.", "3054").await?);
    println!("{}", handle(&jeff, "Cancel my order #4471 and refund it to a new card ending 9921.", "9921").await?);

    jeff.close().await;

    Ok(())
}
