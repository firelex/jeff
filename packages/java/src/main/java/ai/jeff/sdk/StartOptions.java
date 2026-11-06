package ai.jeff.sdk;

/** Options of {@link Jeff#local(java.util.List, StartOptions)}, the local mode that finds the engine running for this
 * user on this machine or starts one (contract/local-engine.md). Both are whole numbers of milliseconds of at least 1.
 *
 * @param timeoutMs how long to wait for the engine's hello and each of its replies, in milliseconds (default 60,000)
 * @param startTimeoutMs how long everything before the client is ready may take, in milliseconds: waiting for another
 *     program that is starting the engine, and starting it (default 1,800,000, 30 minutes: the first start downloads
 *     about 2 GB) */
public record StartOptions(long timeoutMs, long startTimeoutMs) {
    /** The default reply timeout: 60,000 ms. */
    public static final long DEFAULT_TIMEOUT_MS = 60_000;
    /** The default start timeout: 1,800,000 ms (30 minutes). */
    public static final long DEFAULT_START_TIMEOUT_MS = 1_800_000;

    /** The default options: a reply timeout of 60,000 ms and a start timeout of 1,800,000 ms.
     *
     * @return the options */
    public static StartOptions defaults() {
        return new StartOptions(DEFAULT_TIMEOUT_MS, DEFAULT_START_TIMEOUT_MS);
    }

    /** These options with another reply timeout.
     *
     * @param newTimeoutMs how long to wait for the engine's hello and each of its replies, in milliseconds
     * @return new options */
    public StartOptions withTimeoutMs(long newTimeoutMs) {
        return new StartOptions(newTimeoutMs, startTimeoutMs);
    }

    /** These options with another start timeout.
     *
     * @param newStartTimeoutMs how long everything before the client is ready may take, in milliseconds
     * @return new options */
    public StartOptions withStartTimeoutMs(long newStartTimeoutMs) {
        return new StartOptions(timeoutMs, newStartTimeoutMs);
    }
}
