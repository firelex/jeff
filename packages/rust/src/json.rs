//! The JSON values the SDK passes around: inputs, context values, label descriptions, rows, decisions. Objects keep
//! their insertion order (serde_json's preserve_order), so a key that looks like a number stays where it was.

/// Any JSON value.
pub type Json = serde_json::Value;
/// A JSON object, in insertion order. Remove keys with `shift_remove`, never `remove` (which moves the last key).
pub type JsonMap = serde_json::Map<String, Json>;
