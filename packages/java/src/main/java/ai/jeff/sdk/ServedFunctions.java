package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;
import java.util.Set;

/** The engine a RemoteJeff holds: the adapters the server serves and the base format its engines serve, read from
 * GET /v1/functions when it is built and again on installed(true). Each read checks that every served adapter the
 * local catalogue also has is the same version, and that the base format has not changed. It never predicts. */
final class ServedFunctions implements Engine {
    /** A function's bfloat16 calibration and its calibration_by_format entries, as the server lists them. */
    public record Listed(Calibration calibration, SequencedMap<String, Calibration> byFormat) {}

    private final Transport transport;
    private final Catalogue catalogue;
    private final String url;
    private final BaseFormat baseFormat;
    private volatile Map<String, Listed> calibrations;
    private volatile Set<String> served;

    private ServedFunctions(Transport transport, Catalogue catalogue, String url, Map<String, Object> answer) {
        this.transport = transport;
        this.catalogue = catalogue;
        this.url = url;
        this.baseFormat = BaseFormat.fromWire(Values.map(answer.get("base_format")));
        this.served = readListing(answer);
    }

    public static ServedFunctions read(Transport transport, Catalogue catalogue, String url) {
        return new ServedFunctions(transport, catalogue, url, transport.functions());
    }

    private Set<String> readListing(Map<String, Object> answer) {
        BaseFormat now = BaseFormat.fromWire(Values.map(answer.get("base_format")));
        if (!now.equals(baseFormat)) {
            throw new JeffError.ContractError("The server at " + url + " now serves a base of format " + PyFormat.repr(now.toMap())
                + ", not " + PyFormat.repr(baseFormat.toMap()) + " as when this client connected; the functions bound here use "
                + "that format's calibration. Connect again");
        }
        Map<String, String> versions = new LinkedHashMap<>();
        Map<String, Listed> listed = new LinkedHashMap<>();
        for (Object item : Values.list(answer.get("functions"))) {
            Map<String, Object> function = Values.map(item);
            String id = (String) function.get("id");
            int dot = id.lastIndexOf('.');
            // Python's id.rsplit(".", 1)[0]: an id with no dot is its own adapter id
            versions.put(dot < 0 ? id : id.substring(0, dot), (String) function.get("adapter_version"));
            Calibration calibration = function.get("calibration") == null ? null : Calibration.fromWire(Values.map(function.get("calibration")));
            // calibration_by_format is optional in functions_response: a function listed without it has no INT4 entries
            SequencedMap<String, Calibration> byFormat = new LinkedHashMap<>();
            if (function.get("calibration_by_format") instanceof Map<?, ?> entries) {
                for (Map.Entry<?, ?> entry : entries.entrySet()) byFormat.put((String) entry.getKey(), Calibration.fromWire(Values.map(entry.getValue())));
            }
            listed.put(id, new Listed(calibration, Values.frozenMap(byFormat)));
        }
        calibrations = Map.copyOf(listed);
        Map<String, String> local = new LinkedHashMap<>();
        for (Manifest manifest : catalogue.manifests()) local.put(manifest.id(), manifest.version());
        for (Map.Entry<String, String> entry : versions.entrySet()) {
            String mine = local.get(entry.getKey());
            if (mine != null && !mine.equals(entry.getValue())) throw RemoteJeff.versionMismatch(entry.getKey(), entry.getValue(), mine);
        }
        return Set.copyOf(versions.keySet());
    }

    public Map<String, Listed> calibrations() {
        return calibrations;
    }

    @Override
    public BaseFormat baseFormat() {
        return baseFormat;
    }

    @Override
    public Set<String> installed(boolean refresh) {
        if (refresh) served = readListing(transport.functions());
        return served;
    }

    @Override
    public EngineResult predict(String adapterId, List<Row> rows) {
        throw new JeffError.EngineError("RemoteJeff sends every decision to the server at " + url + "; nothing may call its "
            + "engine's predict (asked for " + rows.size() + " row(s) of " + adapterId + ")");
    }
}
