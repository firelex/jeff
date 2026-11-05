package ai.jeff.sdk;

/** Options of Jeff.local: how long to wait for each reply from the engine, in milliseconds, and the manifests calls
 * are bound with (null: the catalogue shipped with the package).
 *
 * @param timeoutMs how long to wait for each reply from the engine, in milliseconds (more than 0)
 * @param catalogue the manifests calls are bound with, or {@code null} for the shipped catalogue */
public record LocalOptions(long timeoutMs, Catalogue catalogue) {
    /** Options with this timeout and the shipped catalogue.
     *
     * @param timeoutMs how long to wait for each reply from the engine, in milliseconds
     * @return the options */
    public static LocalOptions timeoutMs(long timeoutMs) {
        return new LocalOptions(timeoutMs, null);
    }

    /** These options with another catalogue.
     *
     * @param newCatalogue the manifests calls are bound with, or {@code null} for the shipped catalogue
     * @return new options */
    public LocalOptions withCatalogue(Catalogue newCatalogue) {
        return new LocalOptions(timeoutMs, newCatalogue);
    }
}
