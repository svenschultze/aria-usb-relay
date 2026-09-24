package dev.aria.usb;

import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;

import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicLong;

import static dev.aria.usb.AdbProtocol.*;

/**
 * USB ADB host with ordinary RSA authorization. Call connect/shell on a worker thread.
 * One service is open at a time; its reader and writer can run concurrently. Incoming
 * data is acknowledged only when consumed, giving bounded buffering/backpressure.
 */
public final class AdbConnection implements Closeable {
    private final UsbDeviceConnection usb;
    private final UsbInterface usbInterface;
    private final UsbEndpoint input, output;
    private final AdbIdentity identity;
    private final Object sendLock = new Object(), serviceLock = new Object();
    private final AtomicLong receivedBytes = new AtomicLong(), sentBytes = new AtomicLong();
    private final AtomicLong queuedBurstPackets = new AtomicLong(), discardedHandshakePackets = new AtomicLong();
    private volatile boolean closed, connected;
    private volatile IOException terminalError;
    private volatile Stream activeStream;
    private volatile String banner = "";
    private int nextId = 1, maxWrite = MAX_PAYLOAD;
    private Thread reader;

    public AdbConnection(UsbDeviceConnection usb, UsbInterface usbInterface, AdbIdentity identity)
            throws IOException {
        this.usb = usb;
        this.usbInterface = usbInterface;
        this.identity = identity;
        UsbEndpoint in = null, out = null;
        if (!isAdbInterface(usbInterface)) {
            usb.close();
            throw new IOException("This USB interface is not ADB");
        }
        for (int i = 0; i < usbInterface.getEndpointCount(); i++) {
            UsbEndpoint endpoint = usbInterface.getEndpoint(i);
            if (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK) continue;
            if (endpoint.getDirection() == UsbConstants.USB_DIR_IN) in = endpoint;
            else out = endpoint;
        }
        if (in == null || out == null || !usb.claimInterface(usbInterface, true)) {
            usb.close();
            throw new IOException("Cannot claim the glasses' ADB USB bulk interface");
        }
        input = in; output = out;
    }

    public static boolean isAdbInterface(UsbInterface iface) {
        return iface.getInterfaceClass() == 255 && iface.getInterfaceSubclass() == 66
                && iface.getInterfaceProtocol() == 1;
    }

    public static UsbInterface findAdbInterface(UsbDevice device) {
        for (int i = 0; i < device.getInterfaceCount(); i++)
            if (isAdbInterface(device.getInterface(i))) return device.getInterface(i);
        return null;
    }

    public String connect() throws IOException { return connect(15000); }

    public synchronized String connect(int timeoutMs) throws IOException {
        ensureOpen();
        if (connected) return banner;
        if (timeoutMs < 1) throw new IllegalArgumentException("Positive timeout required");
        long deadline = deadline(timeoutMs);
        boolean signatureSent = false, publicKeySent = false, connectRetried = false;
        long retryAt = Math.min(deadline, deadline(1500));
        try {
            send(CNXN, VERSION, MAX_PAYLOAD, cString("host::features="));
            while (true) {
                Packet packet;
                try {
                    long readDeadline = !signatureSent && !connectRetried && discardedHandshakePackets.get() > 0
                            ? Math.min(deadline, retryAt) : deadline;
                    packet = receive(readDeadline, true);
                } catch (SocketTimeoutException timeout) {
                    if (!signatureSent && !connectRetried && discardedHandshakePackets.get() > 0
                            && now() < deadline) {
                        send(CNXN, VERSION, MAX_PAYLOAD, cString("host::features="));
                        connectRetried = true;
                        continue;
                    }
                    throw timeout;
                }
                if (packet.command == CNXN) {
                    if (packet.arg0 < VERSION || packet.arg0 > VERSION + 1 || packet.arg1 < 1)
                        throw new IOException("Unsupported ADB protocol negotiation");
                    maxWrite = Math.min(MAX_PAYLOAD, packet.arg1);
                    banner = new String(packet.data, StandardCharsets.UTF_8).replace("\0", "");
                    connected = true;
                    reader = new Thread(this::readLoop, "aria-usb-adb-reader");
                    reader.setDaemon(true);
                    reader.start();
                    return banner;
                }
                // Closing the Android USB handle does not reset adbd on the glasses.
                // The endpoint can therefore still contain complete frames from a prior
                // session. ADB ignores service traffic before the new CONNECT completes.
                if (packet.command == WRTE || packet.command == OKAY || packet.command == CLSE
                        || packet.command == OPEN) {
                    discardedHandshakePackets.incrementAndGet();
                    continue;
                }
                if (packet.command != AUTH || packet.arg0 != 1 || packet.arg1 != 0)
                    throw new IOException("Unexpected ADB handshake response");
                if (!signatureSent) {
                    send(AUTH, 2, 0, identity.signToken(packet.data));
                    signatureSent = true;
                } else if (!publicKeySent) {
                    send(AUTH, 3, 0, identity.publicKeyPayload());
                    publicKeySent = true;
                } else {
                    throw new AuthorizationRequiredException();
                }
            }
        } catch (GeneralSecurityException e) {
            IOException failure = new IOException("Cannot sign the glasses' ADB challenge", e);
            fail(failure);
            throw failure;
        } catch (IOException e) {
            IOException failure = publicKeySent && e instanceof SocketTimeoutException
                    ? new AuthorizationRequiredException() : e;
            fail(failure);
            throw failure;
        }
    }

    public String getBanner() { return banner; }
    public String banner() { return banner; }
    public boolean isConnected() { return connected && !closed; }
    public long getReceivedBytes() { return receivedBytes.get(); }
    public long getSentBytes() { return sentBytes.get(); }
    public long getQueuedBurstPackets() { return queuedBurstPackets.get(); }
    public long getDiscardedHandshakePackets() { return discardedHandshakePackets.get(); }

    /** Legacy shell has merged stdout/stderr and no reliable exit-status field. */
    public String shell(String command, int maxBytes) throws IOException {
        return shell(command, maxBytes, 15000);
    }

    public String shell(String command, int maxBytes, int timeoutMs) throws IOException {
        if (maxBytes < 1 || maxBytes > 16 * 1024 * 1024)
            throw new IllegalArgumentException("Output limit must be 1 byte to 16 MiB");
        long deadline = deadline(timeoutMs);
        try (Stream stream = openService("shell:" + command, remaining(deadline))) {
            ByteArrayOutputStream result = new ByteArrayOutputStream();
            while (true) {
                byte[] chunk = stream.read(remaining(deadline));
                if (chunk == null) return new String(result.toByteArray(), StandardCharsets.UTF_8);
                if (result.size() + chunk.length > maxBytes)
                    throw new IOException("Glasses shell output exceeded " + maxBytes + " bytes");
                result.write(chunk, 0, chunk.length);
            }
        }
    }

    /** Local TCP service on the glasses; no network discovery is performed. */
    public Stream openTcp(int port) throws IOException {
        if (port < 1 || port > 65535) throw new IllegalArgumentException("Invalid TCP port");
        return openService("tcp:" + port, 10000);
    }

    public Stream openService(String service, int timeoutMs) throws IOException {
        byte[] destination = cString(service);
        if (destination.length > maxWrite) throw new IllegalArgumentException("Service name too long");
        synchronized (serviceLock) {
            ensureOpen();
            if (!connected) throw new IOException("ADB is not connected");
            if (activeStream != null) throw new IOException("Another glasses service is already open");
            Stream stream = new Stream(nextId++);
            if (nextId <= 0) nextId = 1;
            activeStream = stream;
            try {
                send(OPEN, stream.localId, 0, destination);
                stream.awaitOpen(timeoutMs);
                return stream;
            } catch (IOException e) {
                stream.close();
                throw e;
            }
        }
    }

    private void readLoop() {
        try {
            while (!closed) {
                Packet packet = receive(Long.MAX_VALUE, true);
                Stream stream = activeStream;
                if (packet.command == OPEN) {
                    // Device-initiated services (ADB reverse) are deliberately unsupported.
                    send(CLSE, 0, packet.arg0, EMPTY);
                } else if (packet.command == OKAY || packet.command == WRTE || packet.command == CLSE) {
                    if (stream != null && packet.arg1 == stream.localId) stream.receive(packet);
                    else if (packet.command == WRTE || packet.command == OKAY)
                        send(CLSE, packet.arg1, packet.arg0, EMPTY);
                } else {
                    throw new IOException("ADB connection unexpectedly restarted");
                }
            }
        } catch (IOException e) {
            if (!closed) fail(e);
        }
    }

    private Packet receive(long deadline, boolean idleAllowed) throws IOException {
        byte[] bytes = new byte[HEADER_LENGTH];
        readFully(bytes, deadline, idleAllowed);
        Header header = parseHeader(bytes, MAX_PAYLOAD);
        byte[] payload = new byte[header.length];
        readFully(payload, Math.min(deadline, deadline(5000)), false);
        return header.packet(payload);
    }

    private void readFully(byte[] target, long deadline, boolean idleAllowed) throws IOException {
        int position = 0, immediateFailures = 0;
        while (position < target.length) {
            ensureOpen();
            int timeout = Math.min(1000, remaining(deadline));
            long started = now();
            int count = usb.bulkTransfer(input, target, position, target.length - position, timeout);
            if (count > 0) {
                position += count;
                receivedBytes.addAndGet(count);
                immediateFailures = 0;
            } else if (count < 0) {
                if (position != 0 || !idleAllowed)
                    throw new IOException("USB read failed inside an ADB packet; reconnect required");
                if (now() - started < Math.min(50, timeout / 2)) {
                    if (++immediateFailures >= 3) throw new IOException("USB glasses connection was lost");
                    try { Thread.sleep(20); }
                    catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        throw new IOException("USB read interrupted", e);
                    }
                } else immediateFailures = 0;
            }
            // A zero-length USB packet carries no ADB data. Keep the same read position.
        }
    }

    private void send(int command, int arg0, int arg1, byte[] payload) throws IOException {
        synchronized (sendLock) {
            ensureOpen();
            try {
                // USB ADB requires separate header and body transfers, not one joined buffer.
                writeTransfer(header(command, arg0, arg1, payload));
                if (payload.length != 0) writeTransfer(payload);
            } catch (IOException e) {
                fail(e);
                throw e;
            }
        }
    }

    private void writeTransfer(byte[] data) throws IOException {
        long deadline = deadline(5000);
        int position = 0;
        while (position < data.length) {
            ensureOpen();
            int count = usb.bulkTransfer(output, data, position, data.length - position, remaining(deadline));
            if (count <= 0) throw new IOException("USB write failed; reconnect required");
            position += count;
            sentBytes.addAndGet(count);
        }
        // adbd reads fixed-size buffers and needs a short packet to delimit full USB packets.
        if (data.length % output.getMaxPacketSize() == 0) {
            int result = usb.bulkTransfer(output, EMPTY, 0, 0, remaining(deadline));
            if (result < 0) throw new IOException("USB zero-length terminator failed");
        }
    }

    private void ensureOpen() throws IOException {
        if (closed) throw terminalError == null ? new IOException("ADB connection is closed") : terminalError;
    }

    private void fail(IOException failure) {
        terminalError = failure;
        close();
    }

    /** Safe from the UI thread; closes USB to unblock ongoing operations. */
    @Override public void close() {
        if (closed) return;
        closed = true;
        connected = false;
        Stream stream = activeStream;
        activeStream = null;
        if (stream != null) stream.fail(terminalError == null ? new IOException("ADB connection closed") : terminalError);
        usb.releaseInterface(usbInterface);
        usb.close();
    }

    private static long now() { return System.nanoTime() / 1_000_000L; }
    private static long deadline(int timeoutMs) {
        if (timeoutMs < 1) throw new IllegalArgumentException("Positive timeout required");
        return now() + timeoutMs;
    }
    private static int remaining(long deadline) throws SocketTimeoutException {
        long millis = deadline - now();
        if (millis <= 0) throw new SocketTimeoutException("Timed out waiting for the glasses");
        return (int) Math.min(Integer.MAX_VALUE, millis);
    }

    public static final class AuthorizationRequiredException extends IOException {
        public AuthorizationRequiredException() {
            super("The glasses require authorization of this phone's ADB key. Authorize this host "
                    + "using the glasses' supported provisioning flow, then reconnect. No authorization bypass is attempted.");
        }
    }

    public final class Stream implements Closeable {
        private static final int MAX_QUEUED_PACKETS = 64;
        private static final int MAX_QUEUED_BYTES = MAX_QUEUED_PACKETS * MAX_PAYLOAD;
        private final int localId;
        private final Object monitor = new Object(), writeSerial = new Object(), readSerial = new Object();
        private final ArrayDeque<byte[]> incoming = new ArrayDeque<>();
        private int remoteId, queuedBytes;
        private boolean ended, canWrite;
        private IOException failure;

        private Stream(int localId) { this.localId = localId; }

        private void awaitOpen(int timeoutMs) throws IOException {
            long deadline = deadline(timeoutMs);
            synchronized (monitor) {
                while (remoteId == 0 && !ended && failure == null) await(deadline);
                checkFailure();
                if (remoteId == 0) throw new IOException("The glasses rejected the requested ADB service");
            }
        }

        private void receive(Packet packet) throws IOException {
            synchronized (monitor) {
                if (ended) return;
                if (packet.command == CLSE) {
                    if (remoteId != 0 && packet.arg0 != remoteId && packet.arg0 != 0)
                        throw new IOException("ADB stream ID changed on close");
                    ended = true;
                } else {
                    if (packet.arg0 == 0) throw new IOException("Invalid zero ADB stream ID");
                    if (remoteId == 0) {
                        if (packet.command != OKAY) throw new IOException("ADB stream data before open acknowledgement");
                        remoteId = packet.arg0;
                    } else if (remoteId != packet.arg0) throw new IOException("ADB stream ID changed");
                    if (packet.command == OKAY) canWrite = true;
                    else {
                        // Legacy adbd can leave several WRTE packets in flight. Keep
                        // their order without treating a small burst as a transport loss.
                        // Memory remains bounded even if a peer disregards every ACK.
                        if (incoming.size() >= MAX_QUEUED_PACKETS
                                || queuedBytes + packet.data.length > MAX_QUEUED_BYTES)
                            throw new IOException("ADB receive buffer exceeded 256 KiB / 64 packets");
                        if (!incoming.isEmpty()) queuedBurstPackets.incrementAndGet();
                        incoming.add(packet.data);
                        queuedBytes += packet.data.length;
                    }
                }
                monitor.notifyAll();
            }
        }

        /** Returns one payload, or null after remote EOF. Timeout does not corrupt the stream. */
        public byte[] read(int timeoutMs) throws IOException {
            long deadline = deadline(timeoutMs);
            synchronized (readSerial) {
                byte[] result;
                boolean acknowledge;
                synchronized (monitor) {
                    while (incoming.isEmpty() && !ended && failure == null) await(deadline);
                    checkFailure();
                    if (incoming.isEmpty()) return null;
                    result = incoming.remove();
                    queuedBytes -= result.length;
                    acknowledge = !ended;
                }
                if (acknowledge) send(OKAY, localId, remoteId, EMPTY);
                return result;
            }
        }

        public void write(byte[] data, int timeoutMs) throws IOException {
            long deadline = deadline(timeoutMs);
            synchronized (writeSerial) {
                int offset = 0;
                while (offset < data.length) {
                    synchronized (monitor) {
                        while (!canWrite && !ended && failure == null) await(deadline);
                        checkWritable();
                        canWrite = false;
                    }
                    int length = Math.min(maxWrite, data.length - offset);
                    send(WRTE, localId, remoteId, Arrays.copyOfRange(data, offset, offset + length));
                    offset += length;
                }
                synchronized (monitor) {
                    while (!canWrite && !ended && failure == null) await(deadline);
                    checkWritable();
                }
            }
        }

        private void checkFailure() throws IOException { if (failure != null) throw failure; }
        private void checkWritable() throws IOException {
            checkFailure();
            if (ended) throw new IOException("ADB stream is closed");
        }
        private void await(long deadline) throws IOException {
            try { monitor.wait(remaining(deadline)); }
            catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException("ADB service interrupted", e);
            }
        }
        private void fail(IOException cause) {
            synchronized (monitor) { failure = cause; ended = true; monitor.notifyAll(); }
        }
        @Override public void close() {
            int peer;
            boolean sendClose;
            synchronized (monitor) {
                sendClose = !ended;
                ended = true;
                peer = remoteId;
                incoming.clear();
                queuedBytes = 0;
                monitor.notifyAll();
            }
            if (activeStream == this) activeStream = null;
            if (sendClose && peer != 0 && !closed) {
                try { send(CLSE, localId, peer, EMPTY); }
                catch (IOException ignored) { /* send already closes a failed transport. */ }
            }
        }
    }
}
