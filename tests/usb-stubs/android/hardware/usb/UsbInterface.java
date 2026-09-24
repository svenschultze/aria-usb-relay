package android.hardware.usb;
public final class UsbInterface {
    public int getInterfaceClass() { return 255; }
    public int getInterfaceSubclass() { return 66; }
    public int getInterfaceProtocol() { return 1; }
    public int getEndpointCount() { return 2; }
    public UsbEndpoint getEndpoint(int index) { return new UsbEndpoint(index == 0 ? 128 : 0); }
}
