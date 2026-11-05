package ai.jeff.sdk;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.locks.ReentrantLock;

/** The connections of one RestTransport, as httpx's default Limits keep them (httpcore's pool): at most
 * maxConnections open at once (idle or busy, connecting included), at most maxIdle idle ones (the one idle longest is
 * closed first), and an idle one is not reused after keepaliveMs. A request that finds every place taken waits, in
 * arrival order: the next connection put back, or the next place freed, goes to the request that has waited longest,
 * never to one that came later. One lock guards it all. */
final class ConnectionPool {
    /** httpx's default max_connections. */
    static final int MAX_CONNECTIONS = 100;
    /** httpx's default max_keepalive_connections. */
    static final int MAX_IDLE = 20;
    /** httpx's default keepalive_expiry (5 s): a connection that a NAT or load balancer dropped without telling either
     * side is not written into. */
    static final long KEEPALIVE_MS = 5000;

    /** No place came free within the timeout (httpx's PoolTimeout). */
    static final class PoolTimeout extends Exception {
        PoolTimeout() {
            super("no connection came free in time");
        }
    }

    /** The pool was closed (RestTransport.close). */
    static final class Closed extends Exception {
        Closed() {
            super("the pool is closed");
        }
    }

    /** What a request is given: an idle connection to reuse, or (connection null) a place to open a new one in. */
    record Lease(HttpConnection connection) {}

    private final int maxConnections;
    private final int maxIdle;
    private final long keepaliveNanos;
    private final ReentrantLock lock = new ReentrantLock();
    /** Idle connections, the one idle longest first. */
    private final Deque<HttpConnection> idle = new ArrayDeque<>();
    private final Deque<CompletableFuture<Lease>> waiters = new ArrayDeque<>();
    private int open;
    private boolean closed;

    ConnectionPool(int maxConnections, int maxIdle, long keepaliveMs) {
        this.maxConnections = maxConnections;
        this.maxIdle = maxIdle;
        this.keepaliveNanos = TimeUnit.MILLISECONDS.toNanos(keepaliveMs);
    }

    /** A connection or a place, waiting at most timeoutMs (in arrival order) when every place is taken. An idle
     * connection that can no longer be used is closed and its place used for a new one. */
    Lease acquire(long timeoutMs) throws PoolTimeout, Closed, InterruptedException {
        CompletableFuture<Lease> waiter;
        List<HttpConnection> expired = new ArrayList<>();
        Lease found = null;
        lock.lock();
        try {
            if (closed) throw new Closed();
            if (waiters.isEmpty()) {
                long now = System.nanoTime();
                for (Iterator<HttpConnection> each = idle.iterator(); each.hasNext(); ) {
                    HttpConnection connection = each.next();
                    if (now - connection.idleSince() > keepaliveNanos) {
                        each.remove();
                        expired.add(connection);
                        open--;
                    }
                }
                if (!idle.isEmpty()) found = new Lease(idle.pollLast());
                else if (open < maxConnections) {
                    open++;
                    found = new Lease(null);
                }
            }
            waiter = found == null ? new CompletableFuture<>() : null;
            if (waiter != null) waiters.addLast(waiter);
        } finally {
            lock.unlock();
        }
        for (HttpConnection connection : expired) connection.abort();
        Lease lease = found != null ? found : await(waiter, timeoutMs);
        if (lease.connection() != null && !lease.connection().usable()) {
            lease.connection().abort();
            return new Lease(null);
        }
        return lease;
    }

    private Lease await(CompletableFuture<Lease> waiter, long timeoutMs) throws PoolTimeout, Closed, InterruptedException {
        try {
            return waiter.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException | InterruptedException stopped) {
            lock.lock();
            boolean removed;
            try {
                removed = waiters.remove(waiter);
            } finally {
                lock.unlock();
            }
            if (removed) {
                if (stopped instanceof InterruptedException interrupted) throw interrupted;
                throw new PoolTimeout();
            }
            // handed a lease just as the wait ended: give it back rather than leak its place
            Lease handed = waiter.join();
            if (stopped instanceof InterruptedException interrupted) {
                release(handed.connection(), handed.connection() != null);
                throw interrupted;
            }
            return handed;
        } catch (ExecutionException failed) {
            if (failed.getCause() instanceof Closed closing) throw closing;
            throw new IllegalStateException("A pool waiter failed with something other than the pool closing", failed.getCause());
        }
    }

    /** Gives a connection back after a request: to the request that has waited longest when it can be reused, else to
     * the idle ones (closing the one idle longest beyond maxIdle). A connection that cannot be reused (or none, for a
     * request that failed before or while opening one) frees its place, which goes to the oldest waiter. */
    void release(HttpConnection connection, boolean reusable) {
        List<HttpConnection> closing = new ArrayList<>();
        lock.lock();
        try {
            if (reusable && connection != null && !closed) {
                CompletableFuture<Lease> waiter = waiters.pollFirst();
                if (waiter != null) {
                    waiter.complete(new Lease(connection));
                    return;
                }
                connection.markIdle();
                idle.addLast(connection);
                while (idle.size() > maxIdle) {
                    closing.add(idle.pollFirst());
                    open--;
                }
                return;
            }
            if (connection != null) closing.add(connection);
            CompletableFuture<Lease> waiter = closed ? null : waiters.pollFirst();
            if (waiter != null) waiter.complete(new Lease(null));
            else open--;
        } finally {
            lock.unlock();
            for (HttpConnection each : closing) each.abort();
        }
    }

    /** Closes every idle connection and fails every waiting request with Closed; later requests are refused. */
    void close() {
        List<HttpConnection> closing;
        List<CompletableFuture<Lease>> waiting;
        lock.lock();
        try {
            closed = true;
            closing = new ArrayList<>(idle);
            open -= idle.size();
            idle.clear();
            waiting = new ArrayList<>(waiters);
            waiters.clear();
        } finally {
            lock.unlock();
        }
        for (HttpConnection connection : closing) connection.abort();
        for (CompletableFuture<Lease> waiter : waiting) waiter.completeExceptionally(new Closed());
    }

    /** How many connections are open (idle or busy, connecting included). */
    int openCount() {
        lock.lock();
        try {
            return open;
        } finally {
            lock.unlock();
        }
    }

    int idleCount() {
        lock.lock();
        try {
            return idle.size();
        } finally {
            lock.unlock();
        }
    }
}
