package dev.aria.usb;

import android.hardware.usb.*;
import java.net.*;
import java.util.*;

/** Descriptor-only inspection: a USB connection is never reported as a sensor stream. */
public final class UsbInventory {
    private UsbInventory() {}

    public static boolean isCandidate(UsbDevice device) { return device.getVendorId() == 0x2833; }

    public static List<UsbDevice> candidates(UsbManager manager) {
        List<UsbDevice> result = new ArrayList<>();
        for (UsbDevice device : devices(manager)) if (isCandidate(device)) result.add(device);
        return result;
    }

    public static String serial(UsbManager manager, UsbDevice device) {
        if (!manager.hasPermission(device)) return "";
        try { String serial = device.getSerialNumber(); return serial == null ? "" : serial; }
        catch (SecurityException denied) { return ""; }
    }

    public static String label(UsbDevice device) {
        String product = device.getProductName();
        return String.format(Locale.US, "%s · %04X:%04X", product == null ? "Aria USB device" : product,
                device.getVendorId(), device.getProductId());
    }

    public static UsbInterface adbInterface(UsbDevice device) {
        for (int i = 0; i < device.getInterfaceCount(); i++) {
            UsbInterface face = device.getInterface(i);
            if (face.getInterfaceClass() == 255 && face.getInterfaceSubclass() == 66
                    && face.getInterfaceProtocol() == 1) return face;
        }
        return null;
    }

    public static List<UsbDevice> devices(UsbManager manager) {
        ArrayList<UsbDevice> devices = new ArrayList<>(manager.getDeviceList().values());
        devices.sort(Comparator.comparingInt((UsbDevice d) -> d.getVendorId() == 0x2833 ? 0 : 1)
                .thenComparing(UsbDevice::getDeviceName));
        return devices;
    }

    public static String describe(UsbManager manager) {
        StringBuilder out = new StringBuilder();
        for (UsbDevice device : devices(manager)) {
            out.append(String.format(Locale.US, "%s\nUSB %04X:%04X · %s\n",
                    device.getProductName() == null ? "USB device" : device.getProductName(),
                    device.getVendorId(), device.getProductId(),
                    manager.hasPermission(device) ? "permission granted" : "permission needed"));
            out.append("Path: ").append(device.getDeviceName()).append('\n');
            for (int i = 0; i < device.getInterfaceCount(); i++) {
                UsbInterface face = device.getInterface(i);
                out.append(String.format(Locale.US, "Interface %d / alt %d · %02X:%02X:%02X",
                        face.getId(), face.getAlternateSetting(), face.getInterfaceClass(),
                        face.getInterfaceSubclass(), face.getInterfaceProtocol()));
                if (face.getInterfaceClass() == 255 && face.getInterfaceSubclass() == 66) out.append(" · ADB");
                if (face.getInterfaceClass() == 224) out.append(" · wireless/RNDIS candidate");
                if (face.getInterfaceClass() == 2) out.append(" · CDC control");
                if (face.getInterfaceClass() == 10) out.append(" · CDC data");
                out.append('\n');
                for (int e = 0; e < face.getEndpointCount(); e++) {
                    UsbEndpoint ep = face.getEndpoint(e);
                    out.append(String.format(Locale.US, "  Endpoint 0x%02X · %s · type %d · %d bytes\n",
                            ep.getAddress(), ep.getDirection() == UsbConstants.USB_DIR_IN ? "IN" : "OUT",
                            ep.getType(), ep.getMaxPacketSize()));
                }
            }
            out.append('\n');
        }
        if (out.length() == 0) out.append("No USB devices visible to Android.\nConnect powered-on glasses with a data-capable cable. The Pixel must be the USB host.\n");
        out.append("\nPHONE NETWORK INTERFACES\n");
        try {
            Enumeration<NetworkInterface> faces = NetworkInterface.getNetworkInterfaces();
            if (faces != null) while (faces.hasMoreElements()) {
                NetworkInterface face = faces.nextElement();
                if (!face.isUp() || face.isLoopback()) continue;
                out.append(face.getName()).append(" · ");
                for (InterfaceAddress address : face.getInterfaceAddresses()) {
                    out.append(address.getAddress().getHostAddress()).append('/').append(address.getNetworkPrefixLength()).append(' ');
                }
                out.append('\n');
            }
        } catch (Exception e) { out.append("Unavailable: ").append(e.getMessage()); }
        return out.toString();
    }
}
