package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.List;

/** Run as its own JVM: makes one predict over the engine socket at args[0], prints its probabilities and never closes
 * the engine, so the JVM ends only once nothing keeps it alive. */
public final class OneCall {
    private OneCall() {}

    public static void main(String[] args) {
        SocketEngine engine = SocketEngine.connect(args[0], 5000);
        EngineResult result = engine.predict("jeff.safety.spam", List.of(new Row("x", new RowQuestion("noul", "?", null))));
        List<Object> rows = new ArrayList<>();
        for (double[] row : result.probabilities()) {
            List<Object> values = new ArrayList<>();
            for (double value : row) values.add(value);
            rows.add(values);
        }
        System.out.println(Json.write(rows));
    }
}
