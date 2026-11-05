package ai.jeff.sdk;

/** The input of a manifest.
 *
 * @param key the input's key in the state the model reads
 * @param description what the input holds
 * @param codeName the input's code name
 * @param form {@code "fields"} (the input is a field of the state) or {@code "text"} (the state is the plain input
 *     string) */
public record InputField(String key, String description, String codeName, String form) {}
