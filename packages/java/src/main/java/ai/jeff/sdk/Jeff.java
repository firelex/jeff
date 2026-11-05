package ai.jeff.sdk;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;

/** The Jeff client (port of jeff/client.py): bind functions, decide in batches grouped by adapter, call through the
 * interface shorthands, dispatch through routers and run decision graphs. Make one with {@link #local} (an engine on
 * this machine), {@link #remote(String, String, RemoteOptions)} (a Jeff server) or {@link #create} (any
 * {@link Engine}). Blocking methods throw {@link JeffError}; each has an {@code ...Async} twin returning a
 * {@code CompletableFuture} (wait for one with {@link Async#join}). Safe to use from many threads at once. */
public class Jeff implements Decider, AutoCloseable {
    /** The router a dispatch starts from when none is named. */
    public static final String DEFAULT_ROUTER = "jeff.core.router";

    /** A call and its binding. */
    record Bound(Call call, Binding binding) {}

    /** What a Jeff needs, read once from its engine: the base format (picks each function's calibration) and the
     * exact keys of every installed router. */
    record Parts(Engine engine, Catalogue catalogue, String formatId, Map<String, Map<String, String>> exactKeys) {}

    static Parts parts(Engine engine, Catalogue catalogue) {
        String formatId = BaseFormat.formatIdOf(engine.baseFormat());
        return new Parts(engine, catalogue, formatId, Routing.checkRouters(catalogue, engine.installed()));
    }

    private final Engine engine;
    private final Catalogue catalogue;
    private final BaseFormat baseFormat;
    private final String formatId;
    private final Map<String, Map<String, String>> exactKeys;
    /** Function ID -> its calibration on this engine's base; an uncalibrated function is held as empty. */
    private final Map<String, Optional<Calibration>> calibrations = new ConcurrentHashMap<>();

    Jeff(Parts parts) {
        this.engine = parts.engine();
        this.catalogue = parts.catalogue();
        this.baseFormat = parts.engine().baseFormat();
        this.formatId = parts.formatId();
        this.exactKeys = new ConcurrentHashMap<>(parts.exactKeys());
    }

    /** A Jeff over any engine (Python's {@code Jeff(engine, catalogue)}): reads the engine's base format and installed
     * routers once.
     *
     * @param engine the engine
     * @param catalogue the manifests calls are bound with
     * @return the client; closing it closes the engine
     * @throws JeffError.EngineError when the engine's base format is malformed
     * @throws JeffError.ManifestError when an installed router's tree is malformed */
    public static Jeff create(Engine engine, Catalogue catalogue) {
        return new Jeff(parts(engine, catalogue));
    }

    static void checkTimeoutMs(long timeoutMs) {
        if (timeoutMs <= 0) {
            throw new JeffError.InvalidRequest("timeoutMs must be a whole number of milliseconds above 0; got " + timeoutMs);
        }
    }

    /** A Jeff over the engine already running on this machine at {@code socketPath} (start it with {@code jeff runtime serve}).
     * It never starts an engine and never falls back to a server: no engine there is an EngineError naming the path
     * and the command that starts one.
     *
     * @param socketPath the engine's Unix socket
     * @param options how long to wait for each reply, and the catalogue
     * @return the client, over one connection all its threads share
     * @throws JeffError.InvalidRequest when the timeout is not above 0
     * @throws JeffError.EngineError when nothing listens on the socket (naming the command that starts an engine),
     *     or the engine sends no hello, installed list or health within the timeout
     * @throws JeffError.ProtocolMismatch when the engine speaks another protocol version
     * @throws JeffError.ManifestError when an installed router's tree is malformed */
    public static Jeff local(String socketPath, LocalOptions options) {
        checkTimeoutMs(options.timeoutMs());
        SocketEngine engine = SocketEngine.connect(socketPath, options.timeoutMs());
        try {
            return create(engine, options.catalogue() == null ? Catalogue.shipped() : options.catalogue());
        } catch (RuntimeException | Error error) {
            try {
                engine.close();
            } catch (RuntimeException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
    }

    /** A Jeff over the Jeff server at {@code url} (ours or your own): reads {@code GET /v1/functions} once, and
     * refuses a server serving another version of an adapter this client's catalogue has.
     *
     * @param url the server's {@code http://} or {@code https://} address
     * @param token the bearer token: {@link TokenSource#of}, {@link TokenSource#from} or {@link TokenSource#none}
     * @param options the timeout, the catalogue, and whether to use the WebSocket channel
     * @return the client: a {@link ChannelJeff} with the channel, else a {@link RemoteJeff} over HTTP
     * @throws JeffError.InvalidRequest when the timeout is not above 0 or the address is not one the SDK can call
     * @throws JeffError.ContractError when the server serves another version of an adapter, or answers in a form
     *     the contract does not allow
     * @throws JeffError.Unauthorised when the server refuses the token, or the token is not a non-empty printable
     *     ASCII string
     * @throws JeffError.BackendUnavailable when the server cannot be reached
     * @throws JeffError.DeadlineExceeded when the server takes longer than the timeout */
    public static RemoteJeff remote(String url, TokenSource token, RemoteOptions options) {
        checkTimeoutMs(options.timeoutMs());
        Catalogue catalogue = options.catalogue() == null ? Catalogue.shipped() : options.catalogue();
        if (options.channel()) return ChannelJeff.connect(url, token, options.timeoutMs(), catalogue);
        RestTransport transport = new RestTransport(url, token, options.timeoutMs());
        try {
            return RemoteJeff.connect(transport.asTransport(), catalogue, transport.url());
        } catch (RuntimeException | Error error) {
            try {
                transport.close();
            } catch (RuntimeException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
    }

    /** {@link #remote(String, TokenSource, RemoteOptions)} with a fixed token.
     *
     * @param url the server's address
     * @param token the bearer token, never refreshed
     * @param options the timeout, the catalogue, and whether to use the channel
     * @return the client
     * @throws JeffError the errors of {@link #remote(String, TokenSource, RemoteOptions)} */
    public static RemoteJeff remote(String url, String token, RemoteOptions options) {
        return remote(url, TokenSource.of(token), options);
    }

    /** {@link #remote(String, TokenSource, RemoteOptions)} with a function returning the current token: called before
     * every HTTP request, and on the channel when its token is about to expire.
     *
     * @param url the server's address
     * @param token the function
     * @param options the timeout, the catalogue, and whether to use the channel
     * @return the client
     * @throws JeffError the errors of {@link #remote(String, TokenSource, RemoteOptions)} */
    public static RemoteJeff remote(String url, Supplier<String> token, RemoteOptions options) {
        return remote(url, TokenSource.from(token), options);
    }

    /** The engine this client decides with.
     *
     * @return the engine (for a remote client, the server's listing) */
    public Engine engine() {
        return engine;
    }

    /** The manifests calls are bound with.
     *
     * @return the catalogue */
    public Catalogue catalogue() {
        return catalogue;
    }

    /** The engine's base format, read once when the client was made.
     *
     * @return the format */
    public BaseFormat baseFormat() {
        return baseFormat;
    }

    /** Which calibration functions decide with on this engine ({@link BaseFormat#formatIdOf}).
     *
     * @return null for a bfloat16 base; the INT4 base's {@code format_id} otherwise */
    public String formatId() {
        return formatId;
    }

    Map<String, Map<String, String>> exactKeys() {
        return exactKeys;
    }

    /** The calibration a function decides with on this client's engine, resolved once per function and kept.
     *
     * @param fn the function
     * @return its calibration, or null when it is uncalibrated
     * @throws JeffError.ManifestError when a calibrated function has no calibration for the INT4 base the engine
     *     serves
     * @throws JeffError.NotInstalled on a remote client, when the server does not list the function */
    public Calibration calibrationOf(FunctionSpec fn) {
        Optional<Calibration> known = calibrations.get(fn.id());
        if (known != null) return known.isPresent() ? known.get() : null;
        Calibration resolved = resolveCalibrationOf(fn);
        calibrations.put(fn.id(), Optional.ofNullable(resolved));
        return resolved;
    }

    Calibration resolveCalibrationOf(FunctionSpec fn) {
        return Manifests.calibrationFor(fn, formatId);
    }

    /** Whether the engine serves the adapter: a cached list missing it is asked once more, with refresh. */
    static boolean serves(Engine engine, String adapterId) {
        return engine.installed(false).contains(adapterId) || engine.installed(true).contains(adapterId);
    }

    /** The one place a call is bound: its function resolved, its adapter checked as served, its calibration chosen. */
    Binding bindCall(Call call) {
        FunctionSpec fn = catalogue.function(call.function());
        Manifest manifest = catalogue.manifestOf(fn);
        if (!serves(engine, manifest.id())) throw JeffError.notInstalled(fn.id());
        return Binding.bind(manifest, fn, call.context(), call.labels(), calibrationOf(fn));
    }

    /** Decides calls in one batch: one engine request per adapter (in the order the adapters are first met), every
     * request started before any is waited for so the engine can batch them. No policy is applied: each decision is
     * returned as it is, abstained or not.
     *
     * @param calls the calls
     * @return their decisions, in call order
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    @Override
    public List<Decision> decide(List<Call> calls) {
        List<Bound> bound = new ArrayList<>();
        for (Call call : calls) bound.add(new Bound(call, bindCall(call)));
        return decideBound(bound);
    }

    /** {@link #decide} on a virtual thread; the list is copied first.
     *
     * @param calls the calls
     * @return a future of their decisions, failed with the error {@link #decide} would raise */
    public CompletableFuture<List<Decision>> decideAsync(List<Call> calls) {
        // copied here, so a caller changing its list later changes nothing; a null list (or a null call) fails in the
        // future, as every other failure of an ...Async method does
        List<Call> copy = calls == null ? null : new ArrayList<>(calls);
        return Async.run(() -> decide(copy));
    }

    private static List<Row> rowsOf(List<Bound> bound, List<Integer> indices) {
        List<Row> rows = new ArrayList<>();
        for (int index : indices) rows.add(Binding.buildRow(bound.get(index).binding(), bound.get(index).call().input()));
        return rows;
    }

    private static void decideGroup(List<Bound> bound, String adapterId, List<Integer> indices, EngineResult output,
                                    Decision[] results) {
        if (output.probabilities().size() != indices.size()) {
            throw new JeffError.EngineError("The engine answered " + output.probabilities().size() + " of " + indices.size()
                + " rows for " + adapterId);
        }
        for (int row = 0; row < indices.size(); row++) {
            int index = indices.get(row);
            results[index] = Decision.make(bound.get(index).binding(), output.probabilities().get(row), output.timing());
        }
    }

    /** Bound calls, one predict per adapter (in the order the adapters are first met); decisions in call order. With
     * several adapters every predict is started before any is waited for, so the engine can batch them; a failure is
     * reported as Python, which handles one adapter after another, meets it. */
    List<Decision> decideBound(List<Bound> bound) {
        Map<String, List<Integer>> groups = new LinkedHashMap<>();
        for (int index = 0; index < bound.size(); index++) {
            groups.computeIfAbsent(bound.get(index).binding().manifest().id(), id -> new ArrayList<>()).add(index);
        }
        Decision[] results = new Decision[bound.size()];
        if (groups.size() == 1) {
            Map.Entry<String, List<Integer>> group = groups.entrySet().iterator().next();
            List<Row> rows = rowsOf(bound, group.getValue());
            decideGroup(bound, group.getKey(), group.getValue(), engine.predict(group.getKey(), rows), results);
            return List.of(results);
        }
        List<CompletableFuture<EngineResult>> pending = new ArrayList<>();
        RuntimeException buildFailure = null;
        for (Map.Entry<String, List<Integer>> group : groups.entrySet()) {
            List<Row> rows;
            try {
                rows = rowsOf(bound, group.getValue());
            } catch (RuntimeException error) {
                buildFailure = error;
                break;
            }
            CompletableFuture<EngineResult> predict;
            try {
                predict = engine.predictAsync(group.getKey(), rows);
            } catch (RuntimeException error) {
                // an engine that throws instead of failing its future: the same failure, and no later predict starts
                pending.add(CompletableFuture.failedFuture(error));
                break;
            }
            pending.add(predict);
        }
        // Every started predict has finished before anything is reported, so none is left running; each failure is
        // then reported, below, in adapter order (a later adapter's failure is only met once the earlier ones are
        // decided, as Python meets it).
        awaitAll(pending, groups.keySet());
        int started = 0;
        for (Map.Entry<String, List<Integer>> group : groups.entrySet()) {
            if (started == pending.size()) break;
            decideGroup(bound, group.getKey(), group.getValue(), Async.join(pending.get(started)), results);
            started += 1;
        }
        if (buildFailure != null) throw buildFailure;
        return List.of(results);
    }

    /** Waits until every started predict has finished, successfully or not (their failures are read afterwards, in
     * adapter order). An interrupted caller stops waiting: the interrupt flag is kept and CancellationException names
     * the predicts still running. */
    private static void awaitAll(List<CompletableFuture<EngineResult>> pending, Set<String> adapters) {
        try {
            CompletableFuture.allOf(pending.toArray(CompletableFuture[]::new)).handle((ignored, failure) -> null).get();
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            CancellationException cancelled = new CancellationException("Interrupted while waiting for the engine's "
                + "predicts of " + String.join(", ", adapters) + "; the predicts still running were not waited for");
            cancelled.initCause(error);
            throw cancelled;
        } catch (ExecutionException error) {
            throw new IllegalStateException("a wait that handles every failure failed", error);
        }
    }

    /** Binds a function with no context, no labels and the "return" policy.
     *
     * @param ref the function's reference
     * @return the bound function
     * @throws JeffError the errors of {@link #function(String, CallOptions)} */
    public BoundFunction function(String ref) {
        return function(ref, CallOptions.none());
    }

    /** Binds a function once, now: its context, labels and policy are checked here, and the binding is reused by
     * every call.
     *
     * @param ref the function's reference
     * @param options its context, labels and policy
     * @return the bound function
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...)
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows, or an escalation step is unknown
     * @throws JeffError.InterfaceMismatch when an escalation step implements another interface */
    public BoundFunction function(String ref, CallOptions options) {
        return BoundFunction.create(this, new Call(ref, null, options.context(), options.labels()), options.onLowConfidence());
    }

    Decision applyPolicy(Call call, Decision decision, Policy policy) {
        return Escalation.applyPolicy(this, call, decision, policy);
    }

    /** A call through an interface: routed (dispatch) when {@code function} is null, else to that function. */
    Decision interfaceCall(String iface, Object input, String function, CallOptions options) {
        Policy policy = options.onLowConfidence();
        if (function == null) return dispatch(input, iface, DEFAULT_ROUTER, options);
        FunctionSpec spec = catalogue.function(function);
        if (!spec.interfaceName().equals(iface)) {
            throw new JeffError.InterfaceMismatch(spec.id() + " implements " + spec.interfaceName() + ", not " + iface
                + "; call jeff." + spec.interfaceName());
        }
        Escalation.checkPolicy(this, iface, policy);
        Call call = new Call(function, input, options.context(), options.labels());
        return applyPolicy(call, decide(List.of(call)).get(0), policy);
    }

    private Decision named(String iface, Object input, String function, CallOptions options) {
        return interfaceCall(iface, input, Objects.requireNonNull(function, "function (use the overload without it to route)"), options);
    }

    /** Lets the routers choose the function that answers through {@code classify}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.classify(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision classify(Object input, CallOptions options) { return interfaceCall("classify", input, null, options); }
    /** Calls the named function, which must implement {@code classify}, and applies the policy (Python's
     * {@code jeff.classify(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision classify(Object input, String function, CallOptions options) { return named("classify", input, function, options); }
    /** Lets the routers choose the function that answers through {@code route}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.route(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision route(Object input, CallOptions options) { return interfaceCall("route", input, null, options); }
    /** Calls the named function, which must implement {@code route}, and applies the policy (Python's
     * {@code jeff.route(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision route(Object input, String function, CallOptions options) { return named("route", input, function, options); }
    /** Lets the routers choose the function that answers through {@code rate}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.rate(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision rate(Object input, CallOptions options) { return interfaceCall("rate", input, null, options); }
    /** Calls the named function, which must implement {@code rate}, and applies the policy (Python's
     * {@code jeff.rate(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision rate(Object input, String function, CallOptions options) { return named("rate", input, function, options); }
    /** Lets the routers choose the function that answers through {@code detect}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.detect(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision detect(Object input, CallOptions options) { return interfaceCall("detect", input, null, options); }
    /** Calls the named function, which must implement {@code detect}, and applies the policy (Python's
     * {@code jeff.detect(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision detect(Object input, String function, CallOptions options) { return named("detect", input, function, options); }
    /** Lets the routers choose the function that answers through {@code is_supported}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.is_supported(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision isSupported(Object input, CallOptions options) { return interfaceCall("is_supported", input, null, options); }
    /** Calls the named function, which must implement {@code is_supported}, and applies the policy (Python's
     * {@code jeff.is_supported(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision isSupported(Object input, String function, CallOptions options) { return named("is_supported", input, function, options); }
    /** Lets the routers choose the function that answers through {@code equivalent}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.equivalent(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision equivalent(Object input, CallOptions options) { return interfaceCall("equivalent", input, null, options); }
    /** Calls the named function, which must implement {@code equivalent}, and applies the policy (Python's
     * {@code jeff.equivalent(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision equivalent(Object input, String function, CallOptions options) { return named("equivalent", input, function, options); }
    /** Lets the routers choose the function that answers through {@code requires_escalation}, from {@link #DEFAULT_ROUTER}
     * (Python's {@code jeff.requires_escalation(input)}).
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return the chosen function's decision, after the policy, with the route taken
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision requiresEscalation(Object input, CallOptions options) { return interfaceCall("requires_escalation", input, null, options); }
    /** Calls the named function, which must implement {@code requires_escalation}, and applies the policy (Python's
     * {@code jeff.requires_escalation(input, function=...)}).
     *
     * @param input the input
     * @param function the function's reference (never null; use the overload without it to route)
     * @param options its context, labels and policy
     * @return its decision, after the policy
     * @throws JeffError.InterfaceMismatch when the function implements another interface
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the engine (or server) does not serve its adapter
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.UnknownContext},
     *     {@link JeffError.MissingLabels}, {@link JeffError.InvalidLabels}, ...) and the engine's or server's
     *     ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded},
     *     {@link JeffError.BackendUnavailable}, ...) */
    public Decision requiresEscalation(Object input, String function, CallOptions options) { return named("requires_escalation", input, function, options); }

    /** {@link #classify(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> classifyAsync(Object input, CallOptions options) { return Async.run(() -> classify(input, options)); }
    /** {@link #classify(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> classifyAsync(Object input, String function, CallOptions options) { return Async.run(() -> classify(input, function, options)); }
    /** {@link #route(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> routeAsync(Object input, CallOptions options) { return Async.run(() -> route(input, options)); }
    /** {@link #route(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> routeAsync(Object input, String function, CallOptions options) { return Async.run(() -> route(input, function, options)); }
    /** {@link #rate(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> rateAsync(Object input, CallOptions options) { return Async.run(() -> rate(input, options)); }
    /** {@link #rate(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> rateAsync(Object input, String function, CallOptions options) { return Async.run(() -> rate(input, function, options)); }
    /** {@link #detect(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> detectAsync(Object input, CallOptions options) { return Async.run(() -> detect(input, options)); }
    /** {@link #detect(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> detectAsync(Object input, String function, CallOptions options) { return Async.run(() -> detect(input, function, options)); }
    /** {@link #isSupported(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> isSupportedAsync(Object input, CallOptions options) { return Async.run(() -> isSupported(input, options)); }
    /** {@link #isSupported(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> isSupportedAsync(Object input, String function, CallOptions options) { return Async.run(() -> isSupported(input, function, options)); }
    /** {@link #equivalent(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> equivalentAsync(Object input, CallOptions options) { return Async.run(() -> equivalent(input, options)); }
    /** {@link #equivalent(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> equivalentAsync(Object input, String function, CallOptions options) { return Async.run(() -> equivalent(input, function, options)); }
    /** {@link #requiresEscalation(Object, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> requiresEscalationAsync(Object input, CallOptions options) { return Async.run(() -> requiresEscalation(input, options)); }
    /** {@link #requiresEscalation(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param function the function's reference
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> requiresEscalationAsync(Object input, String function, CallOptions options) { return Async.run(() -> requiresEscalation(input, function, options)); }

    /** Routes the call from {@link #DEFAULT_ROUTER} to the function that should answer it, then decides it there.
     *
     * @param input the input
     * @param iface the interface the answer must come through ({@code classify}, {@code route}, ...)
     * @param options its context, labels and policy
     * @return the decision of the function the routers chose, with the route they took
     * @throws JeffError the errors of {@link #dispatch(Object, String, String, CallOptions)} */
    public Decision dispatch(Object input, String iface, CallOptions options) {
        return dispatch(input, iface, DEFAULT_ROUTER, options);
    }

    /** Routes the call from a router to the function that should answer it, then decides it there.
     *
     * @param input the input
     * @param iface the interface the answer must come through
     * @param router the router to start from (never null)
     * @param options its context, labels and policy
     * @return the decision of the function the routers chose, with the route they took
     * @throws JeffError.LowConfidence when a router abstains
     * @throws JeffError.UnknownFunction when the router, or a target it chose, is not in the catalogue
     * @throws JeffError.NotInstalled when the engine does not serve a router or the chosen function
     * @throws JeffError.ManifestError when a router's tree is malformed
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the binding's errors and the engine's or server's */
    public Decision dispatch(Object input, String iface, String router, CallOptions options) {
        return Routing.dispatch(this, input, iface, Objects.requireNonNull(router, "router"), options);
    }

    /** {@link #dispatch(Object, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param iface the interface
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> dispatchAsync(Object input, String iface, CallOptions options) {
        return Async.run(() -> dispatch(input, iface, options));
    }

    /** {@link #dispatch(Object, String, String, CallOptions)} on a virtual thread.
     *
     * @param input the input
     * @param iface the interface
     * @param router the router to start from
     * @param options its context, labels and policy
     * @return a future of the decision, failed with the error the blocking method would raise */
    public CompletableFuture<Decision> dispatchAsync(Object input, String iface, String router, CallOptions options) {
        return Async.run(() -> dispatch(input, iface, router, options));
    }

    /** Runs a graph with no code nodes and the policies its document gives.
     *
     * @param graph the graph
     * @param input the graph's input
     * @param params the graph's params (an empty map for none)
     * @return every node's result, the outputs and the stages run
     * @throws JeffError the errors of {@link #runGraph(Graph, Object, Map, Map, Map)} */
    public GraphResult runGraph(Graph graph, Object input, Map<String, ?> params) {
        return runGraph(graph, input, params, Map.of(), Map.of());
    }

    /** Runs a graph in batched stages: each stage decides every node whose dependencies have results, one engine
     * request per adapter. Everything that can be checked is checked before the first decision.
     *
     * @param graph the graph
     * @param input the graph's input
     * @param params the graph's params (an empty map for none)
     * @param code the code nodes' functions, by name (an empty map for none)
     * @param policies a policy per node, in place of the document's {@code "return"} or {@code "error"} (an empty map
     *     for none)
     * @return every node's result, the outputs and the stages run
     * @throws JeffError.GraphError when a param, code function or policy is missing or unknown, a code function
     *     fails or returns something that is not JSON, or a node cannot run
     * @throws JeffError.LowConfidence when a node abstains under {@link Policy#ERROR}
     * @throws JeffError the binding's, escalation's and engine's or server's errors, as for {@link #decide} */
    public GraphResult runGraph(Graph graph, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                                Map<String, Policy> policies) {
        return Graphs.run(this, graph, input, params, code, policies);
    }

    /** Runs the graph document in a YAML (or JSON) file.
     *
     * @param file the file
     * @param input the graph's input
     * @param params the graph's params
     * @param code the code nodes' functions, by name
     * @param policies a policy per node
     * @return every node's result, the outputs and the stages run
     * @throws JeffError the errors of {@link Graphs#load} and of {@link #runGraph(Graph, Object, Map, Map, Map)} */
    public GraphResult runGraph(Path file, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                                Map<String, Policy> policies) {
        return runGraph(Graphs.load(file), input, params, code, policies);
    }

    /** Runs a graph document given as a map (contract/schemas/graph.schema.json).
     *
     * @param document the document
     * @param input the graph's input
     * @param params the graph's params
     * @param code the code nodes' functions, by name
     * @param policies a policy per node
     * @return every node's result, the outputs and the stages run
     * @throws JeffError the errors of {@link Graphs#fromMap} and of {@link #runGraph(Graph, Object, Map, Map, Map)} */
    public GraphResult runGraph(Map<String, ?> document, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                                Map<String, Policy> policies) {
        return runGraph(Graphs.fromMap(document, "the graph given to runGraph"), input, params, code, policies);
    }

    /** {@link #runGraph(Graph, Object, Map, Map, Map)} on a virtual thread.
     *
     * @param graph the graph
     * @param input the graph's input
     * @param params the graph's params
     * @param code the code nodes' functions, by name
     * @param policies a policy per node
     * @return a future of the result, failed with the error the blocking method would raise */
    public CompletableFuture<GraphResult> runGraphAsync(Graph graph, Object input, Map<String, ?> params,
                                                        Map<String, CodeFunction> code, Map<String, Policy> policies) {
        return Async.run(() -> runGraph(graph, input, params, code, policies));
    }

    /** Closes the engine (its connection); for a remote client, its connections to the server. */
    @Override
    public void close() {
        engine.close();
    }
}
