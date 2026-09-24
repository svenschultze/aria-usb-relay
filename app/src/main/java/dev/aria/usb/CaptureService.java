package dev.aria.usb;

import android.annotation.SuppressLint;
import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.hardware.usb.*;
import android.net.wifi.WifiManager;
import android.os.*;
import android.provider.Settings;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;

/** Owns an explicitly requested relay independently of the Activity and laptop. */
public final class CaptureService extends Service {
    public static final String CONNECT = "dev.aria.usb.CONNECT";
    public static final String CONNECT_BRIDGE = "dev.aria.usb.CONNECT_BRIDGE", STOP = "dev.aria.usb.STOP";
    private static final String USB_PERMISSION = "dev.aria.usb.SERVICE_USB_PERMISSION", CHANNEL = "aria_usb";
    public enum Action { NONE, PERMISSION, CHOOSE_DEVICE, RETRY }
    public static final class Snapshot {
        public final boolean wanted, relay, laptopConnected;
        public final String state, detail, device;
        public final long bytes;
        public final Action action;
        Snapshot(boolean wanted, boolean relay, boolean laptopConnected, String state,
                String detail, String device, long bytes, Action action) {
            this.wanted = wanted; this.relay = relay; this.laptopConnected = laptopConnected;
            this.state = state; this.detail = detail; this.device = device;
            this.bytes = bytes; this.action = action;
        }
    }
    private final RelayPolicy policy = new RelayPolicy();
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService cleanup = Executors.newSingleThreadExecutor();
    private final ExecutorService journalWorker = new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(128), new ThreadPoolExecutor.DiscardOldestPolicy());
    private final ArrayDeque<String> events = new ArrayDeque<>();
    private final Set<String> permissionAsked = new HashSet<>();
    private volatile Session session;
    private volatile Snapshot snapshot = new Snapshot(false, true, false, "Ready to relay",
            "Connect the glasses to this phone, then start the relay.", "No glasses selected", 0, Action.NONE);
    private UsbManager manager;
    private SharedPreferences preferences;
    private SessionJournal journal;
    private boolean relayMode = true;
    private volatile boolean destroyed;
    private String selectedPath = "", selectedSerial = "", explicitlySelectedPath = "", pendingPermission = "";
    private long permissionToken, transferred;
    private int permissionSequence;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private long lastWakeRenewal;
    private String notifiedState = "";
    private volatile String telemetry = "", probe = "";
    private volatile long lastSample, samples;

    private static final class Session {
        final long token;
        final UsbDevice device;
        final boolean relay;
        volatile boolean ready;
        volatile AdbConnection adb;
        volatile UsbIpServer bridge;
        Session(long token, UsbDevice device, boolean relay) {
            this.token = token; this.device = device; this.relay = relay;
        }
    }
    private final IBinder binder = new LocalBinder();
    public final class LocalBinder extends Binder { public CaptureService getService() { return CaptureService.this; } }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    @Override public void onCreate() {
        super.onCreate();
        manager = getSystemService(UsbManager.class);
        preferences = getSharedPreferences("aria_usb", MODE_PRIVATE);
        journal = new SessionJournal(getFilesDir());
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
                CHANNEL, "USB relay", NotificationManager.IMPORTANCE_LOW));
        IntentFilter filter = new IntentFilter(USB_PERMISSION);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_DETACHED);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(usbEvents, filter, RECEIVER_NOT_EXPORTED);
        else registerReceiver(usbEvents, filter);
    }
    @Override public IBinder onBind(Intent intent) { return binder; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && STOP.equals(intent.getAction())) {
            stopConnection("Relay stopped", "Start again whenever you need the connection.");
            return START_NOT_STICKY;
        }
        if (intent == null) {
            // Recover a previously requested session during this boot only. No boot receiver.
            if (!preferences.getBoolean("session_wanted", false) || bootCount() < 0
                    || preferences.getInt("session_boot", -1) != bootCount()) {
                stopSelf(startId); return START_NOT_STICKY;
            }
            relayMode = preferences.getBoolean("session_relay", true);
            selectedPath = ""; explicitlySelectedPath = ""; selectedSerial = preferences.getString("selected_serial", "");
        } else {
            if (!CONNECT_BRIDGE.equals(intent.getAction()) && !CONNECT.equals(intent.getAction()))
                return policy.wanted() ? START_STICKY : START_NOT_STICKY;
            relayMode = CONNECT_BRIDGE.equals(intent.getAction());
            String path = intent.getStringExtra("device_path"); selectedPath = path == null ? "" : path;
            explicitlySelectedPath = selectedPath;
            UsbDevice selected = manager.getDeviceList().get(selectedPath);
            selectedSerial = selected != null ? UsbInventory.serial(manager, selected)
                    : preferences.getString("selected_serial", "");
        }
        retireCurrent(); policy.start();
        transferred = 0; telemetry = ""; probe = ""; lastSample = 0; samples = 0;
        permissionAsked.clear(); pendingPermission = ""; permissionToken++;
        preferences.edit().putBoolean("session_wanted", true).putBoolean("relay_wanted", relayMode)
                .putBoolean("session_relay", relayMode).putInt("session_boot", bootCount())
                .putString("selected_serial", selectedSerial).apply();
        snapshot = new Snapshot(true, relayMode, false, "Starting relay", "Checking the USB connection…", "Finding glasses", 0, Action.NONE);
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification(snapshot), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
            else startForeground(1, notification(snapshot));
        } catch (RuntimeException error) {
            policy.stop(); persistStopped(); releaseLocks();
            snapshot = new Snapshot(false, relayMode, false, "Start needs attention",
                    "Android could not start the background relay: " + message(error), "No active device", 0, Action.NONE);
            log("foreground_error", message(error)); stopSelf(startId); return START_NOT_STICKY;
        }
        log("session", relayMode ? "Relay requested" : "Debug telemetry requested");
        main.removeCallbacks(tick); reconcile(); main.postDelayed(tick, 1000);
        return START_STICKY;
    }

    private final BroadcastReceiver usbEvents = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (USB_PERMISSION.equals(intent.getAction())) {
                String path = intent.getStringExtra("device_path");
                if (!policy.wanted() || !pendingPermission.equals(path)
                        || intent.getLongExtra("permission_token", -1) != permissionToken) return;
                pendingPermission = "";
                UsbDevice device = manager.getDeviceList().get(path);
                // Check Android's real grant, never trust a permission flag in a broadcast.
                if (device != null && manager.hasPermission(device)) {
                    String serial = UsbInventory.serial(manager, device);
                    if (!selectedSerial.isEmpty() && !serial.isEmpty() && !selectedSerial.equals(serial)) {
                        selectedPath = "";
                        publish("Choose your glasses", "The connected device has a different serial number. Choose it explicitly to switch.", Action.CHOOSE_DEVICE);
                        return;
                    }
                    selectedPath = path; rememberSerial(serial); policy.resetRetries(); reconcile();
                } else publish("USB permission needed", "Open the app and allow USB access. Permission may be needed again after a USB mode change.", Action.PERMISSION);
                return;
            }
            UsbDevice device = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
            if (device == null || !UsbInventory.isCandidate(device)) return;
            if (UsbManager.ACTION_USB_DEVICE_DETACHED.equals(intent.getAction())) {
                permissionAsked.remove(device.getDeviceName());
                if (device.getDeviceName().equals(pendingPermission)) { pendingPermission = ""; permissionToken++; }
                Session current = session;
                if (current != null && current.device.getDeviceName().equals(device.getDeviceName())) {
                    retireCurrent(); selectedPath = ""; explicitlySelectedPath = ""; releaseLocks();
                    log("usb", "Selected glasses detached; relay remains requested");
                }
            }
            if (policy.wanted()) { policy.resetRetries(); main.postDelayed(CaptureService.this::reconcile, 700); }
        }
    };
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (destroyed || !policy.wanted()) return;
            reconcile(); main.postDelayed(this, 1000);
        }
    };

    /** Main-thread transitions plus token checks isolate every asynchronous connection. */
    private void reconcile() {
        if (destroyed || !policy.wanted()) return;
        Session current = session;
        if (current != null) {
            if (!manager.getDeviceList().containsKey(current.device.getDeviceName())) {
                retireCurrent(); selectedPath = ""; explicitlySelectedPath = ""; policy.resetRetries(); releaseLocks();
            } else {
                holdLocks();
                if (current.ready && current.relay && current.bridge != null) {
                    boolean imported = current.bridge.isImported();
                    publish(imported ? "Laptop connected" : "Waiting for laptop",
                            imported ? "USB data is relayed in the background. You can turn off the screen."
                                    : "Keep the glasses connected and start the receiver on your laptop.", Action.NONE);
                }
                return;
            }
        }
        List<UsbDevice> candidates = UsbInventory.candidates(manager);
        if (candidates.isEmpty()) {
            releaseLocks(); publish("Waiting for glasses", "Connect the powered-on glasses with a USB data cable. The relay resumes when access is available.", Action.NONE); return;
        }
        List<RelayPolicy.Device> identities = new ArrayList<>();
        for (UsbDevice device : candidates) identities.add(new RelayPolicy.Device(device.getDeviceName(), UsbInventory.serial(manager, device)));
        String path = RelayPolicy.choose(identities, selectedPath, selectedSerial);
        if (path == null) {
            releaseLocks(); publish("Choose your glasses", "Several devices are available, or the previously selected glasses are absent. Open the app to choose.", Action.CHOOSE_DEVICE); return;
        }
        UsbDevice device = manager.getDeviceList().get(path);
        if (device == null) return;
        selectedPath = path;
        if (!manager.hasPermission(device)) {
            releaseLocks(); publish("USB permission needed", "Allow Android USB access. If no dialog is visible, open the app and tap Allow USB access.", Action.PERMISSION);
            if (!permissionAsked.contains(path)) requestPermission(device);
            return;
        }
        String serial = UsbInventory.serial(manager, device);
        if (!selectedSerial.isEmpty() && serial.isEmpty() && !path.equals(explicitlySelectedPath)) {
            releaseLocks(); publish("Choose your glasses", "Android did not expose a serial number for this attachment. Choose the device explicitly to continue.", Action.CHOOSE_DEVICE); return;
        }
        rememberSerial(serial);
        if (policy.exhausted()) {
            releaseLocks(); publish("Connection needs attention", "Automatic retries have paused. Check the cable, then tap Retry connection.", Action.RETRY); return;
        }
        long now = SystemClock.elapsedRealtime();
        if (!policy.canAttempt(now)) {
            releaseLocks(); publish("Reconnecting", "Retrying USB in " + Math.max(1, (policy.retryAt() - now + 999) / 1000) + " seconds…", Action.RETRY); return;
        }
        Session next = new Session(policy.beginAttempt(), device, relayMode);
        session = next; holdLocks(); publish("Opening USB", "Preparing the glasses connection…", Action.NONE);
        worker.execute(() -> runSession(next));
    }

    private void requestPermission(UsbDevice device) {
        permissionAsked.add(device.getDeviceName()); pendingPermission = device.getDeviceName(); permissionToken++;
        Intent result = new Intent(USB_PERMISSION).setPackage(getPackageName())
                .putExtra("device_path", pendingPermission).putExtra("permission_token", permissionToken);
        // The immutable base intent carries our own identity. Grant status is re-read
        // from UsbManager, so mutable system fill-in extras are unnecessary.
        PendingIntent pending = PendingIntent.getBroadcast(this, ++permissionSequence, result,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        try { manager.requestPermission(device, pending); }
        catch (RuntimeException error) { log("usb_permission", message(error)); }
    }
    public void retryOrRequestPermission() {
        if (!policy.wanted()) return;
        permissionAsked.remove(selectedPath); pendingPermission = ""; permissionToken++;
        policy.resetRetries(); reconcile();
    }

    private void runSession(Session current) {
        Exception failure = null;
        try {
            if (!isCurrent(current)) return;
            log("usb", UsbInventory.describe(manager));
            if (current.relay) runRelay(current); else runTelemetry(current);
        } catch (Exception error) { failure = error; }
        finally {
            closeResources(current);
            Exception result = failure;
            main.post(() -> {
                if (!isCurrent(current)) return;
                transferred += current.bridge == null ? 0 : current.bridge.bytes(); session = null; releaseLocks();
                String reason = result == null ? "USB relay stopped unexpectedly" : message(result);
                log("connection_error", reason);
                long delay = policy.failed(current.token, SystemClock.elapsedRealtime());
                publish(delay < 0 ? "Connection needs attention" : "Reconnecting",
                        reason + (delay < 0 ? " Tap Retry connection after checking USB." : " The relay will retry shortly."), Action.RETRY);
            });
        }
    }
    private void runRelay(Session current) throws Exception {
        UsbIpServer bridge = new UsbIpServer(manager, current.device, new UsbIpServer.Listener() {
            @Override public void event(String text) { if (isCurrent(current)) log("usb_relay", text); }
            @Override public void status(UsbIpServer.Status status, String text) {
                if (status == UsbIpServer.Status.CLIENT_DISCONNECTED) main.post(() -> {
                    if (isCurrent(current)) publish("Waiting for laptop", "Laptop disconnected. The relay is ready for it to reconnect.", Action.NONE);
                });
            }
        });
        current.bridge = bridge;
        if (!isCurrent(current)) return;
        bridge.start(); current.ready = true;
        main.post(() -> { if (isCurrent(current)) publish("Waiting for laptop", "Keep the glasses connected and start the receiver on your laptop.", Action.NONE); });
        while (isCurrent(current)) {
            if (!bridge.isRunning()) throw new IOException("USB listener closed");
            Thread.sleep(500);
        }
    }
    private void runTelemetry(Session current) throws Exception {
        UsbInterface iface = UsbInventory.adbInterface(current.device);
        if (iface == null) throw new IOException("The glasses expose no ADB interface");
        AdbIdentity identity = AdbIdentity.loadOrCreate(getNoBackupFilesDir());
        UsbDeviceConnection raw = manager.openDevice(current.device);
        if (raw == null) throw new IOException("Android could not open the USB device");
        AdbConnection adb = new AdbConnection(raw, iface, identity); current.adb = adb;
        if (!isCurrent(current)) return;
        log("adb", adb.connect()); current.ready = true;
        main.post(() -> { if (isCurrent(current)) publish("Debug telemetry connected", "Internal USB diagnostics mode is active.", Action.NONE); });
        while (isCurrent(current)) {
            String debug = DebugProbe.runPending(this, adb);
            if (debug != null && isCurrent(current)) { probe = debug; log("debug_probe", debug); }
            String value = adb.shell("date; dumpsys battery; cat /proc/uptime", 8192);
            main.post(() -> { if (isCurrent(current)) { telemetry = value.trim(); lastSample = System.currentTimeMillis(); samples++; } });
            if (isCurrent(current)) log("glasses_telemetry", value);
            for (int i = 0; i < 20 && isCurrent(current); i++) Thread.sleep(100);
        }
    }
    private boolean isCurrent(Session current) { return session == current && policy.current(current.token); }
    private void retireCurrent() {
        policy.invalidate(); Session old = session; session = null;
        if (old == null) return;
        if (old.bridge != null) transferred += old.bridge.bytes();
        cleanup.execute(() -> closeResources(old));
    }
    private static void closeResources(Session current) {
        // Serial teardown is shared with the worker's finally; a new worker cannot
        // claim an interface while an old asynchronous cleanup is still releasing it.
        synchronized (current) {
            if (current.adb != null) current.adb.close();
            if (current.bridge != null) current.bridge.close();
        }
    }

    @SuppressWarnings("deprecation")
    private void holdLocks() {
        if (!policy.wanted() || session == null) return;
        try {
            if (wakeLock == null) {
                wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AriaUsbRelay:connection");
                wakeLock.setReferenceCounted(false);
            }
            long now = SystemClock.elapsedRealtime();
            if (!wakeLock.isHeld() || now - lastWakeRenewal > 5 * 60 * 1000L) {
                wakeLock.acquire(10 * 60 * 1000L); lastWakeRenewal = now;
            }
            if (wifiLock == null) {
                WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                if (wifi != null) {
                    int mode = Build.VERSION.SDK_INT >= 34 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                    wifiLock = wifi.createWifiLock(mode, "AriaUsbRelay:connection"); wifiLock.setReferenceCounted(false);
                }
            }
            if (wifiLock != null && !wifiLock.isHeld()) wifiLock.acquire();
        } catch (RuntimeException error) { log("power", "Could not hold connection lock: " + message(error)); }
    }
    private void releaseLocks() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    }

    private void publish(String state, String detail, Action action) {
        Session current = session;
        long bytes = transferred + (current != null && current.bridge != null ? current.bridge.bytes() : 0);
        snapshot = new Snapshot(policy.wanted(), relayMode, current != null && current.bridge != null && current.bridge.isImported(),
                state, detail, current == null ? "No active USB connection" : UsbInventory.label(current.device), bytes, action);
        if (policy.wanted() && !state.equals(notifiedState)) {
            notifiedState = state; getSystemService(NotificationManager.class).notify(1, notification(snapshot));
        }
    }
    private Notification notification(Snapshot value) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, CaptureService.class).setAction(STOP), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_aria)
                .setContentTitle("Aria USB Relay · " + value.state).setContentText(value.detail)
                .setStyle(new Notification.BigTextStyle().bigText(value.detail)).setContentIntent(open)
                .addAction(new Notification.Action.Builder(null, "Stop relay", stop).build())
                .setCategory(Notification.CATEGORY_SERVICE).setOngoing(true).setOnlyAlertOnce(true).build();
    }
    public void stopConnection(String state, String detail) {
        if (Looper.myLooper() != Looper.getMainLooper()) { main.post(() -> stopConnection(state, detail)); return; }
        policy.stop(); persistStopped(); pendingPermission = ""; permissionToken++;
        retireCurrent(); releaseLocks(); main.removeCallbacks(tick);
        log("session", "Relay stopped by user"); publish(state, detail, Action.NONE);
        stopForeground(STOP_FOREGROUND_REMOVE); stopSelf();
    }
    private void persistStopped() { preferences.edit().putBoolean("session_wanted", false).putBoolean("relay_wanted", false).apply(); }
    private int bootCount() { return Settings.Global.getInt(getContentResolver(), Settings.Global.BOOT_COUNT, -1); }
    private void rememberSerial(String serial) {
        if (!serial.isEmpty()) { selectedSerial = serial; preferences.edit().putString("selected_serial", serial).apply(); }
    }
    private static String message(Exception error) { return error.getMessage() == null ? error.toString() : error.getMessage(); }
    private void log(String type, String text) {
        synchronized (events) { events.addLast(type + " · " + text); while (events.size() > 40) events.removeFirst(); }
        if (destroyed) return;
        try {
            journalWorker.execute(() -> {
                try { journal.append(type, text); }
                catch (Exception error) {
                    synchronized (events) { events.addLast("Storage error · " + message(error)); while (events.size() > 40) events.removeFirst(); }
                }
            });
        } catch (RejectedExecutionException ignored) { /* Service is shutting down. */ }
    }
    public Snapshot snapshot() { return snapshot; }
    public boolean isRunning() { return snapshot.wanted; }
    public boolean isRelaying() { return snapshot.wanted && snapshot.relay; }
    public long relayBytes() { return snapshot.bytes; }
    public String state() { return snapshot.state; }
    public String detail() { return snapshot.detail; }
    public String telemetry() { return telemetry; }
    public String probe() { return probe; }
    public long lastSample() { return lastSample; }
    public long samples() { return samples; }
    public boolean hasFreshSample() { return session != null && !relayMode && lastSample != 0 && System.currentTimeMillis() - lastSample < 10000; }
    public long bufferedBytes() { return journal.bytes(); }
    public String logText() { synchronized (events) { return String.join("\n\n", events); } }
    public void export(OutputStream output) throws IOException { journal.export(output); }
    @Override public void onTimeout(int startId, int foregroundServiceType) { stopConnection("Stopped by Android", "Open the app and start the relay again when ready."); }
    @Override public void onDestroy() {
        destroyed = true; policy.stop(); retireCurrent(); releaseLocks(); main.removeCallbacksAndMessages(null);
        worker.shutdownNow(); cleanup.shutdown(); journalWorker.shutdown(); unregisterReceiver(usbEvents);
        // Explicit Stop clears intent. System teardown keeps it for same-boot sticky recovery.
        super.onDestroy();
    }
}
