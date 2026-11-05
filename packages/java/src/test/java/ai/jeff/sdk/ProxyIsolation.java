package ai.jeff.sdk;

import java.util.Map;
import org.junit.platform.launcher.LauncherSession;
import org.junit.platform.launcher.LauncherSessionListener;

/** Runs once when the test run starts (registered in META-INF/services): every transport a test builds without an
 * explicit environment chooses its proxy from an empty one, so HTTP_PROXY, HTTPS_PROXY, ALL_PROXY or NO_PROXY set on
 * the machine never change a test's route. The proxy tests in RemoteHttpTest pass their own environments. */
public final class ProxyIsolation implements LauncherSessionListener {
    @Override
    public void launcherSessionOpened(LauncherSession session) {
        HttpOptions.pinnedEnvironment = Map.of();
    }
}
