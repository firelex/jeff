package ai.jeff.sdk;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.function.Supplier;

/** Where the SDK runs work off the caller's thread: virtual threads for the ...Async methods and for waiting on
 * replies, and one daemon timer thread. Neither keeps the JVM alive. */
public final class Async {
    static final ExecutorService EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();
    static final ScheduledThreadPoolExecutor TIMERS = timers();

    private Async() {}

    private static ScheduledThreadPoolExecutor timers() {
        ScheduledThreadPoolExecutor timers = new ScheduledThreadPoolExecutor(1, work -> {
            Thread thread = new Thread(work, "jeff-timers");
            thread.setDaemon(true);
            return thread;
        });
        timers.setRemoveOnCancelPolicy(true);
        return timers;
    }

    static <T> CompletableFuture<T> run(Supplier<T> work) {
        return CompletableFuture.supplyAsync(work, EXECUTOR);
    }

    /** Waits for one of the SDK's futures (the {@code ...Async} methods) and returns its value; a failure is thrown as
     * itself (a {@link JeffError} stays that {@link JeffError}), not wrapped in a {@code CompletionException}.
     *
     * @param <T> the future's value type
     * @param future a future an {@code ...Async} method returned
     * @return the future's value */
    public static <T> T join(CompletableFuture<T> future) {
        try {
            return future.join();
        } catch (CompletionException error) {
            if (error.getCause() instanceof RuntimeException failure) throw failure;
            if (error.getCause() instanceof Error fatal) throw fatal;
            throw error;
        }
    }
}
