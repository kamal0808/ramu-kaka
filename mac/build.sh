#!/bin/sh
# Builds Watch Mic.app next to this script. Run: sh mac/build.sh && open "mac/Watch Mic.app"
set -e
cd "$(dirname "$0")"
APP="Watch Mic.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O main.swift -o "$APP/Contents/MacOS/watch-mic"
codesign --force --sign - "$APP"
echo "built $APP"
