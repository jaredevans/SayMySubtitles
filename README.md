# SayMySubtitles

SayMySubtitles is a macOS application that adds timed audio to your videos based on a `.srt` subtitle file. This is particularly useful for creators who have American Sign Language (ASL) videos and want to make them accessible to a wider audience by adding a spoken audio track that matches the subtitles.

## About The Project

This project was created to bridge the accessibility gap for videos that primarily use sign language. By taking an existing `.srt` subtitle file, the application generates and syncs audio to the video, making it more accessible to those who are visually impaired or prefer audio narration.

## Features

*   **Text-to-Speech:** Converts subtitle text into spoken audio.
*   **Audio Syncing:** Automatically times the generated audio to match the subtitle timestamps.
*   **Video Integration:** Merges the generated audio track with your existing video file.
*   **User-Friendly Interface:** A simple app for macOS users.

## Getting Started

### Prerequisites

*   A Mac running a recent version of macOS.
*   [ffmpeg](https://ffmpeg.org/): The application uses `ffmpeg` for video and audio processing. While it's bundled with the app, you can also install it manually via [Homebrew](https://brew.sh/):
    ```sh
    brew install ffmpeg
    ```

### Installation

1.  Download the latest version of `SayMySubtitles.app`.
2.  Drag `SayMySubtitles.app` to your `Applications` folder.
3.  Right-click the app and select "Open" to run it for the first time. You may need to grant it permissions in your Mac's security settings.

## Usage

1.  Open the `SayMySubtitles` app.
2.  Select your video file (`.mp4`, `.mov`, etc.).
3.  Select your subtitle file (`.srt`).
4.  Click the "Generate Audio" button.
5.  The app will process the video and save a new version with the added audio track in the same directory as the original video.

## Contributing

Contributions are what make the open-source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.