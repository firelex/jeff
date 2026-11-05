package ai.jeff.sdk;

import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.util.concurrent.Executors;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

/** A self-signed certificate made with keytool for the names in `san` (keytool's SAN syntax), an HTTPS test server
 * presenting it, and a client TLS context that trusts it. */
record TestTls(SSLContext server, SSLContext client) {
    static TestTls create(String san) {
        try {
            Path store = Files.createTempDirectory("jeff-tls").resolve("server.p12");
            Process keytool = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "keytool").toString(),
                "-genkeypair", "-alias", "jeff", "-keyalg", "EC", "-groupname", "secp256r1", "-dname", "CN=jeff-test",
                "-ext", "SAN=" + san, "-validity", "2", "-keystore", store.toString(), "-storetype", "PKCS12",
                "-storepass", "secret", "-keypass", "secret").redirectErrorStream(true).start();
            String output = new String(keytool.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            if (keytool.waitFor() != 0) throw new IllegalStateException("keytool failed: " + output);
            KeyStore keys = KeyStore.getInstance("PKCS12");
            try (InputStream in = Files.newInputStream(store)) {
                keys.load(in, "secret".toCharArray());
            }
            KeyManagerFactory keyManagers = KeyManagerFactory.getInstance("PKIX");
            keyManagers.init(keys, "secret".toCharArray());
            SSLContext server = SSLContext.getInstance("TLS");
            server.init(keyManagers.getKeyManagers(), null, null);
            KeyStore trusted = KeyStore.getInstance("PKCS12");
            trusted.load(null, null);
            trusted.setCertificateEntry("jeff", keys.getCertificate("jeff"));
            TrustManagerFactory trust = TrustManagerFactory.getInstance("PKIX");
            trust.init(trusted);
            SSLContext client = SSLContext.getInstance("TLS");
            client.init(null, trust.getTrustManagers(), null);
            return new TestTls(server, client);
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        } catch (GeneralSecurityException error) {
            throw new IllegalStateException(error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(error);
        }
    }

    /** An HTTPS server on the loopback address answering every request with `body` as JSON. */
    HttpsServer serve(byte[] body) {
        try {
            HttpsServer https = HttpsServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            https.setHttpsConfigurator(new HttpsConfigurator(server));
            https.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
            https.createContext("/", exchange -> {
                exchange.getResponseHeaders().add("content-type", "application/json");
                exchange.sendResponseHeaders(200, body.length);
                try (OutputStream out = exchange.getResponseBody()) {
                    out.write(body);
                }
            });
            https.start();
            return https;
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }
}
