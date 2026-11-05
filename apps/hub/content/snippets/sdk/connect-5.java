// offline
import ai.jeff.sdk.*;

public class ConnectMistakes {
    public static void main(String[] args) {
        // Both mistakes are refused at once, before any network call or connection.
        try (RemoteJeff jeff = Jeff.remote("{{origin}}", TokenSource.none(), RemoteOptions.timeoutMs(0))) {
            throw new IllegalStateException("a timeout of 0 was accepted by " + jeff.url());
        } catch (JeffError.InvalidRequest error) {
            System.out.println(error.getMessage());
        }

        try (Jeff jeff = Jeff.local("/tmp/jeff.sock", LocalOptions.timeoutMs(0))) {
            throw new IllegalStateException("a timeout of 0 was accepted by " + jeff.baseFormat());
        } catch (JeffError.InvalidRequest error) {
            System.out.println(error.getMessage());
        }
    }
}
