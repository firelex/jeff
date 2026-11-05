package ai.jeff.sdk;

import java.util.Map;
import java.util.function.Supplier;
import javax.net.ssl.SSLSocketFactory;

/** How a RestTransport reaches its server: its pool's limits, how connections are opened, its TLS sockets and the
 * environment its proxy is chosen from. standard() is what Jeff.remote uses (httpx's limits, the JDK's network and
 * trust store, the process environment); tests replace parts. */
record HttpOptions(ConnectionPool pool, HttpConnection.Opener opener, Supplier<SSLSocketFactory> tls, Map<String, String> environment) {
    /** For tests: the environment standard() chooses proxies from, in place of the process's. The test run pins it to
     * none once (ProxyIsolation), so the machine's proxy variables never reach a test that does not test proxies;
     * proxy tests pass their own environments with withEnvironment. Null: the process's environment. */
    static volatile Map<String, String> pinnedEnvironment;

    static HttpOptions standard() {
        Map<String, String> pinned = pinnedEnvironment;
        return new HttpOptions(new ConnectionPool(ConnectionPool.MAX_CONNECTIONS, ConnectionPool.MAX_IDLE, ConnectionPool.KEEPALIVE_MS),
            HttpConnection.DIRECT, HttpConnection::defaultTls, pinned != null ? pinned : System.getenv());
    }

    HttpOptions withPool(ConnectionPool newPool) {
        return new HttpOptions(newPool, opener, tls, environment);
    }

    HttpOptions withOpener(HttpConnection.Opener newOpener) {
        return new HttpOptions(pool, newOpener, tls, environment);
    }

    HttpOptions withTls(Supplier<SSLSocketFactory> newTls) {
        return new HttpOptions(pool, opener, newTls, environment);
    }

    HttpOptions withEnvironment(Map<String, String> newEnvironment) {
        return new HttpOptions(pool, opener, tls, newEnvironment);
    }
}
