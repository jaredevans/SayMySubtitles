#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/Applications/SayMySubtitles"
APP_BUNDLE="$APP_DIR/SayMySubtitles.app"
EXTERNAL_BIN="$APP_DIR/bin/ffmpeg"
INTERNAL_BIN="$APP_BUNDLE/Contents/Resources/bin/ffmpeg"

echo "➡️  Target: $APP_DIR"
if [[ ! -d "$APP_DIR" ]]; then
  echo "❌ Not found. Please drag the 'SayMySubtitles' folder from the DMG into /Applications first."
  exit 1
fi

echo "🧹 Removing quarantine flags (this can take a moment)…"
# Try without sudo, then with sudo if needed (some /Applications installs require admin)
xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || sudo xattr -dr com.apple.quarantine "$APP_DIR"

echo "🔧 Making sure ffmpeg is executable (both the external copy and the bundled one)…"
chmod +x "$EXTERNAL_BIN" 2>/dev/null || true
chmod +x "$INTERNAL_BIN" 2>/dev/null || true

echo "✅ Done. You can now open SayMySubtitles."
echo "   If macOS still shows a warning the first time, right-click the app and choose 'Open'."
