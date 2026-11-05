package ai.jeff.sdk;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicInteger;

/** A test server speaking raw bytes: each accepted connection is handed to `conversation` on its own virtual thread,
 * which reads requests with readRequest and writes whatever bytes the test wants. close() closes the listening socket
 * and every connection. */
final class RawHttpServer implements AutoCloseable {
    /** What one connection does; `index` counts connections from 0. */
    interface Conversation {
        void run(Socket socket, int index) throws IOException;
    }

    /** One request as read: its request line, header lines and body. */
    record Request(String line, List<String> headers, byte[] body) {
        String header(String name) {
            for (String field : headers) {
                int colon = field.indexOf(':');
                if (field.substring(0, colon).strip().toLowerCase(Locale.ROOT).equals(name)) return field.substring(colon + 1).strip();
            }
            return null;
        }
    }

    private final ServerSocket listener;
    private final List<Socket> sockets = Collections.synchronizedList(new ArrayList<>());
    final AtomicInteger connections = new AtomicInteger();
    final AtomicInteger requests = new AtomicInteger();

    RawHttpServer(Conversation conversation) {
        try {
            listener = new ServerSocket(0, 512, InetAddress.getLoopbackAddress());
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
        Thread.ofVirtual().start(() -> {
            while (!listener.isClosed()) {
                Socket socket;
                try {
                    socket = listener.accept();
                } catch (IOException closed) {
                    return; // the server was closed
                }
                sockets.add(socket);
                int index = connections.getAndIncrement();
                Thread.ofVirtual().start(() -> {
                    try (socket) {
                        conversation.run(socket, index);
                    } catch (IOException ended) {
                        // the client or the test closed the connection
                    }
                });
            }
        });
    }

    String url() {
        return "http://127.0.0.1:" + listener.getLocalPort();
    }

    int port() {
        return listener.getLocalPort();
    }

    /** The next request on the connection, or null when the client closed it first; counted in `requests`. */
    Request readRequest(InputStream in) throws IOException {
        Request request = read(in);
        if (request != null) requests.incrementAndGet();
        return request;
    }

    /** The next request on the connection, or null when the client closed it first. */
    static Request read(InputStream in) throws IOException {
        String line = readLine(in);
        if (line == null) return null;
        List<String> headers = new ArrayList<>();
        int length = 0;
        while (true) {
            String field = readLine(in);
            if (field == null || field.isEmpty()) break;
            headers.add(field);
            if (field.toLowerCase(Locale.ROOT).startsWith("content-length:")) length = Integer.parseInt(field.substring(15).strip());
        }
        byte[] body = in.readNBytes(length);
        return new Request(line, headers, body);
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        while (true) {
            int next = in.read();
            if (next < 0) return line.size() == 0 ? null : line.toString(StandardCharsets.ISO_8859_1);
            if (next == '\n') break;
            if (next != '\r') line.write(next);
        }
        return line.toString(StandardCharsets.ISO_8859_1);
    }

    /** An answer's bytes: status line, the given header lines, Content-Length, then the body. */
    static byte[] answer(int status, List<String> headers, byte[] body) {
        StringBuilder head = new StringBuilder("HTTP/1.1 " + status + " X\r\n");
        for (String field : headers) head.append(field).append("\r\n");
        head.append("Content-Length: ").append(body.length).append("\r\n\r\n");
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.writeBytes(head.toString().getBytes(StandardCharsets.ISO_8859_1));
        out.writeBytes(body);
        return out.toByteArray();
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
