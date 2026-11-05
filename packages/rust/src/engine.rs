//! What the decision logic needs from an inference engine (port of jeff/engine.py): the engine socket
//! (SocketEngine), a Jeff server's listing (ServedFunctions) or a test engine (FakeEngine).

use std::collections::BTreeSet;

use async_trait::async_trait;

use crate::binding::Row;
use crate::errors::{ErrorType, JeffError, Result};
use crate::json::{Json, JsonMap};
use crate::pyformat::py_repr;

/// The format name of a bfloat16 base.
pub const BF16: &str = "bf16";

/// The base's weight format: bfloat16, or one exact set of INT4 weights named by its format_id.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BaseFormat {
    /// The format name: `bf16`, or an INT4 format.
    pub format: String,
    /// The exact set of INT4 weights; none for a bfloat16 base.
    pub format_id: Option<String>,
}

impl BaseFormat {
    /// The bfloat16 base format.
    pub fn bf16() -> BaseFormat {
        BaseFormat {
            format: BF16.into(),
            format_id: None,
        }
    }

    /// `{"format"}` or `{"format", "format_id"}`, as the wire writes it.
    pub fn to_json(&self) -> Json {
        let mut map = JsonMap::new();
        map.insert("format".into(), Json::from(self.format.as_str()));
        if let Some(id) = &self.format_id {
            map.insert("format_id".into(), Json::from(id.as_str()));
        }
        Json::Object(map)
    }

    /// The base format a `{"format", "format_id"}` object describes. Raises ContractError when `format` is
    /// not a string or `format_id` is present and not a string.
    pub fn from_json(value: &Json) -> Result<BaseFormat> {
        let format = value.get("format").and_then(Json::as_str).ok_or_else(|| {
            JeffError::new(
                ErrorType::ContractError,
                format!(
                    "A base format needs a 'format' string; got {}",
                    py_repr(value)
                ),
            )
        })?;
        let format_id = match value.get("format_id") {
            None => None,
            Some(Json::String(id)) => Some(id.clone()),
            Some(other) => {
                return Err(JeffError::new(
                    ErrorType::ContractError,
                    format!(
                        "A base format's format_id must be a string; got {}",
                        py_repr(other)
                    ),
                ));
            }
        };
        Ok(BaseFormat {
            format: format.to_owned(),
            format_id,
        })
    }
}

/// None for a bfloat16 base (functions decide with `calibration`); for any other format its format_id.
pub fn format_id_of(base_format: &BaseFormat) -> Result<Option<String>> {
    let shown = py_repr(&base_format.to_json());
    if base_format.format == BF16 {
        if base_format.format_id.is_some() {
            return Err(JeffError::new(
                ErrorType::EngineError,
                format!("The engine's base format {shown} is bfloat16 but carries a format_id"),
            ));
        }
        return Ok(None);
    }
    match &base_format.format_id {
        Some(id) => Ok(Some(id.clone())),
        None => Err(JeffError::new(
            ErrorType::EngineError,
            format!(
                "The engine's base format {shown} names no format_id; every base format other than {BF16} is one exact set of \
             weights, named by its format_id"
            ),
        )),
    }
}

/// An engine's answer to one predict.
#[derive(Clone, Debug, PartialEq)]
pub struct EngineResult {
    /// One list per row, over the row's options in order.
    pub probabilities: Vec<Vec<f64>>,
    /// The engine's timing for the predict (for example `path` and `model_ms`).
    pub timing: JsonMap,
    /// Each row's prompt length in tokens, when the engine reports it.
    pub input_tokens: Option<Vec<u64>>,
}

/// What the decision logic needs from an inference engine. [`SocketEngine`](crate::SocketEngine) is the
/// real one and [`FakeEngine`](crate::testing::FakeEngine) a test one; implement it to plug in another.
#[async_trait]
pub trait Engine: Send + Sync {
    /// The base's weight format, read once when the engine connects.
    fn base_format(&self) -> BaseFormat;
    /// The adapter ids loaded; a cached copy for a remote engine, asked again with `refresh`.
    async fn installed(&self, refresh: bool) -> Result<BTreeSet<String>>;
    /// The probabilities of each row's options under the adapter `adapter_id`, one list per row in order. An
    /// engine that cannot answer raises a JeffError (EngineError for a failed engine).
    async fn predict(&self, adapter_id: &str, rows: &[Row]) -> Result<EngineResult>;
    /// Ends the engine's connection; later calls fail. Nothing to do for an engine without one.
    async fn close(&self) {}
}

/// Whether the engine serves the adapter: a cached list missing it is asked once more, with refresh.
pub(crate) async fn serves(engine: &dyn Engine, adapter_id: &str) -> Result<bool> {
    Ok(engine.installed(false).await?.contains(adapter_id)
        || engine.installed(true).await?.contains(adapter_id))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct Listing {
        cached: BTreeSet<String>,
        fresh: BTreeSet<String>,
        asked: Mutex<Vec<bool>>,
    }

    #[async_trait]
    impl Engine for Listing {
        fn base_format(&self) -> BaseFormat {
            BaseFormat::bf16()
        }

        async fn installed(&self, refresh: bool) -> Result<BTreeSet<String>> {
            self.asked.lock().unwrap().push(refresh);
            Ok(if refresh {
                self.fresh.clone()
            } else {
                self.cached.clone()
            })
        }

        async fn predict(&self, adapter_id: &str, _rows: &[Row]) -> Result<EngineResult> {
            panic!("predict({adapter_id}) is not part of this test")
        }
    }

    #[tokio::test]
    async fn serves_asks_a_cached_listing_once_more_with_refresh() {
        let engine = Listing {
            cached: BTreeSet::from(["jeff.safety.spam".to_owned()]),
            fresh: BTreeSet::from([
                "jeff.safety.spam".to_owned(),
                "jeff.support.triage".to_owned(),
            ]),
            asked: Mutex::new(Vec::new()),
        };
        assert!(serves(&engine, "jeff.safety.spam").await.unwrap());
        assert_eq!(*engine.asked.lock().unwrap(), [false]);
        assert!(serves(&engine, "jeff.support.triage").await.unwrap());
        assert!(!serves(&engine, "jeff.safety.guard").await.unwrap());
        assert_eq!(
            *engine.asked.lock().unwrap(),
            [false, false, true, false, true]
        );
    }
}
