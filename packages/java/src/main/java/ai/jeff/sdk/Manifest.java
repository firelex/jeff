package ai.jeff.sdk;

import java.util.List;
import java.util.Map;
import java.util.SequencedMap;

/** An adapter manifest (contract/schemas/manifest.schema.json), checked. Read one with {@link Manifests#load}.
 *
 * @param id the adapter's id
 * @param version the adapter's version
 * @param aliases other names it is found by
 * @param licenceKind its licence's kind
 * @param weights where its weights are
 * @param baseId the base model it runs on
 * @param baseVersion the base model's version
 * @param maxOptions the most options one question may have on that base
 * @param context the adapter's context fields
 * @param input the adapter's input
 * @param functions its functions by key, in document order
 * @param routesTo for a router, the adapters it routes to; empty otherwise
 * @param tests its test settings
 * @param raw the document as it was read
 * @param path where it was read from
 * @param isRouter whether it is a router */
public record Manifest(String id, String version, List<String> aliases, String licenceKind, Map<String, Object> weights,
                       String baseId, String baseVersion, long maxOptions, List<ContextField> context, InputField input,
                       SequencedMap<String, FunctionSpec> functions, List<String> routesTo, Map<String, Object> tests,
                       Map<String, Object> raw, String path, boolean isRouter) {}
