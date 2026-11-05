package ai.jeff.sdk;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

/** Keeps every recursive walk of the SDK (PyFormat.repr, Values.plain, Json, MsgPack, Yaml and the contract's schema
 * checker) from overflowing the caller's stack, whatever its size.
 *
 * <p>Each walk has its own depth limit and refuses a deeper value with an error that names the limit. Those limits are
 * deep enough that the walk can need several megabytes of stack, which a small thread (a 256 KB platform thread, say)
 * does not have. So before a walk starts, its input is measured without recursion. When the input is at most
 * {@link #INLINE_DEPTH} levels deep, the walk runs on the caller's thread, which then needs only a few tens of
 * kilobytes. When the input is deeper, the walk runs on a new thread with a {@link #LARGE_STACK}-byte stack, and the
 * caller waits for it. That thread's stack holds every walk to its own limit, so the result is always either the
 * answer or the named error, never a StackOverflowError.
 *
 * <p>Nothing here catches StackOverflowError: that error can leave a class half-initialized or a call site unlinked
 * for good. A thread is started only for deep input, which a real message rarely is. */
final class Nesting {
    /** The deepest input (lists and maps inside one another) a walk handles on the caller's own thread. */
    static final int INLINE_DEPTH = 16;
    /** The stack of the thread that handles deeper input: room for every walk at its own depth limit. */
    static final long LARGE_STACK = 64L * 1024 * 1024; // measured worst case about 2 MB (repr at 9998 levels); 64 MB keeps a wide margin while reserving little address space per deep call
    /** How deep CPython 3.13 lets repr(), json.dumps and json.loads go (measured: 9998 lists inside one another work,
     * 9999 raise a RecursionError); PyFormat.repr, Values.plain and Json stop at the same depth, by name. */
    static final int PYTHON_DEPTH = 9998;

    private static final ThreadLocal<Boolean> ON_LARGE_STACK = ThreadLocal.withInitial(() -> false);

    private Nesting() {}

    /** The work on this thread when `shallow` (or when this already is a large-stack thread), else on a new thread
     * with a large stack. Whatever the work throws is thrown here. */
    static <T> T run(boolean shallow, Supplier<T> work) {
        if (shallow || ON_LARGE_STACK.get()) return work.get();
        return onLargeStack(work);
    }

    /** The work on a large-stack thread, always (for input that cannot be measured cheaply first, such as YAML). */
    static <T> T onLargeStack(Supplier<T> work) {
        if (ON_LARGE_STACK.get()) return work.get();
        Object[] result = new Object[1];
        Throwable[] failure = new Throwable[1];
        Thread thread = Thread.ofPlatform().name("jeff-deep-value").daemon(true).stackSize(LARGE_STACK).unstarted(() -> {
            ON_LARGE_STACK.set(true);
            try {
                result[0] = work.get();
            } catch (Throwable thrown) {
                failure[0] = thrown;
            }
        });
        thread.start();
        boolean interrupted = false;
        while (true) {
            try {
                thread.join();
                break;
            } catch (InterruptedException error) {
                // The work is finite and has no side effects: wait for it, and hand the interrupt back afterwards.
                interrupted = true;
            }
        }
        if (interrupted) Thread.currentThread().interrupt();
        if (failure[0] instanceof RuntimeException error) throw error;
        if (failure[0] instanceof Error error) throw error;
        if (failure[0] != null) throw new IllegalStateException("A deep value's walk failed", failure[0]);
        @SuppressWarnings("unchecked")
        T value = (T) result[0];
        return value;
    }

    /** True when the value holds lists and maps at most INLINE_DEPTH levels deep, measured without recursion; stops
     * looking as soon as it finds a deeper one. */
    static boolean shallow(Object value) {
        Deque<Object> values = new ArrayDeque<>();
        Deque<Integer> depths = new ArrayDeque<>();
        values.push(value == null ? Nesting.class : value);
        depths.push(0);
        while (!values.isEmpty()) {
            Object current = values.pop();
            int depth = depths.pop();
            Iterable<?> children;
            if (current instanceof List<?> list) children = list;
            else if (current instanceof Map<?, ?> map) children = map.values();
            else continue;
            if (depth + 1 > INLINE_DEPTH) return false;
            for (Object child : children) {
                if (child instanceof List<?> || child instanceof Map<?, ?>) {
                    values.push(child);
                    depths.push(depth + 1);
                }
            }
        }
        return true;
    }

    /** True when JSON text opens at most INLINE_DEPTH arrays and objects inside one another (brackets inside strings
     * do not count). Text that is not JSON may measure either way; the parser then names what is wrong. */
    static boolean shallowJson(String text) {
        int depth = 0;
        boolean inString = false;
        for (int index = 0; index < text.length(); index++) {
            char character = text.charAt(index);
            if (inString) {
                if (character == '\\') index++;
                else if (character == '"') inString = false;
            } else if (character == '"') {
                inString = true;
            } else if (character == '[' || character == '{') {
                if (++depth > INLINE_DEPTH) return false;
            } else if (character == ']' || character == '}') {
                depth--;
            }
        }
        return true;
    }

    /** True when MessagePack bytes hold arrays and maps at most INLINE_DEPTH levels deep, read without recursion;
     * bytes that are not MessagePack measure as deep, and the decoder then names what is wrong. */
    static boolean shallowMsgPack(byte[] bytes) {
        long[] left = new long[INLINE_DEPTH + 1];
        int depth = 0;
        left[0] = 1;
        int position = 0;
        while (depth >= 0) {
            if (left[depth] == 0) {
                depth--;
                continue;
            }
            left[depth]--;
            if (position >= bytes.length) return false;
            int code = bytes[position++] & 0xff;
            long children = -1;
            long skip = 0;
            if (code <= 0x7f || code >= 0xe0 || code == 0xc0 || code == 0xc2 || code == 0xc3) skip = 0;
            else if ((code & 0xe0) == 0xa0) skip = code & 0x1f;
            else if ((code & 0xf0) == 0x90) children = code & 0x0f;
            else if ((code & 0xf0) == 0x80) children = 2L * (code & 0x0f);
            else {
                int width = switch (code) {
                    case 0xc4, 0xd9, 0xcc, 0xd0 -> 1;
                    case 0xc5, 0xda, 0xdc, 0xde, 0xcd, 0xd1 -> 2;
                    case 0xc6, 0xdb, 0xdd, 0xdf, 0xca, 0xce, 0xd2 -> 4;
                    case 0xcb, 0xcf, 0xd3 -> 8;
                    default -> -1;
                };
                if (width < 0 || position + width > bytes.length) return false;
                long number = 0;
                for (int index = 0; index < width; index++) number = (number << 8) | (bytes[position + index] & 0xff);
                position += width;
                switch (code) {
                    case 0xc4, 0xc5, 0xc6, 0xd9, 0xda, 0xdb -> skip = number;
                    case 0xdc, 0xdd -> children = number;
                    case 0xde, 0xdf -> children = 2 * number;
                    default -> skip = 0;
                }
            }
            if (skip > bytes.length - position) return false;
            position += (int) skip;
            if (children > 0) {
                if (depth + 1 > INLINE_DEPTH) return false;
                left[++depth] = children;
            }
        }
        return true;
    }
}
