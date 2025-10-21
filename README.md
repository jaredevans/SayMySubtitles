# SayMySubtitles

SayMySubtitles is a macOS application that adds timed audio to your videos based on a `.srt` subtitle file. This is particularly useful for creators who have American Sign Language (ASL) videos and want to make them accessible to a wider audience by adding a spoken audio track that matches the subtitles.

## About The Project

This project was created to bridge the accessibility gap for videos that primarily use sign language. By taking an existing `.srt` subtitle file, the application generates and syncs audio to the video, making it more accessible to the world at large.

## Features

*   **Text-to-Speech:** Converts subtitle text into spoken audio.
*   **Audio Syncing:** Automatically times the generated audio to match the subtitle timestamps.
*   **Video Integration:** Merges the generated audio track with your existing video file.
*   **User-Friendly Interface:** A simple app for macOS users.

## Getting Started

### Prerequisites

*   A Silicon Mac (M1,M2,M3,M4) running a recent version of macOS, Tahoe or Sequoia.

## 🧩 Getting Started

### Installation

1. **Download** the latest version of [SayMySubtitles.dmg](https://github.com/jaredevans/SayMySubtitles/releases/tag/1.0).

2. **Open** the DMG file — you’ll see a **SayMySubtitles** folder and an **/Applications** shortcut.

3. **Drag the `SayMySubtitles` folder** to the **/Applications** folder.  Go to /Applications folder and open the SayMySubtitles folder.
   
4. **Run the `1-Allow-Run.command` script** once.  
   It removes macOS’s quarantine flags and ensures the app and `ffmpeg` binary are executable.  
   - it will be blocked when you try to run it for the first time.
   - Go to **System Settings ▸ Privacy & Security ▸ Open Anyway**. 

5. After that, double-click **`SayMySubtitles.app`**   
   Go again to **System Settings ▸ Privacy & Security ▸ Open Anyway**.  
   This is required because the app is not Apple-signed.

---

### Usage

1. **Open** the `SayMySubtitles` app.

2. **Drag and drop** your video file (`.mp4`, `.mov`, etc.) and your subtitle file (`.srt`) into the window.

3. Choose a **voice** from the dropdown menu.

4. Click **Replace Audio**.  
   The app will generate a timed spoken narration of each subtitle line and automatically replace the
   video’s audio track with that narration.

5. When finished, a new video file will be saved next to your original, named:

   ```
   <original_name>_tts_audio.mp4
   ```
### Notes

The app works entirely offline — all processing is done locally using macOS’s built-in text-to-speech
and a bundled ffmpeg binary.

Your original video and subtitle files are never modified.
