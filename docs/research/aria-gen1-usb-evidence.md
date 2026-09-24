# Aria Gen 1 USB integration evidence

Research date: 2026-09-24. This file distinguishes documented behavior, binary inspection, and the subsequently verified phone-to-laptop USB relay. Standalone Android sensor decoding remains unimplemented.

## Official documentation

- [Client SDK overview](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk): Gen 1 device control and subscriptions use the Client SDK over USB or Wi-Fi.
- [Setup and pairing](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/setup): `aria auth pair` requires approval in the Companion App. The control certificate remains until revoked or a factory reset.
- [Streaming internals](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/concepts/streaming_internals): starting acquisition and subscribing are separate operations. Data is transmitted only to a subscriber. Streaming certificates are distinct from control-pairing certificates. Persistent streaming certificates can be used on several computers.
- [Gen 1 API reference](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/api_reference): USB device control relies on ADB; `DeviceClientConfig.adb_path` selects its executable. The API names DDS, RNDIS, and NCM explicitly. Sensor callbacks cover camera, audio, magnetometer, barometer, and IMU.
- [Linux USB troubleshooting](https://facebookresearch.github.io/projectaria_tools/docs/ARK/troubleshooting/linux_usb_driver): example initial USB VID/PID `2833:0086`. This is an example, not an exhaustive allowlist.
- [SDK troubleshooting](https://facebookresearch.github.io/projectaria_tools/docs/ARK/sdk/sdk_troubleshooting): USB streaming exposes an Ethernet interface. UDP streaming ports 7000–8000 must be reachable.
- [Published SDK artifacts](https://pypi.org/project/projectaria-client-sdk/2.5.0/): official Linux ARM64 wheels exist. They use glibc and are not Android/Bionic JNI libraries. No Android SDK wheel or standalone AAR is published there.
- [Android USB host API](https://developer.android.com/develop/connectivity/usb/host): apps can enumerate, obtain permission for, and communicate with USB endpoints. USB permission alone does not provide an Aria protocol decoder.

Gen 1 must not be confused with Gen 2's HTTP/TLS streaming transport on port 6768.

## Inspected official SDK artifact

Artifact: `projectaria_client_sdk-2.5.0-cp312-cp312-manylinux_2_34_aarch64.whl`.

SHA-256: `0368d9b92d6aa4a1edc19b9075d65a86f090bd046a12a9ae4fd93eb2cf574181`.

The wheel contains `aria/sdk.pyi`, a Python extension, and `aria/libsdk_core.so`. Gen 1 control, certificate handling, and DDS live in native code rather than the Python sample wrappers. The ARM64 wheel contains the Gen 1 Python extension but no `aria-cli` executable despite the entry-point wrapper expecting one.

The distributed `aria/doctor.py` maps the Gen 1 Ethernet mode to USB `2833:0300`; its rules separately map Gen 2 to `2833:9001` and `2833:9002`. The same file configures automatic IPv4/IPv6 addressing without making the USB network the default route. Initial ADB/MTP and streaming-network USB product IDs differ.

The binary includes Fast-DDS 2.9.0 paths and DDS security code. It also includes the complete protobuf descriptor for `aria.proto` in namespace `aria_proto`.

## Reproducible protocol schema extraction

`extract_sdk_schema.py` reads the official wheel without installing or executing it and extracts `descriptor_table_protodef_aria_2eproto` from its ELF symbol table. It needs only Python's standard library:

```text
python docs/research/extract_sdk_schema.py downloaded-sdk.whl docs/research/aria-gen1-protobuf-schema-sdk-2.5.0.json
```

The resulting JSON records 122 message descriptors, 23 enums, field numbers/types/defaults, and source hashes. It is a schema reference, not a complete client implementation. Protobuf numeric field-type values follow `google.protobuf.FieldDescriptorProto.Type`. Label 1 is optional, 2 required, 3 repeated.

## Gen 1 control transport: binary observations

These were recovered from the official SDK binary. They still require validation against the connected firmware before being used in the app.

1. ADB forwards a dynamically chosen localhost TCP port to a control TCP port on the glasses. Literal command templates include `forward tcp:0 tcp:` and `host-serial:{}:forward:tcp:{};tcp:{}`.
2. `aria::sdk::internal::getServerPort(ConnectionType)` returns 52742 for enum value 0, 52743 otherwise. Its AArch64 code at virtual address `0x3a2c110` is `cmp w0,#0; mov w8,#0xce06; cinc w0,w8,ne` with function prologue/epilogue. The authentication coroutine explicitly connects with enum value 0. Other SDK code distinguishes SSL and ordinary sockets. Therefore 52742 is the initial pairing path; 52743 is the subsequent secure path. This mapping has not yet been tested on hardware.
3. `DeviceClientSocket::co_write` prefixes its payload with a four-byte, big-endian length. The payload is a protobuf `aria_proto.Request`. No USB-level sensor decoder follows from this framing.
4. `DeviceController::sendRequestImpl` sets request version to 1, allocates a sequence number, sets the requested method, and serializes the protobuf.

Envelope fields:

| Request field | Number | Type |
| --- | --- | --- |
| version | 1 | required uint32; SDK uses 1 |
| method | 2 | Method enum |
| seq | 3 | uint32 |
| body | 4 | bytes containing method-specific protobuf |
| compress_response | 5 | bool |

| Response field | Number | Type |
| --- | --- | --- |
| seq | 1 | uint32 |
| code | 2 | ResponseCode: SUCCESS=0, FAIL=1, FAIL_RETRY=2 |
| body | 3 | bytes |
| compressed | 4 | bool |

Selected method values: `PING=0`, `HELLO=1`, `AUTHENTICATE=2`, `HMD_STATUS=8001`, `HMD_VERSION=8002`, `START_RECORDING_V2=18010`, `RECORDING_STATE=18016`, `INSTALL_STREAMING_CERTS=18031`, `INSTALL_TLS_CLIENT_CERTS=18046`.

The complete `StartRecordingV2Request` fields are in the extracted JSON. Its streaming interface enum is `WIFI_STA=0`, `USB_RNDIS=1`, `USB_NCM=2`, `WIFI_SAP=3`, `WIFI_PROXY=4`, `WIFI_ANY=5`. Do not confuse these wire enum values with SDK enum ordinals.

## Standard SDK certificate pairing observations

`DeviceClientImpl::co_authenticate` connects via connection type 0, generates a fresh client certificate, computes a SHA-256 hash of the generated PKCS#12 payload for confirmation, sends `requestInstallTlsClientCerts`, and disconnects. The Companion App performs the documented user approval; the client does not approve itself.

The generated key is RSA-2048. The self-signed certificate has a 365-day validity and common-name text `Aria Tls Client Cert`. Local names are `tls-client-key.pem`, `tls-client.pem`, `tls-client.p12` under `tls-client-certs/`. `InstallTlsClientCertsRequest` carries the PKCS#12 bytes in field 1. This is the normal pairing flow, separate from legacy HELLO/AUTHENTICATE challenge messages also present in the descriptor.

Do not remove, overwrite, or export existing user certificates while experimenting. If implementing this route, maintain a separate app-owned identity and obtain standard Companion App approval.

## Remaining sensor-streaming work

A working control connection is insufficient for camera/IMU streaming. A native Android implementation still needs:

- Working USB Ethernet on the phone, or an implemented userspace RNDIS/NCM network stack.
- Device control with valid pairing certificates and correct profile/streaming configuration.
- DDS discovery/subscription, matching topic names and serialized types, and compatible DDS security certificates.
- Bounded queues, image decoding, timestamp handling, and local buffering/reconnect behavior.

The installed Companion App contains an Android native SDK inside a React Native TurboModule. `libxplat_Ariane_ArianeClientSDKAndroid.so` exposes `ClientSDKModuleProvider`; it does not export a JNI interface or the ordinary C++ SDK APIs. Its provider returns a shared pointer and accepts one shared-pointer argument, consistent with a CallInvoker-based TurboModule factory, but the exact type is inferred. Reusing it requires the matching JSI/IRuntime ABI and transitive runtime libraries. This is not currently a drop-in AAR solution.

The connected firmware may expose native sensor command-line programs. Their actual `--help` output and behavior should be inspected as a separate route. Public searches found no official documentation for `upstream-stream-sensor-app`, `upstream-camera-test`, or `stream-camera-serial`; do not infer their options from their names.

## Verified laptop fallback on the connected Gen 1

The firmware sensor programs were subsequently tested: they required permissions
unavailable to the ordinary ADB shell on this production device. No root bypass
was attempted.

USB/IP proved viable on the actual phone and laptop. The Android app's USB/IP
server relayed the glasses to WSL; official SDK 2.5.0 authenticated using the
existing user certificates, started USB NCM streaming, and delivered live RGB,
two SLAM camera views, and two IMUs. Linux SDK 2.5.0 converts public
`StreamingInterface.Usb` (1) to wire `USB_NCM` (2), observed in the x86-64
`DeviceController::requestStartRecording` implementation and then confirmed by
the real `cdc_ncm` interface. The device changed from `2833:0083` to `2833:0300`.

See [the sanitized hardware validation record](usbip-live-run.md). Raw device
snapshots are intentionally excluded from the public repository. This solution still
requires the laptop for DDS decoding and stores only bounded recent samples; it
does not establish a standalone Android decoder or lossless offline buffering.
