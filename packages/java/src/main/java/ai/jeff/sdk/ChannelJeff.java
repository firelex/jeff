package ai.jeff.sdk;

import java.lang.ref.Cleaner;
import java.util.concurrent.atomic.AtomicBoolean;

/** A {@link RemoteJeff} over the server's WebSocket channel ({@link Jeff#remote(String, String, RemoteOptions)} with
 * {@link RemoteOptions#withChannel(boolean)}): everything as {@link RemoteJeff}, with bound functions as handles on
 * the server. Close it to end the channel; one the garbage collector takes without {@link #close()} is abandoned (its
 * channel ended at once). */
public final class ChannelJeff extends RemoteJeff {
    private static final Cleaner CLEANER = Cleaner.create();

    private final ChannelTransport channel;
    private final AtomicBoolean closed;

    private ChannelJeff(Parts parts, ChannelTransport channel, String url) {
        super(parts, channel, url);
        this.channel = channel;
        AtomicBoolean flag = new AtomicBoolean();
        this.closed = flag;
        // the action holds the transport and the flag, never this object
        CLEANER.register(this, () -> {
            if (flag.compareAndSet(false, true)) channel.abandon();
        });
    }

    static ChannelJeff open(ChannelTransport transport, Catalogue catalogue, String url) {
        ServedFunctions served = ServedFunctions.read(transport, catalogue, url);
        return new ChannelJeff(parts(served, catalogue), transport, url);
    }

    /** Jeff.remote with the channel. */
    static ChannelJeff connect(String url, TokenSource token, long timeoutMs, Catalogue catalogue) {
        return connect(url, token, timeoutMs, catalogue, HttpOptions.standard(), ChannelTransport.KEEPALIVE_MS);
    }

    /** With the network, TLS sockets and environment given, and the keepalive interval (tests). */
    static ChannelJeff connect(String url, TokenSource token, long timeoutMs, Catalogue catalogue, HttpOptions options, long keepaliveMs) {
        ChannelTransport transport = ChannelTransport.open(url, token, timeoutMs, options, keepaliveMs);
        try {
            return open(transport, catalogue, transport.url());
        } catch (RuntimeException | Error error) {
            try {
                transport.close();
            } catch (RuntimeException closing) {
                error.addSuppressed(closing);
            }
            throw error;
        }
    }

    ChannelTransport channel() {
        return channel;
    }

    /** Binds a function here (context, labels and policy checked) and then on the server, as a handle.
     *
     * @param ref the function's reference
     * @param options its context, labels and policy
     * @return a {@link ChannelBoundFunction}; close it to release the handle
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the server does not serve it
     * @throws JeffError.TooManyHandles when the server holds as many handles as it allows
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.InvalidLabels},
     *     {@link JeffError.InvalidPolicy}, ...) and the server's */
    @Override
    public BoundFunction function(String ref, CallOptions options) {
        return ChannelBoundFunction.create(this, new Call(ref, null, options.context(), options.labels()), options.onLowConfidence());
    }

    /** Closes the channel; calling it again does nothing more. */
    @Override
    public void close() {
        if (closed.compareAndSet(false, true)) channel.close();
    }
}
