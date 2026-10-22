# Third-party notices

## Ghost in the Droid / android-agent

D3m0n retains MIT-licensed upstream source from https://github.com/ghost-in-the-droid/android-agent. The original license is preserved in LICENSE; its README and supporting integration/Portal/iOS/skills source are retained. Customized D3m0n source does not imply ownership of upstream projects.

## scrcpy Android server 5.0.1

`ops/scrcpy-server-v5.0.1.jar` is the official Genymobile scrcpy Android server release asset, renamed with a `.jar` extension for the existing D3m0n streaming code. It is required by the H.264/WebCodecs and native-control implementation; it is not a phone recording, an APK upload or private runtime state. Source: https://github.com/Genymobile/scrcpy, release https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1. Apache License 2.0 is preserved in `ops/SCRCPY-LICENSE.txt`.

The published GitHub asset digest and the downloaded SHA-256 both equal `764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536`; size 733930 bytes. The release was published 8 October 2026. Installation/discovery does not push or run this server on a phone; the existing explicit streaming/control operation does so only for a selected authorized device.

## Mobilerun

Fleet AI installs Mobilerun from https://github.com/droidrun/mobilerun at commit `301856138c6edeb2ae692bfab85b279ace520e59` (runtime SDK 0.7.0) into the separate isolated worker environment. Its own license remains with that checked-out source. No cloud credentials or model weights are redistributed in this repository.
