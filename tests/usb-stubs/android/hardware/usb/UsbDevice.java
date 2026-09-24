package android.hardware.usb;
public final class UsbDevice {
    public int getInterfaceCount() { return 1; }
    public UsbInterface getInterface(int index) { return new UsbInterface(); }
}
