import ai.jeff.sdk.*;
import java.util.Map;

public class ConnectToken {
    // Called before every request, so a token refreshed here (read again from the environment, or fetched from your
    // secret store) is used from the next request on.
    static String currentToken() {
        String token = System.getenv("JEFF_API_KEY");

        if (token == null) throw new IllegalStateException("Set JEFF_API_KEY");

        return token;
    }

    public static void main(String[] args) {
        try (RemoteJeff jeff = Jeff.remote("{{origin}}", TokenSource.from(ConnectToken::currentToken), RemoteOptions.timeoutMs(30000))) {
            BoundFunction guard = jeff.function("guard.attempt", CallOptions.none().context(Map.of(
                "application", "A support chat assistant for an online shop.",
                "source", "user message")));

            System.out.println(guard.call("What are your opening hours?").value());
        }
    }
}
