#!/bin/sh
# Builds Watch Mic, puts it in ~/Applications and starts it (plus its Bonjour announcement) at login.
# Then: grant Accessibility to Watch Mic, pick "BlackHole 2ch" as VoiceInk's microphone (brew install --cask blackhole-2ch).
set -e
cd "$(dirname "$0")"
sh build.sh
mkdir -p ~/Applications
rm -rf ~/Applications/"Watch Mic.app"
cp -R "Watch Mic.app" ~/Applications/
agent() { # label, then program arguments
  label=$1; shift
  args=""; for a in "$@"; do args="$args<string>$a</string>"; done
  cat > ~/Library/LaunchAgents/$label.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array>$args</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>$HOME/Library/Logs/$label.log</string>
</dict></plist>
EOF
  launchctl bootout gui/$(id -u)/$label 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$label.plist
}
agent com.topc.watch-mic "$HOME/Applications/Watch Mic.app/Contents/MacOS/watch-mic"
agent com.topc.watch-mic-beacon /usr/bin/nc -lk 5578
agent com.topc.watch-mic-bonjour /usr/bin/dns-sd -R "Watch Mic" _ramumic._tcp local 5577
echo "Watch Mic installed and running"
