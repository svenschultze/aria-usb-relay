package dev.aria.usb;

import android.hardware.usb.UsbConfiguration;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.hardware.usb.UsbRequest;

import java.io.Closeable;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Experimental USB/IP 1.1.1 exporter. Bind is strictly phone loopback: reach it
 * through authenticated adb forwarding. The laptop imports the real composite
 * USB device and runs its normal Aria client. Do not run AdbConnection alongside it.
 * Wire format: https://docs.kernel.org/usb/usbip_protocol.html
 *
 * Android's public API does not expose native URB errno. Failed synchronous
 * transfers map to EIO; unsupported isochronous transfers are rejected explicitly.
 */
public final class UsbIpServer implements Closeable {
    public enum Status { LISTENING, IMPORTED, CLIENT_DISCONNECTED, ERROR, CLOSED }
    public interface Listener {
        void event(String message);
        default void status(Status status, String message) {}
    }
    public static final int PORT = 3240;
    private static final int VERSION = 0x0111, MAX_TRANSFER = 1024 * 1024;
    private static final int MAX_PENDING = 64, MAX_BUFFERED = 8 * MAX_TRANSFER;
    private static final int EIO = -5, EPIPE = -32, ENOMEM = -12, EINVAL = -22;
    private static final int ECONNRESET = -104, EOPNOTSUPP = -95;
    private final UsbManager manager;
    private final UsbDevice device;
    private final Listener listener;
    private final AtomicLong byteCount = new AtomicLong();
    private final Object lifecycle = new Object();
    private final Map<Socket, Boolean> clients = new ConcurrentHashMap<>();
    private volatile boolean closed;
    private ServerSocket server;
    private volatile ImportedSession imported;
    private byte[] deviceRecord, interfaceRecords;
    private String busId;
    private int deviceId, configurationValue;

    public UsbIpServer(UsbManager manager, UsbDevice device, Listener listener) {
        this.manager = manager; this.device = device; this.listener = listener;
    }

    public void start() throws IOException {
        synchronized (lifecycle) {
            if (server != null) throw new IOException("USB/IP server already started");
            if (closed) throw new IOException("USB/IP server is closed");
            if (!manager.hasPermission(device)) throw new IOException("USB access permission is required");
            readDeviceDescription();
            ServerSocket socket = new ServerSocket();
            socket.setReuseAddress(true);
            try { socket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT), 4); }
            catch (IOException error) { socket.close(); throw error; }
            server = socket;
            Thread accept = new Thread(this::acceptLoop, "aria-usbip-listener");
            accept.setDaemon(true);
            accept.start();
        }
        event("USB/IP listening on 127.0.0.1:" + PORT + "; export " + busId);
        status(Status.LISTENING, "Waiting for the laptop receiver");
    }

    public long bytes() { return byteCount.get(); }
    public String busId() { return busId; }
    public boolean isRunning() { return !closed && server != null && !server.isClosed(); }
    public boolean isImported() { ImportedSession session = imported; return session != null && !session.stopped; }

    private void readDeviceDescription() throws IOException {
        UsbDeviceConnection raw = manager.openDevice(device);
        if (raw == null) throw new IOException("Android could not open the USB device");
        byte[] descriptors;
        try {
            descriptors = raw.getRawDescriptors();
            byte[] current = new byte[1];
            int count = raw.controlTransfer(0x80, 8, 0, 0, current, 1, 1000);
            configurationValue = count == 1 ? current[0] & 255
                    : device.getConfiguration(0).getId();
        } finally { raw.close(); }
        if (descriptors == null || descriptors.length < 18 || descriptors[1] != 1)
            throw new IOException("The USB device descriptor is unavailable");
        String[] path = device.getDeviceName().split("/");
        int bus = 1, number = device.getDeviceId() & 65535;
        try {
            bus = Integer.parseInt(path[path.length - 2]);
            number = Integer.parseInt(path[path.length - 1]);
        } catch (RuntimeException ignored) {}
        if (number == 0) number = 1;
        busId = bus + "-" + number;
        deviceId = (bus << 16) | number;
        UsbConfiguration configuration = findConfiguration(configurationValue);
        if (configuration == null) configuration = device.getConfiguration(0);
        Map<Integer, UsbInterface> interfaces = defaultInterfaces(configuration);
        int speed = 2; // Full speed unless endpoint/device descriptor proves otherwise.
        for (int i = 0; i < configuration.getInterfaceCount(); i++) {
            UsbInterface iface = configuration.getInterface(i);
            for (int j = 0; j < iface.getEndpointCount(); j++) {
                int packetSize = iface.getEndpoint(j).getMaxPacketSize();
                if (packetSize >= 1024) speed = 5;
                else if (packetSize >= 512 && speed < 3) speed = 3;
            }
        }
        if (le16(descriptors, 2) >= 0x0300) speed = 5;
        ByteBuffer description = ByteBuffer.allocate(312).order(ByteOrder.BIG_ENDIAN);
        description.put(fixedString(device.getDeviceName(), 256)).put(fixedString(busId, 32));
        description.putInt(bus).putInt(number).putInt(speed);
        description.putShort((short) le16(descriptors, 8)).putShort((short) le16(descriptors, 10));
        description.putShort((short) le16(descriptors, 12));
        description.put(descriptors[4]).put(descriptors[5]).put(descriptors[6]);
        description.put((byte) configurationValue).put(descriptors[17]).put((byte) interfaces.size());
        deviceRecord = description.array();
        ByteBuffer interfaceData = ByteBuffer.allocate(interfaces.size() * 4);
        for (UsbInterface iface : interfaces.values())
            interfaceData.put((byte) iface.getInterfaceClass()).put((byte) iface.getInterfaceSubclass())
                    .put((byte) iface.getInterfaceProtocol()).put((byte) 0);
        interfaceRecords = interfaceData.array();
    }

    private void acceptLoop() {
        while (!closed) {
            try {
                Socket socket = server.accept();
                if (clients.size() >= 4) { socket.close(); continue; }
                socket.setTcpNoDelay(true);
                socket.setKeepAlive(true);
                socket.setSoTimeout(5000);
                clients.put(socket, Boolean.TRUE);
                Thread handler = new Thread(() -> handleClient(socket), "aria-usbip-client");
                handler.setDaemon(true);
                handler.start();
            } catch (IOException error) {
                if (!closed) {
                    event("USB/IP listener stopped: " + error.getMessage());
                    status(Status.ERROR, error.getMessage());
                    close();
                }
                break;
            }
        }
    }

    private void handleClient(Socket socket) {
        ImportedSession session = null;
        try {
            DataInputStream input = new DataInputStream(socket.getInputStream());
            DataOutputStream output = new DataOutputStream(socket.getOutputStream());
            int version = input.readUnsignedShort(), operation = input.readUnsignedShort();
            input.readInt();
            if (version != VERSION) throw new IOException("Unsupported USB/IP version " + version);
            if (operation == 0x8005) {
                operationReply(output, 5, 0);
                output.writeInt(1); output.write(deviceRecord); output.write(interfaceRecords); output.flush();
                return;
            }
            if (operation != 0x8003) throw new IOException("Unsupported USB/IP operation");
            byte[] requested = new byte[32]; input.readFully(requested);
            if (!busId.equals(zString(requested)) || !manager.getDeviceList().containsKey(device.getDeviceName())) {
                operationReply(output, 3, 4); output.flush(); return;
            }
            synchronized (lifecycle) {
                if (closed || imported != null) {
                    operationReply(output, 3, 2); output.flush(); return;
                }
                session = new ImportedSession(socket, input, output);
                imported = session;
            }
            operationReply(output, 3, 0);
            output.write(deviceRecord); output.flush();
            socket.setSoTimeout(0);
            event("USB/IP imported " + busId + "; the laptop owns the glasses USB interfaces");
            status(Status.IMPORTED, "Laptop receiver connected");
            session.run();
        } catch (EOFException ignored) {
            if (!closed) event("USB/IP client disconnected");
        } catch (Exception error) {
            if (!closed) event("USB/IP: " + error.getMessage());
        } finally {
            if (session != null) session.close();
            synchronized (lifecycle) { if (imported == session) imported = null; }
            if (session != null && !closed) status(Status.CLIENT_DISCONNECTED, "Laptop disconnected; waiting to reconnect");
            clients.remove(socket);
            try { socket.close(); } catch (IOException ignored) {}
        }
    }

    private final class ImportedSession implements Closeable {
        private final Socket socket;
        private final DataInputStream input;
        private final DataOutputStream output;
        private final UsbDeviceConnection usb;
        private final Object responses = new Object(), interfacesLock = new Object();
        private final Map<Integer, Urb> pending = new HashMap<>();
        private final Map<Integer, UsbInterface> claimed = new LinkedHashMap<>();
        private final Map<Integer, UsbEndpoint> endpoints = new ConcurrentHashMap<>();
        private final Map<Integer, ExecutorService> endpointWorkers = new ConcurrentHashMap<>();
        private final Map<UsbRequest, Urb> async = new ConcurrentHashMap<>();
        private final ExecutorService controlWorker = Executors.newSingleThreadExecutor();
        private volatile boolean stopped;
        private int bufferedBytes;
        private UsbConfiguration configuration;

        ImportedSession(Socket socket, DataInputStream input, DataOutputStream output) throws IOException {
            this.socket = socket; this.input = input; this.output = output;
            usb = manager.openDevice(device);
            if (usb == null) throw new IOException("Could not open imported USB device");
            try {
                configuration = findConfiguration(configurationValue);
                if (configuration == null) configuration = device.getConfiguration(0);
                claim(configuration);
            } catch (IOException error) { usb.close(); throw error; }
        }

        void run() throws IOException {
            Thread completions = new Thread(this::completeAsync, "aria-usbip-usb-completion");
            completions.setDaemon(true); completions.start();
            while (!stopped && !closed) {
                byte[] header = new byte[48]; input.readFully(header);
                ByteBuffer packet = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN);
                int command = packet.getInt(), sequence = packet.getInt(), requestedDevice = packet.getInt();
                int direction = packet.getInt(), endpoint = packet.getInt();
                if (requestedDevice != deviceId) throw new IOException("USB/IP device id mismatch");
                if (command == 2) { unlink(sequence, packet.getInt()); continue; }
                if (command != 1 || (direction != 0 && direction != 1) || endpoint < 0 || endpoint > 15)
                    throw new IOException("Malformed USB/IP URB header");
                int flags = packet.getInt(), length = packet.getInt(), startFrame = packet.getInt();
                int packets = packet.getInt(), interval = packet.getInt();
                byte[] setup = new byte[8]; packet.get(setup);
                if (length < 0 || length > MAX_TRANSFER) throw new IOException("USB/IP URB exceeds the 1 MiB limit");
                if (packets > 0) throw new IOException("Isochronous USB/IP transfers are not supported by this experiment");
                byte[] data = new byte[length];
                if (direction == 0) input.readFully(data);
                Urb urb = new Urb(sequence, direction, endpoint, flags, length, startFrame, packets, setup, data);
                synchronized (responses) {
                    if (pending.containsKey(sequence)) throw new IOException("Duplicate USB/IP sequence");
                    if (pending.size() >= MAX_PENDING || bufferedBytes + length > MAX_BUFFERED) {
                        submitReply(urb, ENOMEM, 0); continue;
                    }
                    pending.put(sequence, urb); bufferedBytes += length;
                }
                submit(urb);
            }
        }

        private void submit(Urb urb) throws IOException {
            if (urb.endpoint == 0) {
                controlWorker.execute(() -> control(urb));
                return;
            }
            UsbEndpoint endpoint = endpoints.get(urb.endpoint | (urb.direction == 1 ? 0x80 : 0));
            if (endpoint == null) { finish(urb, EPIPE, 0); return; }
            if (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK
                    && endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_INT) {
                event("Unsupported endpoint transfer type " + endpoint.getType());
                finish(urb, EOPNOTSUPP, 0); return;
            }
            if (urb.direction == 0 && endpoint.getType() == UsbConstants.USB_ENDPOINT_XFER_BULK) {
                ExecutorService worker = endpointWorkers.computeIfAbsent(endpoint.getAddress(),
                        ignored -> Executors.newSingleThreadExecutor());
                worker.execute(() -> bulkOut(urb, endpoint));
            } else {
                // An async IN remains pending until data arrives or UNLINK cancels it.
                // Repeated timed bulk reads would lose partially received data on timeout.
                UsbRequest request = new UsbRequest();
                if (!request.initialize(usb, endpoint)) { request.close(); finish(urb, EIO, 0); return; }
                urb.buffer = ByteBuffer.wrap(urb.data);
                urb.request = request;
                request.setClientData(urb);
                async.put(request, urb);
                if (!request.queue(urb.buffer)) {
                    async.remove(request); request.close(); finish(urb, EIO, 0);
                }
            }
        }

        private void control(Urb urb) {
            if (stopped) return;
            if (urb.cancelled) { discardCancelled(urb); return; }
            int type = urb.setup[0] & 255, request = urb.setup[1] & 255;
            int value = le16(urb.setup, 2), index = le16(urb.setup, 4), length = le16(urb.setup, 6);
            int result;
            try {
                if (length > urb.length || ((type >>> 7) & 1) != urb.direction) {
                    finish(urb, EINVAL, 0); return;
                }
                if (type == 0 && request == 5) {
                    // Android already assigned the physical device address.
                    result = 0;
                } else if (type == 0 && request == 9) {
                    synchronized (interfacesLock) {
                        // Match the historical Linux USB/IP exporter workaround: the
                        // exporting host has already configured this device. Reissuing
                        // the current value can unbind/rebind its physical interfaces
                        // and break the exported transport. Alternate settings remain
                        // under the importer's explicit SET_INTERFACE requests.
                        // kernel/msm f6509b5f002a, drivers/staging/usbip/stub_rx.c,
                        // tweak_set_configuration_cmd. Modern Linux changed this;
                        // Android public USB APIs lack that exporter's kernel control.
                        if (configuration != null && configuration.getId() == value) {
                            finish(urb, 0, 0);
                            return;
                        }
                        synchronized (responses) {
                            // A configuration change invalidates all endpoint URBs.
                            // The importing USB core must unlink them first.
                            if (pending.size() > 1) { finish(urb, -16, 0); return; }
                        }
                        if (value == 0) {
                            releaseInterfaces();
                            result = usb.controlTransfer(0, 9, 0, 0, new byte[0], 0, 5000);
                            if (result >= 0) configuration = null;
                            finish(urb, result < 0 ? EIO : 0, 0);
                            return;
                        }
                        UsbConfiguration desired = findConfiguration(value);
                        if (desired == null) { finish(urb, EINVAL, 0); return; }
                        // A genuinely different configuration still needs a physical
                        // change; the importer must have unlinked its other URBs first.
                        releaseInterfaces();
                        if (!usb.setConfiguration(desired)) { finish(urb, EIO, 0); return; }
                        configuration = desired;
                        claim(desired);
                    }
                    result = 0;
                } else if (type == 1 && request == 11) {
                    synchronized (interfacesLock) {
                        if (configuration == null) { finish(urb, EPIPE, 0); return; }
                        UsbInterface target = findInterface(configuration, index, value);
                        if (target == null || !usb.setInterface(target)) { finish(urb, EPIPE, 0); return; }
                        claimed.put(index, target);
                        rebuildEndpoints();
                    }
                    result = 0;
                } else {
                    result = usb.controlTransfer(type, request, value, index, urb.data, length, 5000);
                }
                finish(urb, result < 0 ? EIO : 0, Math.max(0, result));
            } catch (Exception error) { transferFailed(urb, error); }
        }

        private void bulkOut(Urb urb, UsbEndpoint endpoint) {
            if (stopped) return;
            if (urb.cancelled) { discardCancelled(urb); return; }
            try {
                int count = usb.bulkTransfer(endpoint, urb.data, urb.length, 5000);
                if (count >= 0 && count == urb.length && urb.length > 0 && (urb.flags & 0x40) != 0
                        && urb.length % endpoint.getMaxPacketSize() == 0) {
                    if (usb.bulkTransfer(endpoint, new byte[0], 0, 5000) < 0) {
                        finish(urb, EIO, count); return;
                    }
                }
                finish(urb, count < 0 ? EIO : 0, Math.max(0, count));
            } catch (Exception error) { transferFailed(urb, error); }
        }

        private void completeAsync() {
            while (!stopped && !closed) {
                try {
                    UsbRequest request = usb.requestWait(1000);
                    if (request == null) {
                        if (!stopped) { event("USB device disconnected; import it again after re-enumeration"); close(); }
                        return;
                    }
                    Urb urb = async.remove(request);
                    if (urb != null) {
                        int actual = urb.buffer.position();
                        int status = (urb.flags & 1) != 0 && actual < urb.length ? -121 : 0;
                        finish(urb, status, actual);
                    }
                    request.close();
                } catch (TimeoutException idle) {
                    // No transfer completed; pending reads remain queued in the kernel.
                } catch (Exception error) {
                    if (!stopped) event("USB/IP receive completion failed: " + error.getMessage());
                    close(); return;
                }
            }
        }

        private void unlink(int sequence, int targetSequence) throws IOException {
            synchronized (responses) {
                Urb target = pending.get(targetSequence);
                boolean cancelled = target != null && !target.cancelled;
                if (cancelled) {
                    // Keep its memory charged until the native request or queued
                    // worker actually finishes, even after acknowledging UNLINK.
                    target.cancelled = true;
                    if (target.request != null) target.request.cancel();
                }
                byte[] header = new byte[48];
                ByteBuffer reply = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN);
                reply.putInt(4).putInt(sequence);
                reply.position(20);
                reply.putInt(cancelled ? ECONNRESET : 0);
                output.write(header); output.flush();
            }
        }

        private void finish(Urb urb, int status, int actual) throws IOException {
            synchronized (responses) {
                if (stopped || pending.remove(urb.sequence) != urb) return;
                bufferedBytes -= urb.length;
                if (urb.cancelled) return;
                submitReply(urb, status, actual);
            }
        }

        private void submitReply(Urb urb, int status, int actual) throws IOException {
            if (actual < 0 || actual > urb.length) throw new IOException("Invalid USB transfer completion length");
            ByteBuffer reply = ByteBuffer.allocate(48).order(ByteOrder.BIG_ENDIAN);
            reply.putInt(3).putInt(urb.sequence).putInt(0).putInt(0).putInt(0);
            reply.putInt(status).putInt(actual).putInt(urb.startFrame).putInt(urb.packets).putInt(0).putLong(0);
            output.write(reply.array());
            if (urb.direction == 1 && actual > 0) output.write(urb.data, 0, actual);
            output.flush();
            byteCount.addAndGet(actual);
        }

        private void transferFailed(Urb urb, Exception error) {
            if (stopped) return;
            event("USB/IP transfer failed: " + error.getMessage());
            try { finish(urb, EIO, 0); }
            catch (IOException failure) { close(); }
        }

        private void discardCancelled(Urb urb) {
            try { finish(urb, ECONNRESET, 0); }
            catch (IOException impossible) { close(); }
        }

        private void claim(UsbConfiguration config) throws IOException {
            synchronized (interfacesLock) {
                for (UsbInterface iface : defaultInterfaces(config).values()) {
                    if (!usb.claimInterface(iface, true)) throw new IOException("Cannot claim USB interface " + iface.getId());
                    claimed.put(iface.getId(), iface);
                }
                rebuildEndpoints();
            }
        }

        private void rebuildEndpoints() {
            endpoints.clear();
            for (UsbInterface iface : claimed.values())
                for (int i = 0; i < iface.getEndpointCount(); i++) {
                    UsbEndpoint endpoint = iface.getEndpoint(i);
                    endpoints.put(endpoint.getAddress(), endpoint);
                }
        }

        private void releaseInterfaces() {
            for (UsbInterface iface : claimed.values()) usb.releaseInterface(iface);
            claimed.clear(); endpoints.clear();
        }

        @Override public void close() {
            if (stopped) return;
            stopped = true;
            try { socket.close(); } catch (IOException ignored) {}
            synchronized (responses) {
                for (Urb urb : pending.values()) urb.cancelled = true;
                pending.clear(); bufferedBytes = 0;
            }
            for (UsbRequest request : async.keySet()) request.cancel();
            controlWorker.shutdownNow();
            for (ExecutorService worker : endpointWorkers.values()) worker.shutdownNow();
            synchronized (interfacesLock) { releaseInterfaces(); }
            usb.close();
            for (UsbRequest request : async.keySet()) request.close();
            async.clear();
        }
    }

    private static final class Urb {
        final int sequence, direction, endpoint, flags, length, startFrame, packets;
        final byte[] setup, data;
        volatile boolean cancelled;
        volatile UsbRequest request;
        ByteBuffer buffer;
        Urb(int sequence, int direction, int endpoint, int flags, int length, int startFrame,
                int packets, byte[] setup, byte[] data) {
            this.sequence = sequence; this.direction = direction; this.endpoint = endpoint;
            this.flags = flags; this.length = length; this.startFrame = startFrame;
            this.packets = packets; this.setup = setup; this.data = data;
        }
    }

    private UsbConfiguration findConfiguration(int value) {
        for (int i = 0; i < device.getConfigurationCount(); i++)
            if (device.getConfiguration(i).getId() == value) return device.getConfiguration(i);
        return null;
    }
    private static UsbInterface findInterface(UsbConfiguration configuration, int id, int alternate) {
        for (int i = 0; i < configuration.getInterfaceCount(); i++) {
            UsbInterface iface = configuration.getInterface(i);
            if (iface.getId() == id && iface.getAlternateSetting() == alternate) return iface;
        }
        return null;
    }
    private static Map<Integer, UsbInterface> defaultInterfaces(UsbConfiguration configuration) {
        Map<Integer, UsbInterface> result = new LinkedHashMap<>();
        for (int i = 0; i < configuration.getInterfaceCount(); i++) {
            UsbInterface iface = configuration.getInterface(i);
            if (!result.containsKey(iface.getId()) || iface.getAlternateSetting() == 0) result.put(iface.getId(), iface);
        }
        return result;
    }
    private static int le16(byte[] bytes, int offset) { return (bytes[offset] & 255) | (bytes[offset + 1] & 255) << 8; }
    private static byte[] fixedString(String value, int length) {
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8), result = new byte[length];
        System.arraycopy(bytes, 0, result, 0, Math.min(bytes.length, length - 1));
        return result;
    }
    private static String zString(byte[] bytes) {
        int length = 0; while (length < bytes.length && bytes[length] != 0) length++;
        return new String(bytes, 0, length, StandardCharsets.UTF_8);
    }
    private static void operationReply(DataOutputStream output, int command, int status) throws IOException {
        output.writeShort(VERSION); output.writeShort(command); output.writeInt(status);
    }
    private void event(String message) {
        if (listener != null) { try { listener.event(message); } catch (RuntimeException ignored) {} }
    }
    private void status(Status status, String message) {
        if (listener != null) { try { listener.status(status, message); } catch (RuntimeException ignored) {} }
    }
    @Override public void close() {
        closed = true;
        synchronized (lifecycle) {
            if (imported != null) imported.close();
            if (server != null) try { server.close(); } catch (IOException ignored) {}
        }
        for (Socket socket : clients.keySet()) try { socket.close(); } catch (IOException ignored) {}
        clients.clear();
        status(Status.CLOSED, "USB relay stopped");
    }
}
