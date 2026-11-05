package ai.jeff.sdk;

import java.util.List;
import java.util.SequencedMap;

/** One function of an adapter. {@code calibration} is its bfloat16 threshold (null: uncalibrated); {@code calibrationByFormat}
 * holds its INT4 bases' thresholds by format_id.
 *
 * @param adapterId the adapter's id
 * @param key the function's key within the adapter
 * @param id the function's id: the adapter id, a dot, and the key
 * @param interfaceName the interface it implements ({@code classify}, {@code route}, {@code rate}, ...)
 * @param type its question type
 * @param instructions what the model is asked
 * @param labels its labels as the manifest declares them
 * @param abstainLabels the labels whose choice abstains
 * @param calibration its bfloat16 threshold, or null when uncalibrated
 * @param context its context fields
 * @param input its input
 * @param calibrationByFormat its INT4 bases' thresholds, by format_id */
public record FunctionSpec(String adapterId, String key, String id, String interfaceName, String type, String instructions,
                           LabelSpec labels, List<String> abstainLabels, Calibration calibration,
                           List<ContextField> context, InputField input,
                           SequencedMap<String, Calibration> calibrationByFormat) {}
