#!/usr/bin/env python3
# app.py — Drag & drop .mp4 + .srt → synthesize timed speech and replace video audio
# Uses /usr/bin/say (rate locked to 200 WPM) and bundled ffmpeg. Safe threading + main-thread alerts.

import os, re, shutil, subprocess, tempfile, threading
from pathlib import Path

import objc
from objc import python_method, typedSelector
from Cocoa import (
    NSApplication, NSApp, NSWindow, NSView, NSButton, NSTextField, NSPopUpButton,
    NSScreen, NSMakeRect, NSDragOperationCopy, NSURL, NSWorkspace
)
from AppKit import (
    NSPasteboardTypeFileURL, NSPasteboardURLReadingFileURLsOnlyKey,
    NSAlert, NSAlertStyleInformational
)
from Foundation import NSObject

import srt
from pydub import AudioSegment

# ---------- config ----------
RATE_WPM = 200  # fixed speaking rate

# ---------- logging & helpers ----------

LOGFILE = str(Path.home() / "Library/Logs/SRTTimedSpeech.log")

def append_log(txt: str):
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(txt + "\n")
    except Exception:
        pass

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        details = (
            "Command failed:\n"
            f"$ {' '.join(cmd)}\n\n"
            f"STDOUT:\n{p.stdout}\n\nSTDERR:\n{p.stderr}"
        )
        append_log(details)
        raise RuntimeError(details)
    return p

def which_ffmpeg():
    here = Path(__file__).resolve().parent
    bundled = here / "bin" / "ffmpeg"
    if bundled.exists():
        return str(bundled)
    return shutil.which("ffmpeg") or "ffmpeg"

def which_say():
    p = Path("/usr/bin/say")
    return str(p) if p.exists() else shutil.which("say") or "say"

FFMPEG = which_ffmpeg()
SAY = which_say()
AudioSegment.converter = FFMPEG

# ---------- audio core ----------

def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def ms(td) -> int:
    return int(td.total_seconds() * 1000)

def mac_say_to_aiff(text: str, out_path: str, voice: str = None):
    """Use macOS 'say' to create AIFF at fixed -r 200. Retry without -v if voice missing."""
    def build_cmd(use_voice: bool):
        cmd = [SAY, "-o", out_path]
        if use_voice and voice:
            cmd += ["-v", voice]
        cmd += ["-r", str(RATE_WPM)]
        cmd += [text]
        return cmd

    try:
        run(build_cmd(use_voice=True))
    except Exception as e:
        msg = str(e)
        if "Voice" in msg or "voice" in msg or "Invalid" in msg:
            append_log("Retrying /usr/bin/say without -v …")
            run(build_cmd(use_voice=False))
        else:
            raise

def aiff_to_wav(aiff_path: str, wav_path: str):
    run([FFMPEG, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "2", wav_path])

def time_stretch_to_duration(in_wav: str, out_wav: str, target_ms: int):
    """Stretch/compress to exactly target_ms using ffmpeg atempo and clamp (no in-place writes)."""
    try:
        seg = AudioSegment.from_file(in_wav)
        cur_ms = len(seg)
    except Exception:
        cur_ms = 0

    if target_ms <= 0 or cur_ms <= 0:
        run([FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{max(target_ms/1000.0, 0.001):.6f}", out_wav])
        return

    factor = (target_ms / 1000.0) / (cur_ms / 1000.0)
    remaining = factor
    tmp_in = in_wav
    with tempfile.TemporaryDirectory() as td:
        stage = 0
        while not (0.5 <= remaining <= 2.0):
            stage += 1
            step = 2.0 if remaining > 2.0 else 0.5
            out_stage = os.path.join(td, f"stage_{stage}.wav")
            run([FFMPEG, "-y", "-i", tmp_in, "-af", f"atempo={step}", out_stage])
            tmp_in = out_stage
            remaining /= step

        stretched = os.path.join(td, "stretched.wav")
        run([FFMPEG, "-y", "-i", tmp_in, "-af", f"atempo={remaining}", stretched])

        clamped = os.path.join(td, "clamped.wav")
        run([FFMPEG, "-y", "-i", stretched, "-t", f"{target_ms/1000.0:.6f}", clamped])

        os.makedirs(os.path.dirname(out_wav), exist_ok=True)
        shutil.copyfile(clamped, out_wav)

def build_timed_track_from_srt(srt_path: str, voice: str = None, status_cb=None) -> AudioSegment:
    with open(srt_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        subs = list(srt.parse(f.read()))
    if not subs:
        raise ValueError("No subtitles found in SRT.")

    total_ms = ms(subs[-1].end) + 500
    timeline = AudioSegment.silent(duration=total_ms, frame_rate=48000).set_channels(2)

    with tempfile.TemporaryDirectory() as td:
        for i, cue in enumerate(subs, start=1):
            text = compact(cue.content)
            if not text:
                continue
            if status_cb:
                status_cb(f"TTS {i}/{len(subs)}…")

            aiff = os.path.join(td, f"cue_{i:05d}.aiff")
            wav  = os.path.join(td, f"cue_{i:05d}.wav")
            fit  = os.path.join(td, f"cue_{i:05d}_fit.wav")

            mac_say_to_aiff(text, aiff, voice=voice)
            aiff_to_wav(aiff, wav)
            target = ms(cue.end - cue.start)
            time_stretch_to_duration(wav, fit, target)

            start = ms(cue.start)
            seg = AudioSegment.from_wav(fit)
            timeline = timeline.overlay(seg, position=start)

    return timeline

def voices_list():
    try:
        out = run(["/usr/bin/say", "-v", "?"]).stdout.splitlines()
        names, seen = [], set()
        for line in out:
            parts = line.strip().split()
            if parts:
                v = parts[0]
                if v not in seen:
                    seen.add(v); names.append(v)
        return names or ["Samantha", "Alex"]
    except Exception:
        return ["Samantha", "Alex"]

def replace_video_audio(in_video: str, in_audio: str, out_video: str):
    run([
        FFMPEG, "-y",
        "-i", in_video, "-i", in_audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_video
    ])

# ---------- UI ----------

class DropView(NSView):
    def initWithOwner_(self, owner):
        self = objc.super(DropView, self).init()
        if self is None: return None
        self.owner = owner
        self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        return self

    def draggingEntered_(self, sender):
        return NSDragOperationCopy

    def performDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        NSURL_cls = objc.lookUpClass("NSURL")
        urls = pboard.readObjectsForClasses_options_(
            [NSURL_cls],
            {NSPasteboardURLReadingFileURLsOnlyKey: True}
        )
        paths = [u.path() for u in (urls or [])]
        self.owner.handleDropped(paths)
        return True

class App(NSObject):
    def init(self):
        self = objc.super(App, self).init()
        if self is None: return None
        self.video_path = None
        self.srt_path = None
        self.voice = None
        self.voices = voices_list()
        self._build_ui()
        return self

    @python_method
    def _reveal_in_finder(self, path: str):
        NSWorkspace.sharedWorkspace().performSelectorOnMainThread_withObject_waitUntilDone_(
            "activateFileViewerSelectingURLs:", [NSURL.fileURLWithPath_(path)], False
        )

    @python_method
    def _build_ui(self):
        W, H = 600, 180  # a little taller to comfortably fit both buttons
        scr = NSScreen.mainScreen().frame()
        x = (scr.size.width - W) / 2.0
        y = (scr.size.height - H) / 2.0

        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), 15, 2, False
        )
        self.win.setTitle_("SayMySubtitles")

        c = self.win.contentView()

        info = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-36, W-24, 22))
        info.setBezeled_(False); info.setEditable_(False); info.setDrawsBackground_(False)
        info.setStringValue_("Drop a .mp4 and a .srt. Pick a voice, then Replace Audio. (Rate fixed at 200 WPM)")
        c.addSubview_(info)

        self.drop = DropView.alloc().initWithOwner_(self)
        self.drop.setFrame_(NSMakeRect(12, 56, W-24, 88))
        c.addSubview_(self.drop)

        self.vidLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 38, W-24, 16))
        self.vidLbl.setBezeled_(False); self.vidLbl.setEditable_(False); self.vidLbl.setDrawsBackground_(False)
        self.vidLbl.setStringValue_("Video: —")
        c.addSubview_(self.vidLbl)

        self.srtLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 22, W-24, 16))
        self.srtLbl.setBezeled_(False); self.srtLbl.setEditable_(False); self.srtLbl.setDrawsBackground_(False)
        self.srtLbl.setStringValue_("Subtitles: —")
        c.addSubview_(self.srtLbl)

        vLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 4, 44, 18))
        vLbl.setBezeled_(False); vLbl.setEditable_(False); vLbl.setDrawsBackground_(False)
        vLbl.setStringValue_("Voice:")
        c.addSubview_(vLbl)

        self.voicePop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(54, 0, 160, 24), False)
        for v in self.voices: self.voicePop.addItemWithTitle_(v)
        if "Samantha" in self.voices:
            self.voicePop.selectItemWithTitle_("Samantha")
        c.addSubview_(self.voicePop)

        # ---- Buttons (bottom-right aligned) ----
        BTN_W_REP, BTN_W_QUIT, BTN_H, GAP, M = 160, 80, 24, 8, 12
        quit_x = W - M - BTN_W_QUIT
        rep_x  = quit_x - GAP - BTN_W_REP

        # Replace Audio button (left of Quit)
        self.btnReplace = NSButton.alloc().initWithFrame_(NSMakeRect(rep_x, 0, BTN_W_REP, BTN_H))
        self.btnReplace.setTitle_("Replace Audio")
        self.btnReplace.setTarget_(self)
        self.btnReplace.setAction_("onReplace:")
        c.addSubview_(self.btnReplace)

        # Quit button (to the right of Replace Audio)
        self.btnQuit = NSButton.alloc().initWithFrame_(NSMakeRect(quit_x, 0, BTN_W_QUIT, BTN_H))
        self.btnQuit.setTitle_("Quit")
        self.btnQuit.setTarget_(self)
        self.btnQuit.setAction_("onQuit:")
        c.addSubview_(self.btnQuit)

        self.win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    @python_method
    def handleDropped(self, paths):
        for p in paths:
            ext = Path(p).suffix.lower()
            if ext == ".mp4":
                self.video_path = p
                self.vidLbl.setStringValue_(f"Video: {p}")
            elif ext == ".srt":
                self.srt_path = p
                self.srtLbl.setStringValue_(f"Subtitles: {p}")

    @python_method
    def _read_controls(self):
        self.voice = self.voicePop.titleOfSelectedItem()

    @typedSelector(b"v@:@")
    def _showAlert_(self, payload):
        title = payload.get("title", "Error")
        message = payload.get("message", "")
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message[:8000])
        alert.setAlertStyle_(NSAlertStyleInformational)
        alert.runModal()

    @typedSelector(b"v@:")
    def _restoreButton(self):
        self.btnReplace.setTitle_("Replace Audio")
        self.btnReplace.setEnabled_(True)

    @typedSelector(b"v@:@")
    def onQuit_(self, sender):
        NSApp.terminate_(None)

    @typedSelector(b"v@:@")
    def onReplace_(self, sender):
        if not (self.video_path and self.srt_path):
            payload = {"title": "Missing Files", "message": "Drop both a .mp4 and a .srt first."}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:", payload, False)
            return

        self.btnReplace.setTitle_("Adding Audio…")
        self.btnReplace.setEnabled_(False)

        self._read_controls()
        threading.Thread(target=self._do_replace, daemon=True).start()

    @python_method
    def _do_replace(self):
        try:
            append_log("Generating timed narration…")
            timeline = build_timed_track_from_srt(self.srt_path, voice=self.voice, status_cb=append_log)
            with tempfile.TemporaryDirectory() as td:
                temp_wav = os.path.join(td, "narration.wav")
                timeline.export(temp_wav, format="wav")
                append_log("Replacing video audio…")
                out_mp4 = str(Path(self.video_path).with_name(Path(self.video_path).stem + "_tts_audio.mp4"))
                replace_video_audio(self.video_path, temp_wav, out_mp4)
            append_log(f"Done: {out_mp4}")
            self._reveal_in_finder(out_mp4)
        except Exception as e:
            msg = str(e)
            append_log("ERROR: " + msg)
            payload = {"title": "Command Error", "message": msg}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:", payload, False)
        finally:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_restoreButton", None, False)

def main():
    NSApplication.sharedApplication()
    app = App.alloc().init()
    NSApp.run()

if __name__ == "__main__":
    main()
