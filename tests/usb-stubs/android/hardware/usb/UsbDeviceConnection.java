package android.hardware.usb;
public abstract class UsbDeviceConnection {
    public boolean claimInterface(UsbInterface iface, boolean force) { return true; }
    public boolean releaseInterface(UsbInterface iface) { return true; }
    public abstract int bulkTransfer(UsbEndpoint endpoint, byte[] bytes, int offset, int length, int timeout);
    public abstract void close();
}
