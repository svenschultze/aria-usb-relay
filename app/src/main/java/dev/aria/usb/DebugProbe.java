package dev.aria.usb;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.StandardCopyOption;

/** Private debug-build mailbox for hardware bring-up. Requires local adb run-as access. */
final class DebugProbe {
    static String runPending(Context context, AdbConnection connection) throws IOException {
        if ((context.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) == 0) return null;
        File request = new File(context.getFilesDir(), "probe-request.txt");
        if (!request.isFile()) return null;
        if (request.length() > 4096) { request.delete(); throw new IOException("Diagnostic command is too long"); }
        String command = new String(Files.readAllBytes(request.toPath()), StandardCharsets.UTF_8).trim();
        if (!request.delete()) throw new IOException("Cannot consume diagnostic request");
        String response;
        try { response = connection.shell(command, 256 * 1024, 15000); }
        catch (IOException e) { response = "Probe failed: " + e.getMessage(); }
        File pendingResponse = new File(context.getFilesDir(), "probe-response.tmp");
        File completedResponse = new File(context.getFilesDir(), "probe-response.txt");
        Files.write(pendingResponse.toPath(), response.getBytes(StandardCharsets.UTF_8));
        try {
            Files.move(pendingResponse.toPath(), completedResponse.toPath(),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(pendingResponse.toPath(), completedResponse.toPath(), StandardCopyOption.REPLACE_EXISTING);
        }
        return response;
    }
}
