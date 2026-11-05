package ai.jeff.sdk;

import java.util.Objects;
import java.util.function.Supplier;

/** The bearer token of a remote Jeff: a fixed string, a function returning the current token (called before every
 * HTTP request, so it can refresh), or none for a server with no credentials. No toString() or error message ever
 * shows a token. */
public sealed interface TokenSource permits TokenSource.Fixed, TokenSource.FromFunction, TokenSource.None {
    /** A fixed token, from {@link #of(String)}. */
    final class Fixed implements TokenSource {
        final String token;

        private Fixed(String token) {
            this.token = token;
        }

        /** Names the kind of source and never shows the token.
         *
         * @return {@code TokenSource.Fixed[the token is not shown]} */
        @Override
        public String toString() {
            return "TokenSource.Fixed[the token is not shown]";
        }
    }

    /** A function returning the current token, from {@link #from(Supplier)}. */
    final class FromFunction implements TokenSource {
        final Supplier<String> supplier;

        private FromFunction(Supplier<String> supplier) {
            this.supplier = supplier;
        }

        /** Names the kind of source.
         *
         * @return {@code TokenSource.FromFunction} */
        @Override
        public String toString() {
            return "TokenSource.FromFunction";
        }
    }

    /** No token, from {@link #none()}: only for a server with no credentials. */
    final class None implements TokenSource {
        private static final None INSTANCE = new None();

        private None() {}

        /** Names the kind of source.
         *
         * @return {@code TokenSource.None} */
        @Override
        public String toString() {
            return "TokenSource.None";
        }
    }

    /** A fixed token, never refreshed. A token that is empty or not printable ASCII raises
     * {@link JeffError.Unauthorised} when it is first used, without showing it.
     *
     * @param token the bearer token
     * @return the source
     * @throws NullPointerException when the token is null (use {@link #none()} for a server with no credentials) */
    static TokenSource of(String token) {
        return new Fixed(Objects.requireNonNull(token, "token is null; use TokenSource.none() for a server with no credentials"));
    }

    /** A function returning the current token: called before every HTTP request, and on the channel when its token
     * is about to expire. A token it returns that is null, empty or not printable ASCII raises
     * {@link JeffError.Unauthorised}, without showing it.
     *
     * @param supplier the function
     * @return the source */
    static TokenSource from(Supplier<String> supplier) {
        return new FromFunction(Objects.requireNonNull(supplier, "supplier"));
    }

    /** No token: only for a server with no credentials.
     *
     * @return the source */
    static TokenSource none() {
        return None.INSTANCE;
    }
}
