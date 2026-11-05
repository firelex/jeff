package ai.jeff.sdk;

import java.io.IOException;
import java.net.SocketTimeoutException;
import java.nio.channels.ClosedByInterruptException;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;
import javax.net.ssl.SSLSocketFactory;

/** HTTP/1.1 requests to one Jeff server, with httpx's Timeout(timeout_s) and default Limits: timeoutMs bounds each
 * phase of a request on its own (waiting for a place in the pool, connecting, the answer's headers from the first byte
 * written, each wait for more of the body), and the pool keeps at most 100 connections open, 20 of them idle, none idle
 * longer than 5 s. A request is sent once: nothing here sends it again, whatever happens to its connection. Redirects
 * are not followed (an answer is returned whatever its status). close() ends every request in flight. */
final class Http1Client {
    /** Why a request got no answer. */
    static final class NoAnswer extends Exception {
        enum Kind {
            /** close() was called. */
            CLOSED,
            /** No place in the pool came free within the timeout. */
            POOL_TIMEOUT,
            /** No connection (TCP and TLS) within the timeout. */
            CONNECT_TIMEOUT,
            /** The server's (or proxy's) host name was not resolved within the timeout. */
            LOOKUP_TIMEOUT,
            /** The connection could not be opened: refused, unknown host, TLS failure. */
            CONNECT_FAILED,
            /** No headers, or no further body, within the timeout once the request was being sent. */
            READ_TIMEOUT,
            /** The connection failed once it was open: reset, closed early, an answer that is not HTTP. */
            BROKEN,
            /** The calling thread was interrupted. */
            INTERRUPTED
        }

        final Kind kind;

        NoAnswer(Kind kind, Throwable cause) {
            super(kind.name(), cause);
            this.kind = kind;
        }
    }

    private final ServerUrl url;
    private final Proxies.Proxy proxy;
    private final long timeoutMs;
    private final ConnectionPool pool;
    private final HttpConnection.Opener opener;
    private final Supplier<SSLSocketFactory> tls;
    private final Set<PhaseTimer> inFlight = ConcurrentHashMap.newKeySet();
    private volatile boolean closed;

    Http1Client(ServerUrl url, Proxies.Proxy proxy, long timeoutMs, ConnectionPool pool, HttpConnection.Opener opener,
                Supplier<SSLSocketFactory> tls) {
        this.url = url;
        this.proxy = proxy;
        this.timeoutMs = timeoutMs;
        this.pool = pool;
        this.opener = opener;
        this.tls = tls;
    }

    ConnectionPool pool() {
        return pool;
    }

    int inFlightCount() {
        return inFlight.size();
    }

    boolean closed() {
        return closed;
    }

    /** Sends `request` (its head and body, as bytes) once and reads the whole answer. */
    HttpConnection.Answer send(byte[] request) throws NoAnswer {
        if (closed) throw new NoAnswer(NoAnswer.Kind.CLOSED, null);
        PhaseTimer timer = new PhaseTimer(timeoutMs);
        inFlight.add(timer);
        try {
            if (closed) throw new NoAnswer(NoAnswer.Kind.CLOSED, null);
            return exchange(request, timer);
        } finally {
            timer.finish();
            inFlight.remove(timer);
        }
    }

    private HttpConnection.Answer exchange(byte[] request, PhaseTimer timer) throws NoAnswer {
        ConnectionPool.Lease lease;
        try {
            lease = pool.acquire(timeoutMs);
        } catch (ConnectionPool.PoolTimeout error) {
            throw new NoAnswer(NoAnswer.Kind.POOL_TIMEOUT, error);
        } catch (ConnectionPool.Closed error) {
            throw new NoAnswer(NoAnswer.Kind.CLOSED, error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new NoAnswer(NoAnswer.Kind.INTERRUPTED, error);
        }
        HttpConnection connection = lease.connection();
        boolean released = false;
        try {
            if (connection == null) {
                timer.start(PhaseTimer.Phase.CONNECT);
                connection = HttpConnection.open(url, proxy, timeoutMs, timer, opener, tls);
            }
            boolean[] reusable = new boolean[1];
            HttpConnection.Answer answer = connection.exchange(request, timer, reusable);
            timer.finish();
            released = true;
            pool.release(connection, reusable[0]);
            return answer;
        } catch (Error error) {
            // an Error (out of memory, stack overflow) still gives the pool its place back and closes the socket;
            // cleanup only, the Error goes on unchanged
            if (!released) {
                if (connection != null) connection.abort();
                pool.release(null, false);
            }
            throw error;
        } catch (IOException | RuntimeException error) {
            if (connection != null) connection.abort();
            pool.release(null, false);
            if (closed) throw new NoAnswer(NoAnswer.Kind.CLOSED, error);
            if (error instanceof HttpConnection.LookupTimeout) throw new NoAnswer(NoAnswer.Kind.LOOKUP_TIMEOUT, error);
            PhaseTimer.Phase fired = timer.fired();
            if (fired == PhaseTimer.Phase.CONNECT) throw new NoAnswer(NoAnswer.Kind.CONNECT_TIMEOUT, error);
            if (fired != null) throw new NoAnswer(NoAnswer.Kind.READ_TIMEOUT, error);
            if (error instanceof ClosedByInterruptException || Thread.currentThread().isInterrupted()) {
                Thread.currentThread().interrupt();
                throw new NoAnswer(NoAnswer.Kind.INTERRUPTED, error);
            }
            if (error instanceof RuntimeException runtime) throw runtime;
            if (timer.phase() == PhaseTimer.Phase.CONNECT) {
                throw new NoAnswer(error instanceof SocketTimeoutException ? NoAnswer.Kind.CONNECT_TIMEOUT : NoAnswer.Kind.CONNECT_FAILED,
                    error);
            }
            throw new NoAnswer(NoAnswer.Kind.BROKEN, error);
        }
    }

    /** Ends every request in flight (each gets CLOSED) and closes every connection; later requests are refused. */
    void close() {
        closed = true;
        pool.close();
        for (PhaseTimer timer : inFlight) timer.abort();
    }
}
