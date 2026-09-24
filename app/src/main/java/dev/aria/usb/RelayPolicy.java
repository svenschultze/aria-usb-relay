package dev.aria.usb;

import java.util.ArrayList;
import java.util.List;

/** Android-independent intent, generation and retry policy. All times are monotonic. */
final class RelayPolicy {
    static final int MAX_RETRIES = 7;
    private long generation;
    private boolean wanted;
    private int failures;
    private long retryAt;

    synchronized void start() { wanted = true; generation++; failures = 0; retryAt = 0; }
    synchronized void stop() { wanted = false; generation++; failures = 0; retryAt = 0; }
    synchronized boolean wanted() { return wanted; }
    synchronized long beginAttempt() { return ++generation; }
    synchronized void invalidate() { generation++; }
    synchronized boolean current(long token) { return wanted && generation == token; }
    synchronized void resetRetries() { failures = 0; retryAt = 0; }
    synchronized boolean exhausted() { return failures >= MAX_RETRIES; }
    synchronized long retryAt() { return retryAt; }
    synchronized int failures() { return failures; }
    synchronized boolean canAttempt(long now) { return wanted && !exhausted() && now >= retryAt; }
    synchronized long failed(long token, long now) {
        if (!current(token)) return -1;
        failures++;
        long delay = Math.min(30000, 1000L << Math.min(failures - 1, 5));
        retryAt = now + delay;
        return exhausted() ? -1 : delay;
    }

    static final class Device {
        final String path, serial;
        Device(String path, String serial) { this.path = path; this.serial = serial == null ? "" : serial; }
    }

    /** Never switch to a different known serial or arbitrarily pick among candidates. */
    static String choose(List<Device> devices, String selectedPath, String selectedSerial) {
        String serial = selectedSerial == null ? "" : selectedSerial;
        List<Device> compatible = new ArrayList<>();
        for (Device device : devices) {
            if (!serial.isEmpty() && !device.serial.isEmpty() && !serial.equals(device.serial)) continue;
            compatible.add(device);
            if (device.path.equals(selectedPath)) return device.path;
        }
        if (!serial.isEmpty()) {
            Device matched = null;
            for (Device device : compatible) if (serial.equals(device.serial)) {
                if (matched != null) return null;
                matched = device;
            }
            if (matched != null) return matched.path;
        }
        return compatible.size() == 1 ? compatible.get(0).path : null;
    }
}
