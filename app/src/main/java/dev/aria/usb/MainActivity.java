package dev.aria.usb;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.hardware.usb.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import java.io.*;
import java.util.*;

/** A relay switch and its status. Sensor decoding and viewing belong on the laptop. */
public final class MainActivity extends Activity {
    private static final int BG = Color.rgb(16,19,15), SURFACE = Color.rgb(28,33,26),
            TEXT = Color.rgb(240,244,233), MUTED = Color.rgb(169,179,161), ACCENT = Color.rgb(187,247,123);
    private final Handler ui = new Handler(Looper.getMainLooper());
    private CaptureService service;
    private boolean bound, detailsVisible;
    private TextView status, detail, device, transferred, diagnostics, journal, notificationHelp;
    private Button primary, action, disclosure;
    private LinearLayout details;
    private String pendingPath;
    private boolean pendingRelay = true;
    private Boolean debugStart;

    private final ServiceConnection binding = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((CaptureService.LocalBinder) binder).getService();
            refresh(); runDebugIntent();
        }
        @Override public void onServiceDisconnected(ComponentName name) { service = null; refresh(); }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        buildUi();
        if (saved != null) {
            pendingPath = saved.getString("pending_path");
            pendingRelay = saved.getBoolean("pending_relay", true);
            setDetailsVisible(saved.getBoolean("details_visible", false));
        }
        readDebugIntent(getIntent());
        bound = bindService(new Intent(this, CaptureService.class), binding, BIND_AUTO_CREATE);
    }
    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent); readDebugIntent(intent); runDebugIntent(); refresh();
    }
    private void readDebugIntent(Intent intent) {
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) == 0) return;
        if (intent.getBooleanExtra("connect_usb", false)) debugStart = false;
        if (intent.getBooleanExtra("relay_usb", false)) debugStart = true;
        intent.removeExtra("connect_usb"); intent.removeExtra("relay_usb");
    }
    private void runDebugIntent() {
        if (service == null || debugStart == null) return;
        boolean relay = debugStart; debugStart = null;
        if (service.isRunning() && service.isRelaying() == relay) return;
        chooseDevice(relay);
    }
    @Override public void onResume() { super.onResume(); ui.post(tick); }
    @Override public void onPause() { ui.removeCallbacks(tick); super.onPause(); }
    @Override protected void onSaveInstanceState(Bundle out) {
        out.putString("pending_path", pendingPath); out.putBoolean("pending_relay", pendingRelay);
        out.putBoolean("details_visible", detailsVisible); super.onSaveInstanceState(out);
    }
    private final Runnable tick = new Runnable() {
        @Override public void run() { refresh(); ui.postDelayed(this, 1000); }
    };

    private void buildUi() {
        LinearLayout root = column(); root.setBackgroundColor(BG);
        root.setPadding(dp(24), dp(12), dp(24), dp(12));
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(dp(24) + bars.left, dp(12) + bars.top, dp(24) + bars.right, dp(12) + bars.bottom);
            } else view.setPadding(dp(24) + insets.getSystemWindowInsetLeft(), dp(12) + insets.getSystemWindowInsetTop(),
                    dp(24) + insets.getSystemWindowInsetRight(), dp(12) + insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(root);
        TextView eyebrow = label("PROJECT ARIA  ·  GEN 1", 11, ACCENT); eyebrow.setLetterSpacing(.15f); root.addView(eyebrow);
        root.addView(label("Aria USB Relay", 29, TEXT), gap(0, 10));
        root.addView(label("Glasses → phone → laptop", 14, MUTED), gap(0, 6));
        ScrollView scroll = new ScrollView(this); scroll.setFillViewport(true); scroll.setClipToPadding(false);
        LinearLayout content = column(); content.setPadding(0, dp(24), 0, dp(12)); scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));

        LinearLayout connection = card(content);
        connection.addView(label("USB CONNECTION", 11, ACCENT));
        status = label("Ready to relay", 26, TEXT); status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        connection.addView(status, gap(0, 12));
        detail = label("Connect the glasses to this phone, then start the relay.", 15, MUTED); connection.addView(detail, gap(0, 10));
        device = label("No glasses selected", 12, MUTED); connection.addView(device, gap(0, 16));
        primary = button("Start relay", true); primary.setEnabled(false);
        primary.setOnClickListener(view -> {
            if (service != null && service.isRunning()) service.stopConnection("Relay stopped", "Start again whenever you need the connection.");
            else chooseDevice(true);
            refresh();
        });
        connection.addView(primary, gap(52, 20));
        action = button("Allow USB access", false);
        action.setOnClickListener(view -> {
            if (service == null) return;
            if (service.snapshot().action == CaptureService.Action.CHOOSE_DEVICE) chooseDevice(true);
            else service.retryOrRequestPermission();
            refresh();
        });
        connection.addView(action, gap(48, 10)); action.setVisibility(View.GONE);

        LinearLayout transfer = card(content);
        transfer.addView(label("USB DATA TRANSFERRED", 11, MUTED));
        transferred = label("0 B", 32, TEXT); transfer.addView(transferred, gap(0, 10));
        transfer.addView(label("The laptop receives and decodes the data. This phone keeps the USB connection available in the background.", 14, MUTED), gap(0, 10));
        content.addView(label("You can leave the app or lock the phone. Use Stop relay here or in the notification when finished.", 14, MUTED), gap(0, 8));
        notificationHelp = label("", 13, MUTED); content.addView(notificationHelp, gap(0, 12));
        notificationHelp.setMinHeight(dp(48)); notificationHelp.setGravity(Gravity.CENTER_VERTICAL);
        notificationHelp.setOnClickListener(view -> openNotificationSettings());

        disclosure = button("Connection details & help", false);
        disclosure.setOnClickListener(view -> setDetailsVisible(!detailsVisible)); content.addView(disclosure, gap(48, 24));
        details = column(); content.addView(details, gap(0, 12)); details.setVisibility(View.GONE);
        LinearLayout help = card(details);
        help.addView(label("KEEPING THE CONNECTION AVAILABLE", 11, ACCENT));
        help.addView(label("USB access may need approval after unplugging or a glasses mode change. The relay waits for that approval and reconnects when possible. If several glasses are attached, choose one explicitly.", 14, MUTED), gap(0, 10));
        help.addView(label("Android can still restrict network access during deep sleep. Battery settings are optional and may increase power use. The app never starts itself at boot.", 14, MUTED), gap(0, 12));
        Button battery = button("Battery settings help", false); battery.setOnClickListener(view -> batteryHelp()); help.addView(battery, gap(48, 14));
        Button select = button("Choose different glasses", false); select.setOnClickListener(view -> chooseDevice(true)); help.addView(select, gap(48, 10));
        LinearLayout diagnosticCard = card(details);
        diagnosticCard.addView(label("DIAGNOSTICS", 11, ACCENT));
        diagnostics = label("", 12, MUTED); diagnostics.setTypeface(Typeface.MONOSPACE); diagnostics.setTextIsSelectable(true);
        diagnosticCard.addView(diagnostics, gap(0, 12));
        diagnosticCard.addView(label("The private journal contains connection events and device identifiers, not recorded camera or audio data. Review exports before sharing them.", 13, MUTED), gap(0, 12));
        Button export = button("Export diagnostics", false);
        export.setOnClickListener(view -> startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT)
                .setType("application/x-ndjson").addCategory(Intent.CATEGORY_OPENABLE)
                .putExtra(Intent.EXTRA_TITLE, "aria-usb-diagnostics.jsonl"), 10));
        diagnosticCard.addView(export, gap(48, 14));
        journal = label("No connection events yet.", 12, MUTED); journal.setTypeface(Typeface.MONOSPACE); journal.setTextIsSelectable(true);
        diagnosticCard.addView(journal, gap(0, 16));
    }

    private void chooseDevice(boolean relay) {
        List<UsbDevice> devices = UsbInventory.candidates(getSystemService(UsbManager.class));
        if (devices.size() <= 1) { requestStart(relay, devices.isEmpty() ? "" : devices.get(0).getDeviceName()); return; }
        String[] names = new String[devices.size()];
        UsbManager manager = getSystemService(UsbManager.class);
        for (int i = 0; i < names.length; i++) {
            UsbDevice device = devices.get(i); String serial = UsbInventory.serial(manager, device);
            names[i] = UsbInventory.label(device) + "\n" + (serial.isEmpty() ? device.getDeviceName() : "Serial " + serial);
        }
        new AlertDialog.Builder(this).setTitle("Choose your Aria glasses")
                .setItems(names, (dialog, which) -> requestStart(relay, devices.get(which).getDeviceName()))
                .setNegativeButton("Cancel", null).show();
    }
    private void requestStart(boolean relay, String path) {
        pendingRelay = relay; pendingPath = path;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !getPreferences(MODE_PRIVATE).getBoolean("notification_prompted", false)) {
            getPreferences(MODE_PRIVATE).edit().putBoolean("notification_prompted", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 4);
        } else startPending();
    }
    private void startPending() {
        if (pendingPath == null) return;
        Intent intent = new Intent(this, CaptureService.class).setAction(pendingRelay ? CaptureService.CONNECT_BRIDGE : CaptureService.CONNECT)
                .putExtra("device_path", pendingPath);
        pendingPath = null;
        try { startForegroundService(intent); }
        catch (RuntimeException error) { Toast.makeText(this, "Could not start relay: " + error.getMessage(), Toast.LENGTH_LONG).show(); }
    }
    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == 4) startPending();
    }
    private void refresh() {
        if (status == null) return;
        primary.setEnabled(service != null);
        if (service != null) {
            CaptureService.Snapshot value = service.snapshot();
            status.setText(value.state); detail.setText(value.detail); device.setText(value.device);
            transferred.setText(formatBytes(value.bytes)); primary.setText(value.wanted ? "Stop relay" : "Start relay");
            action.setVisibility(value.wanted && value.action != CaptureService.Action.NONE ? View.VISIBLE : View.GONE);
            action.setText(value.action == CaptureService.Action.PERMISSION ? "Allow USB access"
                    : value.action == CaptureService.Action.CHOOSE_DEVICE ? "Choose glasses" : "Retry connection");
        }
        boolean notifications = getSystemService(NotificationManager.class).areNotificationsEnabled();
        notificationHelp.setText(notifications ? "" : "Notifications are off. Tap here to enable the background status and Stop action.");
        notificationHelp.setVisibility(notifications ? View.GONE : View.VISIBLE);
        if (detailsVisible) {
            diagnostics.setText(UsbInventory.describe(getSystemService(UsbManager.class)));
            if (service != null) journal.setText(service.logText().isEmpty() ? "No connection events yet." : service.logText());
        }
    }
    private static String formatBytes(long bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return String.format(Locale.getDefault(), "%.1f KiB", bytes / 1024.0);
        if (bytes < 1024L * 1024 * 1024) return String.format(Locale.getDefault(), "%.1f MiB", bytes / (1024.0 * 1024));
        return String.format(Locale.getDefault(), "%.2f GiB", bytes / (1024.0 * 1024 * 1024));
    }
    private void setDetailsVisible(boolean visible) {
        detailsVisible = visible; details.setVisibility(visible ? View.VISIBLE : View.GONE);
        disclosure.setText(visible ? "Hide connection details" : "Connection details & help"); refresh();
    }
    private void batteryHelp() {
        new AlertDialog.Builder(this).setTitle("Optional battery settings")
                .setMessage("If the connection pauses with the screen off, open this app's system settings and review its battery/background-use setting. Allowing more background activity can use more power. Android may still interrupt the connection. No setting is changed automatically.")
                .setPositiveButton("Open app settings", (dialog, which) -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()))))
                .setNegativeButton("Cancel", null).show();
    }
    private void openNotificationSettings() {
        startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName()));
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != 10 || result != RESULT_OK || data == null || data.getData() == null) return;
        Uri destination = data.getData();
        new Thread(() -> {
            try (OutputStream output = getContentResolver().openOutputStream(destination)) {
                if (output == null) throw new IOException("Could not open destination");
                CaptureService active = service;
                if (active != null) active.export(output); else new SessionJournal(getFilesDir()).export(output);
                ui.post(() -> Toast.makeText(getApplicationContext(), "Diagnostics exported", Toast.LENGTH_SHORT).show());
            } catch (Exception error) { ui.post(() -> Toast.makeText(getApplicationContext(), "Export failed: " + error.getMessage(), Toast.LENGTH_LONG).show()); }
        }, "aria-journal-export").start();
    }

    private LinearLayout column() { LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); return layout; }
    private LinearLayout card(LinearLayout parent) {
        LinearLayout card = column(); card.setPadding(dp(20), dp(20), dp(20), dp(20)); card.setBackground(shape(SURFACE, 20));
        parent.addView(card, gap(0, 12)); return card;
    }
    private TextView label(String text, int size, int color) {
        TextView view = new TextView(this); view.setText(text); view.setTextSize(size); view.setTextColor(color); view.setLineSpacing(dp(3), 1); return view;
    }
    private Button button(String text, boolean accent) {
        Button button = new Button(this); button.setText(text); button.setTextSize(14); button.setAllCaps(false);
        button.setTextColor(accent ? BG : TEXT); button.setBackground(shape(accent ? ACCENT : Color.rgb(45,53,40), 24));
        button.setPadding(dp(12), 0, dp(12), 0); return button;
    }
    private GradientDrawable shape(int color, int radius) { GradientDrawable drawable = new GradientDrawable(); drawable.setColor(color); drawable.setCornerRadius(dp(radius)); return drawable; }
    private LinearLayout.LayoutParams gap(int height, int top) { LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, height == 0 ? -2 : dp(height)); params.topMargin = dp(top); return params; }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    @Override public void onDestroy() { ui.removeCallbacksAndMessages(null); if (bound) unbindService(binding); super.onDestroy(); }
}
