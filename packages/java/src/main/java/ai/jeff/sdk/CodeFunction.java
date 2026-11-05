package ai.jeff.sdk;

/** A code node's function: given the graph's input, its params and every result so far, it returns a JSON value
 * (null included). */
@FunctionalInterface
public interface CodeFunction {
    /** Runs the code node.
     *
     * @param call the graph's input, its params and every node result so far
     * @return the node's value: any JSON value, {@code null} included */
    Object run(CodeCall call);
}
