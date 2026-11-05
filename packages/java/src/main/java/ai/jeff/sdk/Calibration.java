package ai.jeff.sdk;

import java.util.Map;

/** A function's calibrated threshold, as manifests and {@code GET /v1/functions} write it (in snake_case there): a
 * decision whose confidence is below {@code threshold} abstains.
 *
 * @param data the data set the threshold was measured on
 * @param rows how many rows of it were used
 * @param targetPrecision the precision the threshold was chosen to reach
 * @param threshold the confidence below which a decision abstains
 * @param precisionAtThreshold the precision measured at the threshold
 * @param coverageAtThreshold the share of rows at or above the threshold
 * @param dataMirror where a copy of the data is kept, or null when not given
 * @param excluded why rows were left out (only on an INT4 base's entry), or null when not given */
public record Calibration(String data, long rows, double targetPrecision, double threshold, double precisionAtThreshold,
                          double coverageAtThreshold, DataMirror dataMirror, String excluded) {
    /** Where a copy of a calibration's data is kept.
     *
     * @param path the copy's path
     * @param sha256 the copy's SHA-256 digest */
    public record DataMirror(String path, String sha256) {}

    /** A calibration block a schema has already checked. A required key that is absent, or a row count past 64 bits
     * (which Python's int would hold), is a ContractError naming it, never a default. */
    static Calibration fromWire(Map<String, Object> block) {
        DataMirror mirror = null;
        if (block.get("data_mirror") instanceof Map<?, ?> given) {
            mirror = new DataMirror((String) required(Values.map(given), "path", "calibration data_mirror"),
                (String) required(Values.map(given), "sha256", "calibration data_mirror"));
        }
        if (!(required(block, "rows", "calibration") instanceof Long rows)) {
            throw new JeffError.ContractError("calibration rows " + PyFormat.repr(block.get("rows")) + " is not a whole "
                + "number of at most 64 bits");
        }
        return new Calibration((String) required(block, "data", "calibration"), rows,
            number(block, "target_precision"), number(block, "threshold"), number(block, "precision_at_threshold"),
            number(block, "coverage_at_threshold"), mirror, (String) block.get("excluded"));
    }

    private static Object required(Map<String, Object> block, String key, String what) {
        if (!block.containsKey(key)) throw new JeffError.ContractError(what + " block has no '" + key + "'");
        return block.get(key);
    }

    private static double number(Map<String, Object> block, String key) {
        return ((Number) required(block, key, "calibration")).doubleValue();
    }
}
