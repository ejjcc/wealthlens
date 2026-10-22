// Local override for IBC 3.23.0's GatewayDialogHandler.
//
// Upstream turns every "Connection to server failed" dialog into a cold
// restart.  During IBKR's daily APAC reset this machine instead sees a
// transient TLS handshake failure while the session is already authenticated.
// A cold restart discards the weekly session and forces a new 2FA login.
//
// For that one narrowly identified failure, while already LOGGED_IN and inside
// a configured local-time window, click OK and let Gateway perform its native
// reconnect.  Every other case retains the upstream cold-restart behaviour.

package ibcalpha.ibc;

import java.awt.Window;
import java.awt.event.WindowEvent;
import java.time.LocalTime;
import javax.swing.JDialog;

public class GatewayDialogHandler implements WindowHandler {

    static final String RETRY_WINDOWS_SETTING =
            "GatewayReconnectWithoutColdRestartWindows";

    private static final String CONNECTION_FAILURE_PREFIX =
            "Connection to server failed";
    private static final String TRANSIENT_TLS_FAILURE =
            "Connection to server failed: Failed send NSMsg - " +
            "javax.net.ssl.SSLHandshakeException: Remote host terminated the handshake";

    public GatewayDialogHandler() {
        Utils.logToConsole("Local Gateway maintenance reconnect override active");
    }

    @Override
    public boolean filterEvent(Window window, int eventId) {
        switch (eventId) {
            case WindowEvent.WINDOW_OPENED:
                return true;
            default:
                return false;
        }
    }

    @Override
    public void handleWindow(Window window, int eventID) {
        String text = SwingUtils.getLabelTexts(window);
        // Since this is a generic dialog, always log its text, matching upstream.
        Utils.logToConsole(text);

        if (text.startsWith(CONNECTION_FAILURE_PREFIX)) {
            String windows = Settings.settings().getString(RETRY_WINDOWS_SETTING, "");
            LoginManager.LoginState state = LoginManager.loginManager().getLoginState();

            if (shouldKeepSession(text, LocalTime.now(), state, windows)) {
                Utils.logToConsole(
                        "Transient TLS failure in configured maintenance window; " +
                        "preserving authenticated session for Gateway native reconnect");
                if (SwingUtils.clickButton(window, "OK")) {
                    return;
                }
                // Fail closed. A modal that cannot be dismissed cannot reconnect,
                // so retain upstream cold-restart recovery rather than hanging.
                Utils.logError(
                        "could not dismiss transient Gateway connection failure " +
                        "because the OK button was not found; falling back to cold restart");
            }

            Utils.logToConsole("Cold restart in progress");
            // Preserve upstream behaviour outside the narrow safe-retry case.
            MyCachedThreadPool.getInstance().execute(
                    new StopTask(null, true,
                            "Cold restart after Connection to server failed"));

            if (!SwingUtils.clickButton(window, "OK")) {
                Utils.logError(
                        "could not dismiss Login Error dialog because we could not " +
                        "find the OK button");
            }
        } else {
            // Other Gateway dialogs are deliberately left for the user, matching upstream.
        }
    }

    static boolean shouldKeepSession(
            String text,
            LocalTime now,
            LoginManager.LoginState state,
            String windows) {
        if (state != LoginManager.LoginState.LOGGED_IN) return false;
        if (!isExactTransientTlsFailure(text)) return false;
        return isInConfiguredWindow(now, windows);
    }

    static boolean isExactTransientTlsFailure(String text) {
        if (text == null) return false;
        String normalized = text.trim().replaceAll("\\s+", " ");
        return normalized.equals(TRANSIENT_TLS_FAILURE) ||
                normalized.equals(TRANSIENT_TLS_FAILURE + " IBKR GATEWAY");
    }

    static boolean isInConfiguredWindow(LocalTime now, String windows) {
        if (now == null || windows == null || windows.trim().isEmpty()) return false;

        String[] ranges = windows.split(",", -1);
        boolean matched = false;
        for (String range : ranges) {
            String[] bounds = range.trim().split("-", -1);
            if (bounds.length != 2) return false;

            try {
                LocalTime start = LocalTime.parse(bounds[0].trim());
                LocalTime end = LocalTime.parse(bounds[1].trim());

                // Equal endpoints are ambiguous and must not mean "all day".
                if (start.equals(end)) return false;
                if (start.isBefore(end)) {
                    if (!now.isBefore(start) && !now.isAfter(end)) matched = true;
                } else {
                    // Also support a window crossing midnight.
                    if (!now.isBefore(start) || !now.isAfter(end)) matched = true;
                }
            } catch (RuntimeException ignored) {
                // An invalid range fails closed: retain upstream cold restart.
                return false;
            }
        }
        return matched;
    }

    @Override
    public boolean recogniseWindow(Window window) {
        if (!(window instanceof JDialog)) return false;
        return SwingUtils.titleContains(window, "Gateway");
    }
}
