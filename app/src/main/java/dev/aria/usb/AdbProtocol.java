package dev.aria.usb;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/** Small, Android-independent implementation of the ADB v1 wire framing. */
public final class AdbProtocol {
    public static final int CNXN = 0x4e584e43, AUTH = 0x48545541;
    public static final int OPEN = 0x4e45504f, OKAY = 0x59414b4f;
    public static final int CLSE = 0x45534c43, WRTE = 0x45545257;
    public static final int VERSION = 0x01000000;
    public static final int MAX_PAYLOAD = 4096, HEADER_LENGTH = 24;
    public static final byte[] EMPTY = new byte[0];

    private AdbProtocol() {}

    public static byte[] cString(String value) {
        if (value.indexOf('\0') >= 0) throw new IllegalArgumentException("Embedded NUL");
        return (value + "\0").getBytes(StandardCharsets.UTF_8);
    }

    // Despite the old protocol documentation calling this CRC32, ADB uses a byte sum.
    public static int checksum(byte[] bytes) {
        int sum = 0;
        for (byte b : bytes) sum += b & 255;
        return sum;
    }

    public static byte[] header(int command, int arg0, int arg1, byte[] data) {
        return ByteBuffer.allocate(HEADER_LENGTH).order(ByteOrder.LITTLE_ENDIAN)
                .putInt(command).putInt(arg0).putInt(arg1).putInt(data.length)
                .putInt(checksum(data)).putInt(~command).array();
    }

    public static Header parseHeader(byte[] bytes, int maxPayload) throws IOException {
        if (bytes.length != HEADER_LENGTH) throw new IOException("Wrong ADB header size");
        ByteBuffer b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        int command = b.getInt(), arg0 = b.getInt(), arg1 = b.getInt();
        int length = b.getInt(), checksum = b.getInt(), magic = b.getInt();
        if (magic != ~command) throw new IOException("Invalid ADB header magic");
        if (length < 0 || length > maxPayload) throw new IOException("Invalid ADB payload length: " + length);
        if (command != CNXN && command != AUTH && command != OPEN && command != OKAY
                && command != CLSE && command != WRTE) {
            throw new IOException("Unsupported ADB command: " + Integer.toHexString(command));
        }
        return new Header(command, arg0, arg1, length, checksum);
    }

    public static final class Header {
        public final int command, arg0, arg1, length, checksum;
        private Header(int command, int arg0, int arg1, int length, int checksum) {
            this.command = command; this.arg0 = arg0; this.arg1 = arg1;
            this.length = length; this.checksum = checksum;
        }
        public Packet packet(byte[] payload) throws IOException {
            if (payload.length != length || AdbProtocol.checksum(payload) != checksum)
                throw new IOException("Corrupt ADB payload");
            return new Packet(command, arg0, arg1, payload);
        }
    }

    public static final class Packet {
        public final int command, arg0, arg1;
        public final byte[] data;
        private Packet(int command, int arg0, int arg1, byte[] data) {
            this.command = command; this.arg0 = arg0; this.arg1 = arg1; this.data = data;
        }
    }
}
