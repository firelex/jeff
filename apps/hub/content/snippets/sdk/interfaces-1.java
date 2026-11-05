// offline
import ai.jeff.sdk.*;
import java.util.List;

public class ListInterfaces {
    public static void main(String[] args) {
        Catalogue catalogue = Catalogue.shipped();

        for (String iface : List.of("classify", "route", "rate", "detect", "is_supported", "equivalent", "requires_escalation")) {
            List<String> functions = catalogue.implementing(iface).stream().map(FunctionSpec::id).toList();

            System.out.println(iface + ": " + functions.size() + " functions, for example " + functions.get(0));
        }
    }
}
