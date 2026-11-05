package ai.jeff.sdk;

import java.util.List;
import java.util.SequencedMap;

/** A function's labels as its manifest declares them: {@code fixed} and {@code fixedFirst} map each key to its description (or
 * null), in document order; {@code callerKeys}, {@code max} and {@code subset} are null when the manifest leaves them out.
 *
 * @param source where the labels come from: {@code "fixed"} (the manifest's own), {@code "caller"} (the call
 *     gives them, key to description), {@code "subset"} (the call picks some of the fixed ones) or {@code "describe"}
 *     (the call describes the manifest's keys)
 * @param fixed the fixed labels, key to description (or null)
 * @param levels the levels of a score function, lowest first; empty for other functions
 * @param fixedFirst labels that come before the caller's, key to description (or null)
 * @param callerKeys what the caller's keys are, or null
 * @param max the most labels a call may give, or null
 * @param required the subset label keys every call must keep
 * @param keys the keys a call of describe labels describes
 * @param subset for the fixed labels of a choice function, whether the caller may offer a subset of them; null
 *     everywhere else */
public record LabelSpec(String source, SequencedMap<String, String> fixed, List<String> levels,
                        SequencedMap<String, String> fixedFirst, String callerKeys, Long max, List<String> required,
                        List<String> keys, Boolean subset) {}
