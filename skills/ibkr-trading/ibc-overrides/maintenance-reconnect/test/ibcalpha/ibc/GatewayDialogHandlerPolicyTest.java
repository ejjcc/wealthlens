package ibcalpha.ibc;

import java.time.LocalTime;

public final class GatewayDialogHandlerPolicyTest {
    private static final String TLS =
            "Connection to server failed: Failed send NSMsg - " +
            "javax.net.ssl.SSLHandshakeException: Remote host terminated the handshake";
    private static final String WINDOWS = "04:40-06:15,20:10-21:20";

    private static void expect(boolean actual, boolean wanted, String name) {
        if (actual != wanted) {
            throw new AssertionError(name + ": wanted=" + wanted + " actual=" + actual);
        }
    }

    public static void main(String[] args) {
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS, LocalTime.of(5, 0), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                true, "authenticated 05:00 TLS reset");
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS + "\n\nIBKR GATEWAY", LocalTime.of(5, 0),
                LoginManager.LoginState.LOGGED_IN, WINDOWS),
                true, "exact TLS reset with Gateway footer");
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS, LocalTime.of(20, 15), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                true, "authenticated 20:15 TLS reset");
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS, LocalTime.of(6, 16), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                false, "outside maintenance window");
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS, LocalTime.of(5, 0), LoginManager.LoginState.LOGGING_IN, WINDOWS),
                false, "never suppress during fresh login");
        expect(GatewayDialogHandler.shouldKeepSession(
                "Connection to server failed: Soft token=0 received instead of expected permanent",
                LocalTime.of(20, 15), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                false, "never suppress expired token");
        expect(GatewayDialogHandler.shouldKeepSession(
                "Connection to server failed: Server disconnected, please try again",
                LocalTime.of(5, 0), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                false, "only suppress exact TLS signature");
        expect(GatewayDialogHandler.shouldKeepSession(
                TLS + " Soft token=0 received instead of expected permanent",
                LocalTime.of(5, 0), LoginManager.LoginState.LOGGED_IN, WINDOWS),
                false, "reject exact-signature prefix with extra error text");
        expect(GatewayDialogHandler.isInConfiguredWindow(
                LocalTime.of(0, 30), "23:00-01:00"),
                true, "cross-midnight window");
        expect(GatewayDialogHandler.isInConfiguredWindow(
                LocalTime.of(5, 0), "not-a-window"),
                false, "invalid config fails closed");
        expect(GatewayDialogHandler.isInConfiguredWindow(
                LocalTime.of(5, 0), "04:40-06:15,not-a-window"),
                false, "mixed valid and invalid config fails closed");
        expect(GatewayDialogHandler.isInConfiguredWindow(
                LocalTime.of(5, 0), "04:40-06:15,"),
                false, "trailing empty range fails closed");
        expect(GatewayDialogHandler.isInConfiguredWindow(
                LocalTime.of(5, 0), "05:00-05:00"),
                false, "equal endpoints fail closed");

        System.out.println("GatewayDialogHandler policy tests: PASS");
    }
}
