package ai.jeff.sdk;

import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantLock;

/** The timer of one HTTP request once it holds a place in the pool, restarted as the request moves on, each phase
 * getting the full timeout: connecting (the TCP connection and its TLS handshake), waiting for the answer's headers
 * (from the request's first byte written, so sending it counts), and each wait for more of the answer's body (restarted
 * whenever bytes arrive). When it runs out it records the phase it timed and runs the abort action (which closes the
 * request's connection, so the blocked read or write fails at once). Waiting for a place in the pool is timed by the
 * pool itself (ConnectionPool.acquire). */
final class PhaseTimer {
    enum Phase { CONNECT, HEADERS, BODY }

    private final long timeoutMs;
    private final ReentrantLock lock = new ReentrantLock();
    private ScheduledFuture<?> timer;
    private long deadline;
    private Phase phase;
    private Phase fired;
    private boolean done;
    private boolean aborted;
    private Runnable abort;

    PhaseTimer(long timeoutMs) {
        this.timeoutMs = timeoutMs;
    }

    /** Starts timing `next`, a full timeout from now; does nothing once the request finished, timed out or was aborted. */
    void start(Phase next) {
        lock.lock();
        try {
            if (done) return;
            phase = next;
            deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
            if (timer == null) timer = Async.TIMERS.schedule(this::fire, timeoutMs, TimeUnit.MILLISECONDS);
        } finally {
            lock.unlock();
        }
    }

    /** Bytes of the answer arrived: in the body phase, its wait starts again. */
    void progress() {
        lock.lock();
        try {
            if (!done && phase == Phase.BODY) deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
        } finally {
            lock.unlock();
        }
    }

    /** What ends the request when the timer runs out or abort() is called (run at once if that already happened). */
    void onAbort(Runnable action) {
        boolean now;
        lock.lock();
        try {
            abort = action;
            now = aborted;
        } finally {
            lock.unlock();
        }
        if (now) action.run();
    }

    private void fire() {
        Runnable action;
        lock.lock();
        try {
            timer = null;
            if (done) return;
            long left = deadline - System.nanoTime();
            if (left > 0) {
                // the deadline moved on (a new phase, or more body) since this run was scheduled: wait for the rest
                timer = Async.TIMERS.schedule(this::fire, left, TimeUnit.NANOSECONDS);
                return;
            }
            done = true;
            aborted = true;
            fired = phase;
            action = abort;
        } finally {
            lock.unlock();
        }
        // run outside the lock: closing a connection wakes the request's thread, which reads this timer
        if (action != null) action.run();
    }

    /** The phase that ran out, or null. */
    Phase fired() {
        lock.lock();
        try {
            return fired;
        } finally {
            lock.unlock();
        }
    }

    /** The phase the request is in (or was in when it stopped); null before connecting or reusing a connection. */
    Phase phase() {
        lock.lock();
        try {
            return phase;
        } finally {
            lock.unlock();
        }
    }

    /** Stops the timer for good: the request settled or failed. */
    void finish() {
        lock.lock();
        try {
            done = true;
            if (timer != null) timer.cancel(false);
            timer = null;
        } finally {
            lock.unlock();
        }
    }

    /** Ends the request now (the transport is closing): the timer stops and the abort action runs. */
    void abort() {
        Runnable action;
        lock.lock();
        try {
            if (aborted) return;
            aborted = true;
            done = true;
            if (timer != null) timer.cancel(false);
            timer = null;
            action = abort;
        } finally {
            lock.unlock();
        }
        if (action != null) action.run();
    }
}
