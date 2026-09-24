# Experimental USB/IP transport

`UsbIpServer` exports the glasses' real composite USB device on phone loopback
`127.0.0.1:3240`. An authenticated ADB tunnel carries the connection to the laptop.
The importer runs the normal Linux USB drivers and authenticated Aria Client SDK.
The phone exporter and the app's direct ADB telemetry mode must run separately.

The implementation supports USB/IP 1.1.1 device listing/import, control transfers,
bulk transfers, interrupt transfers, and URB cancellation. Queues are limited to
64 requests and 8 MiB of buffers, with at most 1 MiB per request. Pending USB reads
use Android `UsbRequest`, so idle periods do not repeatedly cancel partial reads.

Configuration handling follows the exporter model: Android has already addressed
and configured the physical device. `SET_ADDRESS` is acknowledged locally, and
`SET_CONFIGURATION` requesting the already selected value succeeds without
reconfiguring the physical interfaces. A historical Linux USB/IP exporter used
the same workaround because reconfiguration could unbind the exporting driver;
modern Linux changed that implementation. This Android experiment retains the
workaround because its public USB APIs and observed hardware behavior differ.
The laptop still controls alternate settings through `SET_INTERFACE`.
A different configuration requires existing transfers to be unlinked first.

This same-value behavior matters on the tested Aria/Pixel connection: physically
reselecting the current configuration was followed by ADB timeouts and repeated
URB cancellations after USB import. The successful earlier streaming build had
acknowledged that request without a physical reconfiguration. The restored
behavior should be checked on the hardware whenever this code changes.

Limitations: isochronous transfers are unsupported; Android's public USB API does
not expose exact native URB error numbers; synchronous failures map to `EIO`.
Unplugging or device re-enumeration ends the import and requires importing the new
device again. Re-selecting the current configuration does not implement a full
physical reset; an importer needing alternate-setting changes must issue them
explicitly.

Sources: [USB/IP wire protocol](https://docs.kernel.org/usb/usbip_protocol.html),
[Historical Linux USB/IP configuration handling](https://android.googlesource.com/kernel/msm/+/f6509b5f002a69c299b04d32a876d051a0b52f68/drivers/staging/usbip/stub_rx.c),
[Android USB host configuration ioctl](https://android.googlesource.com/platform/system/core/+/master/libusbhost/usbhost.c).
