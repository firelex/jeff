import ai.jeff.sdk.*;
import java.util.LinkedHashMap;
import java.util.Map;

public class Shorthands {
    public static void main(String[] args) {
        var shop = CallOptions.none().context(Map.of("company", "A furniture shop.", "channel", "email"));

        String message = "My wardrobe arrived with a broken door and I need it replaced before Friday.";

        var teams = new LinkedHashMap<String, Object>();
        teams.put("deliveries", "Deliveries and damaged goods");
        teams.put("refunds", "Refunds and returns");

        String token = System.getenv("JEFF_API_KEY");

        if (token == null) throw new IllegalStateException("Set JEFF_API_KEY");

        try (RemoteJeff jeff = Jeff.remote("{{origin}}", token, RemoteOptions.timeoutMs(30000))) {
            Decision team = jeff.route(message, "triage.route", shop.labels(teams));

            Decision urgency = jeff.rate(message, "triage.urgency", shop);

            Decision needsHuman = jeff.requiresEscalation(message, "triage.needs_human", shop);

            Decision kind = jeff.classify(message, "guard.kind", CallOptions.none().context(Map.of(
                "application", "A support assistant for a furniture shop.",
                "source", "email body")));

            System.out.println("team: " + team.value());

            System.out.printf("urgency level: %s expected level: %.2f%n", urgency.value(), urgency.score());

            System.out.println("a person must act now: " + needsHuman.value());

            System.out.println("kind of text: " + kind.value());
        }
    }
}
