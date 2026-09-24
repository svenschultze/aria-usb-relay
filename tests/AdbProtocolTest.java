package dev.aria.usb;

import java.io.File;
import java.io.IOException;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.Signature;
import java.security.interfaces.RSAPublicKey;
import java.util.Arrays;
import java.util.Base64;

/** No Android runtime or test framework needed: run with java -ea. */
public final class AdbProtocolTest {
    public static void main(String[] args) throws Exception {
        byte[] data = {0, 1, (byte) 128, (byte) 255};
        byte[] header = AdbProtocol.header(AdbProtocol.WRTE, 12, 81, data);
        assert header[0] == 'W' && header[1] == 'R' && header[2] == 'T' && header[3] == 'E';
        assert ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN).getInt(16) == 384;
        AdbProtocol.Packet packet = AdbProtocol.parseHeader(header, 4096).packet(data);
        assert packet.arg0 == 12 && packet.arg1 == 81 && Arrays.equals(packet.data, data);
        byte[] broken = header.clone(); broken[20] ^= 1;
        expectIo(() -> AdbProtocol.parseHeader(broken, 4096));
        expectIo(() -> AdbProtocol.parseHeader(header, 2));
        expectIo(() -> AdbProtocol.parseHeader(header, 4096).packet(new byte[4]));
        expectIo(() -> AdbProtocol.parseHeader(AdbProtocol.header(1234, 0, 0, new byte[0]), 4096));
        byte[] huge = header.clone();
        ByteBuffer.wrap(huge).order(ByteOrder.LITTLE_ENDIAN).putInt(12, -1);
        expectIo(() -> AdbProtocol.parseHeader(huge, 4096));

        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(2048);
        KeyPair pair = generator.generateKeyPair();
        RSAPublicKey publicKey = (RSAPublicKey) pair.getPublic();
        AdbIdentity identity = new AdbIdentity(pair);
        byte[] encoded = AdbIdentity.encodePublicKey(publicKey);
        ByteBuffer structure = ByteBuffer.wrap(encoded).order(ByteOrder.LITTLE_ENDIAN);
        assert encoded.length == 524 && structure.getInt() == 64;
        long n0inv = structure.getInt() & 0xffffffffL;
        byte[] modulusBytes = new byte[256], rrBytes = new byte[256];
        structure.get(modulusBytes); structure.get(rrBytes);
        BigInteger modulus = fromLittleEndian(modulusBytes);
        assert modulus.equals(publicKey.getModulus());
        assert modulus.multiply(BigInteger.valueOf(n0inv)).add(BigInteger.ONE)
                .mod(BigInteger.ONE.shiftLeft(32)).signum() == 0;
        assert fromLittleEndian(rrBytes).equals(BigInteger.ONE.shiftLeft(4096).mod(modulus));
        assert structure.getInt() == publicKey.getPublicExponent().intValue();
        byte[] payload = identity.publicKeyPayload();
        assert payload[payload.length - 1] == 0;
        assert Arrays.equals(encoded, Base64.getDecoder().decode(
                new String(payload, java.nio.charset.StandardCharsets.UTF_8).split(" ")[0]));

        // Independent RSA verifier hashes the original message once; the token signer
        // receives its SHA-1 digest. This catches the common double-hashing error.
        byte[] challengeMessage = "ADB AUTH test input".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        byte[] token = MessageDigest.getInstance("SHA-1").digest(challengeMessage);
        Signature verifier = Signature.getInstance("SHA1withRSA");
        verifier.initVerify(publicKey);
        verifier.update(challengeMessage);
        assert verifier.verify(identity.signToken(token));
        try { identity.signToken(new byte[19]); throw new AssertionError("Bad token accepted"); }
        catch (java.security.GeneralSecurityException expected) {}

        File privateDirectory = Files.createTempDirectory("aria-adb-test-").toFile();
        try {
            AdbIdentity first = AdbIdentity.loadOrCreate(privateDirectory);
            AdbIdentity second = AdbIdentity.loadOrCreate(privateDirectory);
            assert first.fingerprint().equals(second.fingerprint());
            assert Arrays.equals(first.publicKeyPayload(), second.publicKeyPayload());
            Files.write(new File(privateDirectory, "aria-adb-identity.pk8").toPath(), new byte[]{1, 2});
            try { AdbIdentity.loadOrCreate(privateDirectory); throw new AssertionError("Corrupt identity replaced"); }
            catch (java.security.GeneralSecurityException expected) {}
        } finally {
            Files.deleteIfExists(new File(privateDirectory, "aria-adb-identity.pk8").toPath());
            Files.delete(privateDirectory.toPath());
        }
        System.out.println("PASS: ADB framing, corruption rejection, public-key math, AUTH signature, persistent identity");
    }

    private static BigInteger fromLittleEndian(byte[] bytes) {
        byte[] reversed = bytes.clone();
        for (int i = 0; i < reversed.length / 2; i++) {
            byte value = reversed[i]; reversed[i] = reversed[reversed.length - 1 - i];
            reversed[reversed.length - 1 - i] = value;
        }
        return new BigInteger(1, reversed);
    }
    private interface IoAction { void run() throws IOException; }
    private static void expectIo(IoAction action) throws Exception {
        try { action.run(); throw new AssertionError("Malformed packet accepted"); }
        catch (IOException expected) {}
    }
}
