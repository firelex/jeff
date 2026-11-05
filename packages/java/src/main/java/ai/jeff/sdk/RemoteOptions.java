package ai.jeff.sdk;

/** Options of Jeff.remote: how long each phase of a request may take, in milliseconds (over HTTP: connecting;
 * sending the request until the answer's headers; each wait for more of the answer's body; with the channel: opening
 * it, its hello, each request's wait for its turn to be written, each stall of its upload, its wait for its reply once
 * sent, and the closing handshake), the manifests calls are bound with (null: the catalogue shipped with the
 * package), and whether to use the server's WebSocket channel (lower latency; bound functions become handles on the
 * server) instead of one HTTP request per operation.
 *
 * @param timeoutMs how long each phase of a request may take, in milliseconds (more than 0)
 * @param catalogue the manifests calls are bound with, or {@code null} for the shipped catalogue
 * @param channel whether to use the server's WebSocket channel */
public record RemoteOptions(long timeoutMs, Catalogue catalogue, boolean channel) {
    /** Options with this timeout, the shipped catalogue, and HTTP (no channel).
     *
     * @param timeoutMs how long each phase of a request may take, in milliseconds
     * @return the options */
    public static RemoteOptions timeoutMs(long timeoutMs) {
        return new RemoteOptions(timeoutMs, null, false);
    }

    /** These options with another catalogue.
     *
     * @param newCatalogue the manifests calls are bound with, or {@code null} for the shipped catalogue
     * @return new options */
    public RemoteOptions withCatalogue(Catalogue newCatalogue) {
        return new RemoteOptions(timeoutMs, newCatalogue, channel);
    }

    /** These options with or without the WebSocket channel.
     *
     * @param useChannel {@code true} for the channel, {@code false} for one HTTP request per operation
     * @return new options */
    public RemoteOptions withChannel(boolean useChannel) {
        return new RemoteOptions(timeoutMs, catalogue, useChannel);
    }
}
