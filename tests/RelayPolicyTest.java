package dev.aria.usb;

import java.util.Arrays;
import java.util.Collections;

/** Run with assertions enabled. Exercises intent/USB lifecycle without Android. */
public final class RelayPolicyTest {
    public static void main(String[] args) {
        RelayPolicy policy = new RelayPolicy();
        assert !policy.wanted() && !policy.canAttempt(0);
        policy.start();
        long first = policy.beginAttempt();
        assert policy.current(first);
        policy.invalidate(); // Physical USB detach invalidates callbacks, not user intent.
        assert policy.wanted() && !policy.current(first);
        long reconnect = policy.beginAttempt();
        assert policy.failed(first, 0) == -1; // Old finally cannot schedule a new retry.
        assert policy.failures() == 0;
        long now = 0;
        long[] delays = {1000, 2000, 4000, 8000, 16000, 30000};
        for (long expected : delays) {
            assert policy.failed(reconnect, now) == expected;
            assert !policy.canAttempt(now + expected - 1);
            now += expected;
            assert policy.canAttempt(now);
            reconnect = policy.beginAttempt();
        }
        assert policy.failed(reconnect, now) == -1 && policy.exhausted();
        assert !policy.canAttempt(Long.MAX_VALUE);
        policy.resetRetries(); // Explicit retry or a new USB attachment.
        assert policy.canAttempt(now);
        policy.stop();
        assert !policy.wanted() && !policy.current(reconnect) && !policy.canAttempt(now);

        RelayPolicy.Device a = new RelayPolicy.Device("/usb/1", "A");
        RelayPolicy.Device b = new RelayPolicy.Device("/usb/2", "B");
        RelayPolicy.Device unknown = new RelayPolicy.Device("/usb/3", null);
        assert RelayPolicy.choose(Arrays.asList(a, b), "", "") == null;
        assert RelayPolicy.choose(Arrays.asList(a, b), a.path, "A").equals(a.path);
        assert RelayPolicy.choose(Arrays.asList(a, b), "/old/path", "A").equals(a.path);
        assert RelayPolicy.choose(Collections.singletonList(b), a.path, "A") == null;
        assert RelayPolicy.choose(Arrays.asList(b, unknown), "/old/path", "A").equals(unknown.path);
        assert RelayPolicy.choose(Collections.emptyList(), a.path, "A") == null;
        assert RelayPolicy.choose(Arrays.asList(a, new RelayPolicy.Device("/usb/4", "A")), "", "A") == null;
        System.out.println("PASS: relay intent, stale callbacks, bounded retries, stop, re-enumeration, device ambiguity and identity");
    }
}
