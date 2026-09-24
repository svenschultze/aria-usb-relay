package dev.aria.usb;

import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** Bounded on-phone journal, independent of laptop connectivity. Private app storage only. */
public final class SessionJournal {
    private static final long SEGMENT_LIMIT = 4L * 1024 * 1024;
    private final File current, previous;
    private long count;

    public SessionJournal(File directory) {
        current = new File(directory, "session.jsonl");
        previous = new File(directory, "session.previous.jsonl");
    }

    public synchronized void append(String type, String content) throws Exception {
        if (content.length() > 64 * 1024) content = content.substring(0, 64 * 1024) + "\n[diagnostic entry truncated]";
        byte[] line = (new JSONObject().put("received_at_ms", System.currentTimeMillis())
                .put("type", type).put("text", content).toString() + "\n").getBytes(StandardCharsets.UTF_8);
        if (current.length() + line.length > SEGMENT_LIMIT) {
            if (previous.exists() && !previous.delete()) throw new IOException("Could not rotate old journal");
            if (!current.renameTo(previous)) throw new IOException("Could not rotate journal");
        }
        try (FileOutputStream output = new FileOutputStream(current, true)) { output.write(line); }
        count++;
    }

    public synchronized long bytes() { return current.length() + previous.length(); }
    public synchronized long count() { return count; }

    public synchronized void export(OutputStream output) throws IOException {
        for (File file : new File[]{previous, current}) {
            if (!file.exists()) continue;
            try (InputStream input = new FileInputStream(file)) {
                byte[] bytes = new byte[8192];
                int length;
                while ((length = input.read(bytes)) != -1) output.write(bytes, 0, length);
            }
        }
    }
}
