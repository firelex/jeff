package ai.jeff.sdk;

/** A context field of a manifest: its exact key, description, whether it is required, and the code name callers use.
 *
 * @param key the field's key as the model reads it
 * @param description what the field holds
 * @param required whether a call must give it
 * @param codeName the name a call gives it by in its context */
public record ContextField(String key, String description, boolean required, String codeName) {}
