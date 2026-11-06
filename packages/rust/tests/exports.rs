//! The crate root names the whole SDK surface (a compile-time check: this file builds only if every name exists).

use jeff_sdk::testing::{FakeEngine, golden_problems};
#[allow(unused_imports)]
use jeff_sdk::{
    BaseFormat, BoundFunction, BoxError, Call, CallOptions, Catalogue, ChannelTransport, CodeCall,
    CodeFunction, Context, Decision, DispatchOptions, Engine, EngineResult, ErrorType,
    EscalationRecord, Graph, GraphResult, GraphSource, Jeff, JeffError, Json, JsonMap, Labels,
    LocalOptions, NodeResult, Policy, Question, QuestionState, RemoteOptions, RestTransport,
    Result, RouteStep, ShorthandOptions, SocketEngine, StartOptions, Step, TokenSource, Transport,
    Value, code_function, escalate, function_step, human, llm, remote_step,
};
#[allow(unused_imports)]
use jeff_sdk::{binding, contract, manifest, msgpack, protocol, pyformat, routing, testing, yaml};

#[test]
fn the_crate_root_names_the_sdk_surface() {
    assert_eq!(jeff_sdk::routing::DEFAULT_ROUTER, "jeff.core.router");
    let limit = JeffError::new(ErrorType::UsageLimitReached, "m");
    let _ = (
        limit.account(),
        limit.used(),
        limit.allowance(),
        limit.period_end(),
    );
    let defaults = StartOptions::default();
    assert_eq!(
        (defaults.timeout_ms, defaults.start_timeout_ms),
        (60_000, 1_800_000)
    );
    let _ = (
        FakeEngine::new(|_, _| vec![], Vec::<String>::new()),
        golden_problems as fn(_, _, _) -> _,
    );
}
