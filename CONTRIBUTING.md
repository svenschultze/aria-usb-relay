# Contributing

Describe the concrete behavior you want to change and whether it affects Android USB handling, Linux networking/SDK startup, or the desktop interface. Keep changes bounded and preserve real-data provenance: never substitute phone sensors or synthetic frames into the live data path.

## Local checks

Use Python 3.10+ and JDK 17+ (CI uses JDK 21). From the repository root:

```text
python tests/audit_public.py
python tests/run_jvm.py
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest discover -s bridge -p "test_*.py" -v
```

On Windows, `tests/run.ps1` can discover Android Studio's JDK. Set `JAVA_HOME` for the portable runner when Java is not on `PATH`. Android checks:

```powershell
.\build.ps1 assembleDebug assembleRelease lintDebug --no-daemon
```

On Linux, use `bash gradlew` with the same tasks. An unsigned release build validates packaging; it is not an installable signed release. CI installs no proprietary SDK and needs no glasses, certificates, signing keys, or phone pairing.

## Hardware changes

Protocol and lifecycle tests cannot establish physical USB compatibility. For changes to enumeration, configuration, endpoints, cancellation, reconnect, or NCM startup, record the hardware/firmware context and actual results. Check clean startup, three camera/two IMU freshness, normal stop, detach, and renewed USB permission. Avoid presenting a short successful run as long-duration reliability evidence.

Run hardware checks only against the explicitly selected device and session. Do not reset user pairing, replace certificates, stop unrelated ADB servers, flush host networking, or use broad process-name kills. Keep root operations confined to USB import and scoped network setup; run the SDK as the normal user.

## Public source and diagnostics

Do not commit `.local/`, `.research/`, `diagnostics/`, private keys, certificates, keystores, companion APKs/libraries, VRS files, camera images, pairing codes, machine paths, or real device identifiers. Prefer RFC 5737 documentation addresses and explicit placeholders in tests/examples. `python tests/audit_public.py` checks tracked files and non-ignored source candidates, but manual review is still required before sharing logs.

Use focused tests for behavior at trust and transport boundaries. Keep Python control tooling dependency-light and preview queues bounded. Source and shell files use LF endings; Windows batch wrappers use CRLF. `.gitattributes` records those choices. Because executable bits may be lost on Windows checkouts, documented shell commands use `bash scripts/...`; maintainers can also mark `gradlew` and shell launchers executable in Git.

Pull requests should state the problem, resulting behavior, validation performed, and any remaining hardware limitations. Contributions are accepted under the repository's MIT license; do not add third-party code without its compatible license and attribution.
