package dev.aria.usb;

import android.hardware.usb.*;
import java.io.*;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;
import java.util.concurrent.*;

/** Exercises the production connection against a byte-level USB/ADB peer. */
public final class AdbConnectionTest {
    public static void main(String[] args) throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        KeyPair pair = generator.generateKeyPair();
        AdbIdentity identity = new AdbIdentity(pair);
        Peer usb = new Peer(pair.getPublic(), false);
        try (AdbConnection connection = new AdbConnection(usb, new UsbInterface(), identity)) {
            assert connection.connect().equals("device::test-peer");
            assert connection.isConnected();
            assert connection.shell("test", 128).equals("actual glasses sample\n");
            assert connection.shell("empty", 128).isEmpty();
            try { connection.shell("large", 8); throw new AssertionError("Output limit ignored"); }
            catch (IOException expected) { assert expected.getMessage().contains("exceeded"); }
            try { connection.shell("hang", 128, 100); throw new AssertionError("No shell timeout"); }
            catch (SocketTimeoutException expected) {}
            assert connection.shell("test", 128).equals("actual glasses sample\n");
            // Regression: Aria's Android 7 adbd delivered a burst while the consumer
            // was busy, which must preserve data rather than fail the whole USB link.
            try (AdbConnection.Stream burst = connection.openTcp(1235)) {
                long waitUntil = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                while (connection.getQueuedBurstPackets() < 31 && System.nanoTime() < waitUntil) Thread.sleep(5);
                assert connection.getQueuedBurstPackets() >= 31;
                for (int i = 0; i < 32; i++)
                    assert new String(burst.read(2000), StandardCharsets.UTF_8).equals("chunk-" + i);
            }
            try (AdbConnection.Stream tcp = connection.openTcp(1234)) {
                byte[] payload = new byte[512];
                Arrays.fill(payload, (byte) 'x');
                tcp.write(payload, 2000);
                assert usb.zeroLengthPackets == 1 : "Full USB payload lacked ZLP";
                assert usb.receivedStreamBytes == 512;
                // Concurrent inbound/outbound traffic is permitted without a shared read/write lock.
                FutureTask<byte[]> read = new FutureTask<>(() -> tcp.read(2000));
                new Thread(read).start();
                tcp.write("trigger".getBytes(StandardCharsets.UTF_8), 2000);
                assert new String(read.get(2, TimeUnit.SECONDS), StandardCharsets.UTF_8).equals("response");
            }
            assert connection.getReceivedBytes() > 0 && connection.getSentBytes() > 0;
            try (AdbConnection.Stream waiting = connection.openService("shell:hang", 2000)) {
                FutureTask<byte[]> read = new FutureTask<>(() -> waiting.read(5000));
                new Thread(read).start();
                connection.close();
                try { read.get(1, TimeUnit.SECONDS); throw new AssertionError("Close did not cancel reader"); }
                catch (ExecutionException expected) { assert expected.getCause() instanceof IOException; }
            }
        }
        assert usb.closed;
        // Reopening a USB handle does not reset adbd or drain its endpoint. Ignore
        // old service frames and retry CONNECT once if that leaves adbd silent.
        Peer stale = new Peer(pair.getPublic(), false);
        stale.ignoreFirstConnect = true;
        stale.enqueue(AdbProtocol.WRTE, 400, 300, Peer.bytes("old session data"));
        stale.enqueue(AdbProtocol.OKAY, 400, 300, new byte[0]);
        stale.enqueue(AdbProtocol.CLSE, 400, 300, new byte[0]);
        try (AdbConnection connection = new AdbConnection(stale, new UsbInterface(), identity)) {
            assert connection.connect(3000).equals("device::test-peer");
            assert connection.getDiscardedHandshakePackets() == 3;
            assert connection.shell("test", 128).equals("actual glasses sample\n");
        }
        Peer needsApproval = new Peer(pair.getPublic(), true);
        try (AdbConnection connection = new AdbConnection(needsApproval, new UsbInterface(), identity)) {
            try { connection.connect(200); throw new AssertionError("Unknown host was authorized"); }
            catch (AdbConnection.AuthorizationRequiredException expected) {}
            assert needsApproval.publicKeyOffered;
            assert !connection.isConnected();
        }
        System.out.println("PASS: USB partial transfers, RSA handshake, shell EOF/limits, timeout recovery, burst buffering, stale-frame reconnect, TCP duplex, ZLP, cancellation, authorization refusal");
    }

    private static final class Peer extends UsbDeviceConnection {
        private final BlockingQueue<byte[]> inbound = new LinkedBlockingQueue<>();
        private final PublicKey publicKey;
        private final boolean rejectKey;
        private final ByteArrayOutputStream outbound = new ByteArrayOutputStream();
        private byte[] readPart;
        private int readOffset, expected = 24;
        private int command, local, remote, length, expectedChecksum;
        private int tokenCount;
        private final Map<Integer, String> services = new ConcurrentHashMap<>();
        volatile boolean closed, publicKeyOffered, ignoreFirstConnect;
        volatile int zeroLengthPackets, receivedStreamBytes;
        Peer(PublicKey publicKey, boolean rejectKey) { this.publicKey = publicKey; this.rejectKey = rejectKey; }

        @Override public int bulkTransfer(UsbEndpoint endpoint, byte[] bytes, int offset, int size, int timeout) {
            if (closed) return -1;
            try {
                if (endpoint.getDirection() == 128) {
                    if (readPart == null) {
                        readPart = inbound.poll(timeout, TimeUnit.MILLISECONDS);
                        readOffset = 0;
                        if (readPart == null || closed) return -1;
                    }
                    int count = Math.min(7, Math.min(size, readPart.length - readOffset));
                    System.arraycopy(readPart, readOffset, bytes, offset, count);
                    readOffset += count;
                    if (readOffset == readPart.length) readPart = null;
                    return count;
                }
                if (size == 0) { zeroLengthPackets++; return 0; }
                int count = Math.min(11, size);
                outbound.write(bytes, offset, count);
                assert outbound.size() <= expected : "Host merged header/payload USB transfers";
                if (outbound.size() == expected) {
                    byte[] part = outbound.toByteArray(); outbound.reset();
                    if (expected == 24 && length == 0) {
                        command = word(part, 0); local = word(part, 4); remote = word(part, 8);
                        length = word(part, 12); expectedChecksum = word(part, 16);
                        assert word(part, 20) == ~command;
                        if (length > 0) expected = length;
                        else process(new byte[0]);
                    } else {
                        assert part.length == length;
                        int checksum = 0; for (byte b : part) checksum += b & 255;
                        assert checksum == expectedChecksum;
                        expected = 24; length = 0;
                        process(part);
                    }
                }
                return count;
            } catch (Exception e) { throw new RuntimeException(e); }
        }

        private void process(byte[] payload) throws Exception {
            if (command == AdbProtocol.CNXN) {
                assert local == 0x01000000 && remote == 4096;
                if (ignoreFirstConnect) { ignoreFirstConnect = false; return; }
                enqueue(AdbProtocol.AUTH, 1, 0, token());
            } else if (command == AdbProtocol.AUTH) {
                if (local == 2) {
                    Signature verifier = Signature.getInstance("SHA1withRSA");
                    verifier.initVerify(publicKey);
                    verifier.update("challenge-0".getBytes(StandardCharsets.UTF_8));
                    assert verifier.verify(payload) : "Invalid ADB authentication signature";
                    if (rejectKey) enqueue(AdbProtocol.AUTH, 1, 0, token());
                    else enqueue(AdbProtocol.CNXN, 0x01000000, 4096, bytes("device::test-peer\0"));
                } else {
                    assert local == 3 && payload[payload.length - 1] == 0;
                    publicKeyOffered = true;
                }
            } else if (command == AdbProtocol.OPEN) {
                assert remote == 0;
                String service = new String(payload, StandardCharsets.UTF_8).replace("\0", "");
                services.put(local, service);
                enqueue(AdbProtocol.OKAY, local + 100, local, new byte[0]);
                if (service.equals("shell:empty")) enqueue(AdbProtocol.CLSE, local + 100, local, new byte[0]);
                else if (service.equals("shell:test")) enqueue(AdbProtocol.WRTE, local + 100, local, bytes("actual glasses sample\n"));
                else if (service.equals("shell:large")) enqueue(AdbProtocol.WRTE, local + 100, local, new byte[4096]);
                else if (service.equals("tcp:1235")) {
                    for (int i = 0; i < 32; i++) enqueue(AdbProtocol.WRTE, local + 100, local, bytes("chunk-" + i));
                }
            } else if (command == AdbProtocol.OKAY) {
                assert remote == local + 100;
                if (services.getOrDefault(local, "").startsWith("shell:"))
                    enqueue(AdbProtocol.CLSE, remote, local, new byte[0]);
            } else if (command == AdbProtocol.WRTE) {
                assert remote == local + 100;
                receivedStreamBytes += payload.length;
                enqueue(AdbProtocol.OKAY, remote, local, new byte[0]);
                if (Arrays.equals(payload, bytes("trigger"))) enqueue(AdbProtocol.WRTE, remote, local, bytes("response"));
            } else if (command == AdbProtocol.CLSE) {
                assert remote != 0;
                services.remove(local);
            } else throw new AssertionError("Unexpected command");
        }

        private byte[] token() throws Exception {
            return MessageDigest.getInstance("SHA-1").digest(bytes("challenge-" + tokenCount++));
        }
        private void enqueue(int command, int arg0, int arg1, byte[] payload) {
            byte[] header = new byte[24];
            putWord(header, 0, command); putWord(header, 4, arg0); putWord(header, 8, arg1);
            putWord(header, 12, payload.length);
            int checksum = 0; for (byte b : payload) checksum += b & 255;
            putWord(header, 16, checksum); putWord(header, 20, ~command);
            inbound.add(header);
            if (payload.length != 0) inbound.add(payload);
        }
        @Override public void close() { closed = true; inbound.offer(new byte[0]); }
        private static byte[] bytes(String value) { return value.getBytes(StandardCharsets.UTF_8); }
        private static int word(byte[] bytes, int offset) {
            return (bytes[offset] & 255) | (bytes[offset + 1] & 255) << 8
                    | (bytes[offset + 2] & 255) << 16 | (bytes[offset + 3] & 255) << 24;
        }
        private static void putWord(byte[] bytes, int offset, int value) {
            for (int i = 0; i < 4; i++) bytes[offset + i] = (byte) (value >>> (i * 8));
        }
    }
}
