// offline
import ai.jeff.sdk.*;
import java.util.List;

public class CatchErrors {
    public static void main(String[] args) {
        Catalogue catalogue = Catalogue.shipped();

        for (String ref : List.of("guard.atempt", "guard.attempt.extra", "guard")) {
            try {
                catalogue.function(ref);
            } catch (JeffError.UnknownFunction | JeffError.InvalidReference error) {
                System.out.println(error.type() + " - " + error.getMessage());
            }
        }

        // Every Jeff error is a JeffError, so one catch clause catches them all; requestId() is null for an error raised
        // in this program (a server's error carries the id of the request that failed).
        try {
            catalogue.function("guard.atempt");
        } catch (JeffError error) {
            System.out.println(error.type() + " " + error.requestId());
        }
    }
}
