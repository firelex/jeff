package ai.jeff.sdk;

import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;

/** What the decision logic needs from an inference engine (port of jeff/engine.py): the engine socket
 * ({@link SocketEngine}), a Jeff server's listing (inside {@link RemoteJeff}), a test engine
 * ({@link ai.jeff.sdk.testing.FakeEngine}) or your own. Give your own to {@link Jeff#create}. */
public interface Engine extends AutoCloseable {
    /** The base's weight format, read once when the engine connects.
     *
     * @return the format */
    BaseFormat baseFormat();

    /** The adapters the engine has loaded.
     *
     * @param refresh {@code true} to ask the engine again instead of using a cached list
     * @return the adapter ids */
    Set<String> installed(boolean refresh);

    /** The adapters the engine has loaded, from a cached list when it has one.
     *
     * @return the adapter ids */
    default Set<String> installed() {
        return installed(false);
    }

    /** Runs the adapter's model over rows.
     *
     * @param adapterId the adapter
     * @param rows the rows, each with its state and question
     * @return one probability array per row, with the engine's timing
     * @throws JeffError.NotInstalled when the engine has not loaded the adapter; an engine raises its other named
     *     errors ({@link JeffError.EngineError}, {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, ...) as
     *     they happen */
    EngineResult predict(String adapterId, List<Row> rows);

    /** Starts a predict now and returns its result when it is ready. Requests are started in the order this is
     * called, so an engine sees them in call order. This default runs predict on the calling thread; the engine
     * socket sends the request at once and waits for the reply elsewhere, so several adapters' requests are in
     * flight together.
     *
     * @param adapterId the adapter
     * @param rows the rows
     * @return a future of the result, failed with the error {@link #predict} would raise */
    default CompletableFuture<EngineResult> predictAsync(String adapterId, List<Row> rows) {
        try {
            return CompletableFuture.completedFuture(predict(adapterId, rows));
        } catch (RuntimeException error) {
            return CompletableFuture.failedFuture(error);
        }
    }

    /** Releases what the engine holds (its connection); nothing by default. */
    @Override
    default void close() {}
}
