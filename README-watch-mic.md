# Watch Mic

Use a Wear OS watch (tested: Galaxy Watch5) as the Mac's microphone for VoiceInk or any dictation app.

- `watch/`: Ramu Kaka watch app. Tap to talk, tap to stop; streams 16 kHz mono PCM over TCP to the Mac it finds on the Wi-Fi (`_ramumic._tcp`), holding Wi-Fi awake while you talk.
- `mac/`: Watch Mic receiver. Plays the audio into BlackHole 2ch and holds VoiceInk's shortcut (right Option) while you talk. `sh mac/install.sh` sets it up to start at login.

Setup: `brew install --cask blackhole-2ch`, run `mac/install.sh`, allow Watch Mic in Accessibility, pick BlackHole 2ch as VoiceInk's mic, build `watch/` and install it with `adb install`.

Limits: watch and Mac must share a Wi-Fi network; while BlackHole is VoiceInk's mic, the laptop mic isn't used.
