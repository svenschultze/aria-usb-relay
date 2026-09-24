# Security and privacy

Aria USB Relay runs local control software with access to a phone and camera-equipped glasses. Use it on machines and devices you control and capture only where you have appropriate permission.

The intended trust boundary is the local laptop user plus an explicitly paired Android ADB device. The Android USB/IP endpoint binds to phone loopback and is reached through authenticated ADB forwarding. Desktop and preview HTTP services bind to laptop loopback by default. Do not expose these services through router forwarding, public reverse proxies, or an unrestricted bind address. Loopback is not protection against malicious local software.

Official SDK authentication remains in place. Existing control certificates stay with the normal Linux user; startup uses matching ephemeral streaming certificates. Root privileges are limited to Linux USB import and interface setup. The project does not bypass device approval or bundle a companion application's native SDK.

The Android journal can contain device metadata and developer probe output. Desktop preferences contain local device/settings information; runtime logs and diagnostic exports may contain sensitive context. They are local and ignored by Git. Pairing codes must not be stored or included in bug reports. Review and redact diagnostics before sharing, even when an export is described as redacted.

The debug-only private probe can execute a supplied shell command on the glasses through an already connected app session. It requires authenticated ADB access and `run-as`; it is disabled in release builds. Do not install a debug APK on devices where that development capability is inappropriate.

## Reporting a vulnerability

If this GitHub repository has private vulnerability reporting enabled, use its **Security → Report a vulnerability** flow. Otherwise contact the repository maintainers privately through a contact they have published. Do not put exploit details, credentials, device identifiers, or captures into a public issue. A dedicated public security contact has not been configured in this source tree.

Include the affected version/commit, platform, minimal reproduction, impact, and whether hardware is required. There is no promised response SLA or supported-version window yet; this is an early research release.

## Supply chain and releases

GitHub Actions are pinned to commit IDs with read-only repository permissions. The Gradle distribution is pinned and checksum-verified. CI receives no release signing secrets and produces a debug artifact for testing. Maintainers sign distributable releases separately with their own protected key. The official SDK and OpenCV are installed from their upstream distributions, not copied into this repository.
