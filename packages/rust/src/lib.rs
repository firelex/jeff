//! The Jeff SDK for Rust: bind a decision function, call it, read a typed `Decision`, route a call to the right
//! function, escalate when Jeff is unsure, and run decision graphs, against a Jeff engine on this machine (over its
//! Unix socket) or a Jeff server (over HTTP or its WebSocket channel). The Python SDK is the reference: this crate
//! replays every file of contract/conformance/ exactly as the Python SDK recorded it. See README.md.
//!
//! Feature `testing` (off by default): `testing::pin_proxy_environment`, a test-only helper that changes how every
//! connection in the process picks its proxy. The rest of `testing` (FakeEngine, the golden comparison) is always on.

#![warn(missing_docs)]

pub mod binding;
pub mod catalogue;
pub mod channel;
pub mod client;
mod content_coding;
pub mod contract;
pub mod decision;
pub mod engine;
pub mod errors;
pub mod escalation;
pub mod graph;
mod http;
pub mod ids;
pub mod json;
pub mod manifest;
pub mod msgpack;
pub mod protocol;
mod proxy;
pub mod pyformat;
pub mod remote;
pub mod routing;
pub mod shipped;
pub mod socket_engine;
pub mod testing;
mod tls;
pub mod yaml;

pub use binding::{Call, Context, Labels};
pub use catalogue::Catalogue;
pub use channel::ChannelTransport;
pub use client::{
    BoundFunction, CallOptions, DispatchOptions, Jeff, LocalOptions, ShorthandOptions,
};
pub use decision::{Decision, EscalationRecord, RouteStep, Value};
pub use engine::{BaseFormat, Engine, EngineResult};
pub use errors::{BoxError, ErrorType, JeffError, Result};
/// The escalation step [`escalation::remote`], re-exported under another name so that `jeff_sdk::remote` means only
/// the module of the remote client.
pub use escalation::remote as remote_step;
pub use escalation::{Policy, Question, QuestionState, Step, escalate, function_step, human, llm};
pub use graph::{
    CodeCall, CodeFunction, Graph, GraphResult, GraphSource, NodeResult, code_function,
};
pub use json::{Json, JsonMap};
pub use remote::{RemoteOptions, RestTransport, TokenSource, Transport};
pub use socket_engine::SocketEngine;
