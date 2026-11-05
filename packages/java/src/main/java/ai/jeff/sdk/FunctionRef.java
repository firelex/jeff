package ai.jeff.sdk;

import java.util.Arrays;

/** A reference to an adapter or function: jeff.<domain>.<name>[.<function>][@<version>], or
 * <alias>[.<function>][@<version>] (port of jeff/ids.py). `key` and `version` are null when not given. */
record FunctionRef(String adapter, String key, String version) {
    public static FunctionRef parse(String ref) {
        int at = ref.indexOf('@');
        String text = at < 0 ? ref : ref.substring(0, at);
        String version = at < 0 ? "" : ref.substring(at + 1);
        String[] parts = text.split("\\.", -1);
        if (text.isEmpty() || Arrays.stream(parts).anyMatch(String::isEmpty)) {
            throw new JeffError.InvalidReference("Malformed reference " + PyFormat.str(ref) + ": use jeff.<domain>.<name>"
                + "[.<function>] or <alias>[.<function>], each part non-empty");
        }
        String adapter;
        String key;
        if (parts[0].equals("jeff")) {
            if (parts.length < 3) {
                throw new JeffError.InvalidReference("Malformed reference " + PyFormat.str(ref) + ": use jeff.<domain>.<name>[.<function>]");
            }
            if (parts.length > 4) throw new JeffError.InvalidReference("Malformed reference " + PyFormat.str(ref) + ": too many parts");
            adapter = parts.length == 4 ? String.join(".", parts[0], parts[1], parts[2]) : text;
            key = parts.length == 4 ? parts[3] : null;
        } else {
            if (parts.length > 2) {
                throw new JeffError.InvalidReference("Malformed reference " + PyFormat.str(ref) + ": use <alias>[.<function>] "
                    + "(aliases have no dots)");
            }
            adapter = parts[0];
            key = parts.length == 2 ? parts[1] : null;
        }
        return new FunctionRef(adapter, key, version.isEmpty() ? null : version);
    }

    /** Whether `actual` has the dotted parts of `wanted` (1.3 matches 1.3.0, not 1.30.0). */
    public static boolean versionMatches(String wanted, String actual) {
        String[] want = wanted.split("\\.", -1);
        String[] have = actual.split("\\.", -1);
        if (have.length < want.length) return false;
        for (int index = 0; index < want.length; index++) if (!want[index].equals(have[index])) return false;
        return true;
    }
}
