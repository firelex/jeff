package ai.jeff.sdk;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** An HTTP proxy for tests: CONNECT opens a tunnel (or is answered with `refusal`, when given); any other request,
 * whose target is a whole http:// URL, is passed on to that server with its target made a path. Each first request on
 * a connection is recorded, then the bytes flow both ways untouched. */
final class TestProxy implements AutoCloseable {
    private final ServerSocket listener;
    private final List<Socket> sockets = Collections.synchronizedList(new ArrayList<>());
    final List<RawHttpServer.Request> seen = Collections.synchronizedList(new ArrayList<>());

    TestProxy(String refusal) {
        try {
            listener = new ServerSocket(0, 64, InetAddress.getLoopbackAddress());
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
        Thread.ofVirtual().start(() -> {
            while (true) {
                Socket client;
                try {
                    client = listener.accept();
                } catch (IOException closed) {
                    return; // the proxy was closed
                }
                sockets.add(client);
                Thread.ofVirtual().start(() -> serve(client, refusal));
            }
        });
    }

    int port() {
        return listener.getLocalPort();
    }

    private void serve(Socket client, String refusal) {
        try (client) {
            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();
            RawHttpServer.Request request = RawHttpServer.read(in);
            if (request == null) return;
            seen.add(request);
            String[] parts = request.line().split(" ");
            Socket server;
            if (parts[0].equals("CONNECT")) {
                if (refusal != null) {
                    out.write(("HTTP/1.1 " + refusal + "\r\nContent-Length: 0\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
                    out.flush();
                    return;
                }
                String[] target = parts[1].split(":");
                server = new Socket(target[0], Integer.parseInt(target[1]));
                out.write("HTTP/1.1 200 Connection established\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
            } else {
                URI target = URI.create(parts[1]);
                server = new Socket(target.getHost(), target.getPort());
                StringBuilder head = new StringBuilder(parts[0] + " " + target.getRawPath() + " " + parts[2] + "\r\n");
                for (String field : request.headers()) head.append(field).append("\r\n");
                head.append("\r\n");
                server.getOutputStream().write(head.toString().getBytes(StandardCharsets.ISO_8859_1));
                server.getOutputStream().write(request.body());
                server.getOutputStream().flush();
            }
            sockets.add(server);
            try (server) {
                Thread back = Thread.ofVirtual().start(() -> pipe(server, client));
                pipe(client, server);
                back.join();
            }
        } catch (IOException ended) {
            // a side closed the connection
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static void pipe(Socket from, Socket to) {
        try {
            from.getInputStream().transferTo(to.getOutputStream());
            to.shutdownOutput();
        } catch (IOException ended) {
            // a side closed the connection
        }
    }

    @Override
    public void close() {
        try {
            listener.close();
            synchronized (sockets) {
                for (Socket socket : sockets) socket.close();
            }
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }
}
