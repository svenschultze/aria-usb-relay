package dev.aria.usb;

import java.io.File;
import java.io.IOException;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.interfaces.RSAPrivateCrtKey;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.RSAKeyGenParameterSpec;
import java.security.spec.RSAPublicKeySpec;
import java.util.Base64;

/** An app-private, persistent ADB host identity, never shared with the laptop. */
public final class AdbIdentity {
    private static final byte[] SHA1_DIGEST_INFO = {
        0x30, 0x21, 0x30, 0x09, 0x06, 0x05, 0x2b, 0x0e, 0x03, 0x02,
        0x1a, 0x05, 0x00, 0x04, 0x14
    };
    private final PrivateKey privateKey;
    private final RSAPublicKey publicKey;

    public AdbIdentity(KeyPair pair) {
        privateKey = pair.getPrivate();
        publicKey = (RSAPublicKey) pair.getPublic();
        if (publicKey.getModulus().bitLength() != 2048)
            throw new IllegalArgumentException("ADB requires RSA-2048");
    }

    /** Pass Context.getNoBackupFilesDir(); a corrupt existing identity is never replaced. */
    public static synchronized AdbIdentity loadOrCreate(File privateDirectory)
            throws IOException, GeneralSecurityException {
        if (!privateDirectory.isDirectory() && !privateDirectory.mkdirs())
            throw new IOException("Cannot create private ADB identity directory");
        File keyFile = new File(privateDirectory, "aria-adb-identity.pk8");
        KeyFactory factory = KeyFactory.getInstance("RSA");
        if (keyFile.exists()) {
            if (keyFile.length() > 8192) throw new IOException("Invalid saved ADB identity size");
            RSAPrivateCrtKey key = (RSAPrivateCrtKey) factory.generatePrivate(
                    new PKCS8EncodedKeySpec(Files.readAllBytes(keyFile.toPath())));
            return new AdbIdentity(new KeyPair(factory.generatePublic(new RSAPublicKeySpec(
                    key.getModulus(), key.getPublicExponent())), key));
        }
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
        generator.initialize(new RSAKeyGenParameterSpec(2048, RSAKeyGenParameterSpec.F4));
        KeyPair pair = generator.generateKeyPair();
        File temporary = File.createTempFile("aria-adb-identity-", ".tmp", privateDirectory);
        try {
            // Android supports POSIX permissions. Desktop Windows tests inherit the
            // caller's directory ACL; the caller must always provide a private directory.
            try { Files.setPosixFilePermissions(temporary.toPath(), PosixFilePermissions.fromString("rw-------")); }
            catch (UnsupportedOperationException ignored) {}
            Files.write(temporary.toPath(), pair.getPrivate().getEncoded());
            Files.move(temporary.toPath(), keyFile.toPath(), StandardCopyOption.ATOMIC_MOVE);
        } finally {
            Files.deleteIfExists(temporary.toPath());
        }
        return new AdbIdentity(pair);
    }

    /** AUTH tokens are already a SHA-1 digest: SHA1withRSA would incorrectly hash twice. */
    public byte[] signToken(byte[] token) throws GeneralSecurityException {
        if (token.length != 20) throw new GeneralSecurityException("ADB AUTH token must be 20 bytes");
        Signature signature = Signature.getInstance("NONEwithRSA");
        signature.initSign(privateKey);
        signature.update(SHA1_DIGEST_INFO);
        signature.update(token);
        return signature.sign();
    }

    public byte[] publicKeyPayload() {
        return AdbProtocol.cString(Base64.getEncoder().encodeToString(encodePublicKey(publicKey))
                + " aria-usb@android");
    }

    public String fingerprint() throws GeneralSecurityException {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(encodePublicKey(publicKey));
        return "SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(digest);
    }

    /** Android's 524-byte RSA structure; all integers and arrays are little endian. */
    public static byte[] encodePublicKey(RSAPublicKey key) {
        BigInteger modulus = key.getModulus();
        if (modulus.bitLength() != 2048) throw new IllegalArgumentException("RSA-2048 required");
        BigInteger radix = BigInteger.ONE.shiftLeft(32);
        int n0inv = modulus.modInverse(radix).negate().mod(radix).intValue();
        BigInteger rr = BigInteger.ONE.shiftLeft(4096).mod(modulus);
        return ByteBuffer.allocate(524).order(ByteOrder.LITTLE_ENDIAN)
                .putInt(64).putInt(n0inv).put(littleEndian(modulus, 256))
                .put(littleEndian(rr, 256)).putInt(key.getPublicExponent().intValue()).array();
    }

    private static byte[] littleEndian(BigInteger number, int length) {
        byte[] big = number.toByteArray(), little = new byte[length];
        for (int i = 0; i < length && i < big.length; i++) little[i] = big[big.length - 1 - i];
        return little;
    }
}
