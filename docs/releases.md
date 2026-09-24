# Build, versioning, and releases

## Versioning

`version.properties` contains the Android `versionCode` and `versionName`. Increment the integer code for every distributable update; Android uses it to order upgrades. Environment variables `ARIA_VERSION_CODE` and `ARIA_VERSION_NAME` override the file for a release pipeline. Version strings permit letters, digits, dots, plus signs, underscores, and hyphens.

The first release candidate is `0.1.0` / code `1`. A changelog entry is not evidence that a binary has been published. The current repository does not automatically tag, publish, or upload a release.

## Unsigned validation

With the Android SDK and JDK configured:

```powershell
.\build.ps1 assembleDebug assembleRelease lintDebug --no-daemon
```

On Linux, use `bash gradlew` instead. Without signing environment variables, the release output is `app/build/outputs/apk/release/app-release-unsigned.apk`. This is useful for build validation, not direct installation. Debug builds use the developer's standard Android debug key and have the diagnostic mailbox enabled.

## Optional release signing

Provision a release keystore outside this repository using your organization's normal key-management process. The project never generates or publishes a signing key. Provide all four variables through a protected local environment or secret manager:

| Environment variable | Purpose |
| --- | --- |
| `ARIA_KEYSTORE_PATH` | Path to the protected keystore |
| `ARIA_KEYSTORE_PASSWORD` | Store password |
| `ARIA_KEY_ALIAS` | Signing alias |
| `ARIA_KEY_PASSWORD` | Alias password |

Then run `assembleRelease`. Supplying only some variables fails configuration. Do not put secret values into `gradle.properties`, command-line arguments, committed scripts, logs, or issue reports. The signed output is `app/build/outputs/apk/release/app-release.apk`; release builds are not debuggable and do not process the private probe mailbox.

Keep the signing key stable for app upgrades. A release key cannot update an APK signed with a different debug key; uninstalling the debug app removes its private journal and identity, so export anything needed first. This repository's CI intentionally has no signing secrets.

## Pre-publication validation

1. Run the checks in [CONTRIBUTING](../CONTRIBUTING.md), including the public-source audit and Android lint/build tasks.
2. Review the staged file list and diff. Include only source, public documentation, templates, and the standard Gradle wrapper. Keep SDK binaries, captures, diagnostics, local settings, and all credentials out of Git and release archives.
3. Verify the signed APK with Android Build-Tools `apksigner verify --verbose` and record its SHA-256 separately from the signing key. Confirm package/version metadata and upgrade behavior on the intended device.
4. Test the exact candidate: installation, explicit USB permission, foreground relay, live RGB/SLAM/IMU freshness, disconnect/reconnect behavior, and clean stop. State which platforms had hardware validation.
5. Publish only deliberately selected artifacts with checksums and release notes describing known limits. Do not upload the workspace or ignored runtime directories.

GitHub CI uploads debug APK/lint artifacts for 14 days; these are development artifacts, not signed product releases. Enable branch protections, private vulnerability reporting, and dependency review according to the repository's maintainer policy after publication.

The Gradle wrapper JAR is the standard Gradle build bootstrap, covered by [Gradle's license](https://github.com/gradle/gradle/blob/master/LICENSE). The pinned distribution checksum comes from [Gradle's checksum reference](https://gradle.org/release-checksums/). The official Aria SDK remains separately licensed and installed by the user.
