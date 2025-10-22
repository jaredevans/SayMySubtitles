#!/usr/bin/env python3
# app.py — Drag & drop .mp4 + .srt → synthesize timed speech and replace video audio
# UI status line updates in real-time (main-thread setStringValue: + displayIfNeeded).
# Robust ffmpeg usage (no in-place edits). Conditional logging when DEBUG_KEEP_FILES=True.

import os, re, shutil, subprocess, tempfile, threading, time
from pathlib import Path

import objc
from objc import python_method
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
RATE_WPM = 200
DEBUG_KEEP_FILES = False
APP_NAME = "SayMySubtitles"
LOGFILE = str(Path.home() / "Library/Logs/SRTTimedSpeech.log")

# ---------- logging & helpers ----------
def _now(): return time.strftime("[%Y-%m-%d %H:%M:%S]")

def append_log(txt: str):
    if not DEBUG_KEEP_FILES:
        return
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"{_now()} {txt}\n")
    except Exception:
        pass

def run(cmd, capture=True):
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True
    )
    if p.returncode != 0:
        details = f"Command failed:\n$ {' '.join(cmd)}\n\nSTDERR:\n{(p.stderr or '')}"
        append_log(details)
        raise RuntimeError(details)
    return p

def which_ffmpeg():
    here = Path(__file__).resolve().parent
    bundled = here / "bin" / "ffmpeg"
    app_bundle = Path(__file__).resolve().parents[2] / "Resources" / "bin" / "ffmpeg"
    for p in (bundled, app_bundle):
        if p.exists(): return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"

def which_say():
    p = Path("/usr/bin/say")
    return str(p) if p.exists() else (shutil.which("say") or "say")

FFMPEG = which_ffmpeg()
SAY = which_say()
AudioSegment.converter = FFMPEG

def compact(text: str) -> str: return re.sub(r"\s+", " ", text).strip()
def ms(td) -> int: return int(td.total_seconds() * 1000)
def percent(i, n): return 0 if n <= 0 else int(round((i / n) * 100.0))

# ---------- audio core ----------
def mac_say_to_aiff(text: str, out_path: str, voice: str = None):
    def build_cmd(use_voice: bool):
        cmd = [SAY, "-o", out_path]
        if use_voice and voice: cmd += ["-v", voice]
        cmd += ["-r", str(RATE_WPM), text]
        return cmd
    try:
        append_log("$ " + " ".join(build_cmd(True)))
        run(build_cmd(True))
    except Exception as e:
        if any(k in str(e) for k in ("Voice", "voice", "Invalid")):
            append_log("Retrying /usr/bin/say without -v …")
            run(build_cmd(False))
        else:
            raise
    if DEBUG_KEEP_FILES:
        try: append_log(f"TTS AIFF OK: {out_path} size={os.path.getsize(out_path)}")
        except Exception: pass

def aiff_to_wav(aiff_path: str, wav_path: str):
    cmd = [FFMPEG, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "2", "-acodec", "pcm_s16le", wav_path]
    append_log("$ " + " ".join(cmd))
    run(cmd)
    if DEBUG_KEEP_FILES:
        verify_audio(wav_path)

def verify_audio(path: str):
    run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"])
    append_log(f"✅ verify_audio OK: {path}")

def atempo_chain_factor(f):
    if f <= 0: return []
    steps, remaining = [], f
    while remaining > 2.0: steps.append(2.0); remaining /= 2.0
    while remaining < 0.5: steps.append(0.5); remaining /= 0.5
    steps.append(remaining)
    return steps

def time_stretch_to_duration(in_wav: str, out_wav: str, target_ms: int):
    try:
        cur_ms = len(AudioSegment.from_file(in_wav))
    except Exception:
        cur_ms = 0
    if target_ms <= 0 or cur_ms <= 0:
        run([FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{max(target_ms/1000.0, 0.001):.6f}", out_wav])
        return
    factor = (target_ms / 1000.0) / (cur_ms / 1000.0)
    steps = atempo_chain_factor(factor)
    with tempfile.TemporaryDirectory() as td:
        src = in_wav
        for i, step in enumerate(steps, start=1):
            mid = os.path.join(td, f"step_{i}.wav") if i < len(steps) else os.path.join(td, "stretched.wav")
            run([FFMPEG, "-y", "-i", src, "-af", f"atempo={step}", mid])
            src = mid
        clamped = os.path.join(td, "clamped.wav")
        run([FFMPEG, "-y", "-i", src, "-t", f"{target_ms/1000.0:.6f}", clamped])
        os.makedirs(os.path.dirname(out_wav), exist_ok=True)
        shutil.copyfile(clamped, out_wav)
    if DEBUG_KEEP_FILES:
        verify_audio(out_wav)

def voices_list():
    try:
        raw = subprocess.check_output([SAY, "-v", "?"], stderr=subprocess.STDOUT)
        txt = raw.decode("utf-8", "ignore")
        names, seen = [], set()
        for line in txt.splitlines():
            line = line.strip()
            if not line: continue
            v = re.split(r"\s{2,}", line)[0].strip()
            if v and v not in seen:
                seen.add(v); names.append(v)
        return names or ["Samantha", "Alex"]
    except Exception as e:
        append_log(f"voices_list() failed: {e}")
        return ["Samantha", "Alex"]

def replace_video_audio(in_video: str, in_audio: str, out_video: str):
    tries = [("aac_at", []), ("aac", []), ("aac", ["-strict", "-2"])]
    append_log(f"Mux encoders: {tries}")
    for enc, extra in tries:
        cmd = [
            FFMPEG, "-y",
            "-i", in_video, "-i", in_audio,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", enc, "-b:a", "192k",
            "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-shortest", out_video
        ]
        # insert extras after inputs
        cmd = cmd[:10] + extra + cmd[10:]
        try:
            run(cmd)
            append_log(f"✅ mux ok {enc} -> {out_video}")
            return
        except Exception as e:
            append_log(f"mux with {enc} failed: {e}")
    raise RuntimeError("Failed to mux audio into video with available AAC encoders.")

def build_timed_track_from_srt(srt_path: str, voice: str = None, status_cb=None) -> AudioSegment:
    if status_cb: status_cb("Parsing subtitles…")
    with open(srt_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        subs = list(srt.parse(f.read()))
    if not subs:
        raise ValueError("No subtitles found in SRT.")
    if status_cb: status_cb(f"Parsed {len(subs)} subtitle(s)")

    total_ms = ms(subs[-1].end) + 500
    timeline = AudioSegment.silent(duration=total_ms, frame_rate=48000).set_channels(2)

    with tempfile.TemporaryDirectory() as td_main:
        for i, cue in enumerate(subs, start=1):
            text = compact(cue.content)
            if not text:
                continue
            p = percent(i, len(subs))
            if status_cb: status_cb(f"Generating speech: {i}/{len(subs)} ({p}%)")
            aiff = os.path.join(td_main, f"{i:04d}.aiff")
            wav  = os.path.join(td_main, f"{i:04d}.wav")
            fit  = os.path.join(td_main, f"{i:04d}_fit.wav")
            mac_say_to_aiff(text, aiff, voice=voice)
            aiff_to_wav(aiff, wav)
            target = ms(cue.end - cue.start)
            time_stretch_to_duration(wav, fit, target)
            start = ms(cue.start)
            seg = AudioSegment.from_wav(fit)
            timeline = timeline.overlay(seg, position=start)

    append_log(f"Built track {len(timeline)} ms")
    return timeline

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
    # --- direct, reliable status updates to the main thread ---
    @python_method
    def set_status(self, text: str):
        try:
            s = text if isinstance(text, str) else str(text)
            # setStringValue: on main thread
            self.statusLbl.performSelectorOnMainThread_withObject_waitUntilDone_(
                "setStringValue:", s, False
            )
            # force a paint on the label and content view
            self.statusLbl.performSelectorOnMainThread_withObject_waitUntilDone_(
                "displayIfNeeded", None, False
            )
            self.win.contentView().performSelectorOnMainThread_withObject_waitUntilDone_(
                "displayIfNeeded", None, False
            )
        except Exception as e:
            append_log(f"set_status error: {e}")

    def init(self):
        self = objc.super(App, self).init()
        if self is None: return None
        self.video_path = None
        self.srt_path = None
        self.voice = None
        self.voices = voices_list()
        self._build_ui()
        append_log("— App launched —")
        append_log(f"FFMPEG={FFMPEG}\nSAY={SAY}")
        return self

    @python_method
    def _reveal_in_finder(self, path: str):
        NSWorkspace.sharedWorkspace().performSelectorOnMainThread_withObject_waitUntilDone_(
            "activateFileViewerSelectingURLs:", [NSURL.fileURLWithPath_(path)], False
        )

    @python_method
    def _build_ui(self):
        W, H = 600, 210
        scr = NSScreen.mainScreen().frame()
        x = (scr.size.width - W) / 2.0
        y = (scr.size.height - H) / 2.0

        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), 15, 2, False
        )
        self.win.setTitle_(APP_NAME)
        c = self.win.contentView()

        info = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-34, W-24, 22))
        info.setBezeled_(False); info.setEditable_(False); info.setDrawsBackground_(False)
        info.setStringValue_("Drop a .mp4 and a .srt. Pick a voice, then Replace Audio. (Rate fixed at 200 WPM)")
        c.addSubview_(info)

        # Status line
        self.statusLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-56, W-24, 18))
        self.statusLbl.setBezeled_(False); self.statusLbl.setEditable_(False); self.statusLbl.setDrawsBackground_(False)
        self.statusLbl.setStringValue_("Idle")
        c.addSubview_(self.statusLbl)

        self.drop = DropView.alloc().initWithOwner_(self)
        self.drop.setFrame_(NSMakeRect(12, 64, W-24, 88))
        c.addSubview_(self.drop)

        self.vidLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 46, W-24, 16))
        self.vidLbl.setBezeled_(False); self.vidLbl.setEditable_(False); self.vidLbl.setDrawsBackground_(False)
        self.vidLbl.setStringValue_("Video: —")
        c.addSubview_(self.vidLbl)

        self.srtLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 30, W-24, 16))
        self.srtLbl.setBezeled_(False); self.srtLbl.setEditable_(False); self.srtLbl.setDrawsBackground_(False)
        self.srtLbl.setStringValue_("Subtitles: —")
        c.addSubview_(self.srtLbl)

        vLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 12, 44, 18))
        vLbl.setBezeled_(False); vLbl.setEditable_(False); vLbl.setDrawsBackground_(False)
        vLbl.setStringValue_("Voice:")
        c.addSubview_(vLbl)

        self.voicePop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(54, 8, 160, 24), False)
        for v in self.voices: self.voicePop.addItemWithTitle_(v)
        if "Samantha" in self.voices:
            self.voicePop.selectItemWithTitle_("Samantha")
        c.addSubview_(self.voicePop)

        BTN_W_REP, BTN_W_QUIT, BTN_H, GAP, M = 160, 80, 24, 8, 12
        quit_x = W - M - BTN_W_QUIT
        rep_x  = quit_x - GAP - BTN_W_REP

        self.btnReplace = NSButton.alloc().initWithFrame_(NSMakeRect(rep_x, 8, BTN_W_REP, BTN_H))
        self.btnReplace.setTitle_("Replace Audio")
        self.btnReplace.setTarget_(self)
        self.btnReplace.setAction_("onReplace:")
        c.addSubview_(self.btnReplace)

        self.btnQuit = NSButton.alloc().initWithFrame_(NSMakeRect(quit_x, 8, BTN_W_QUIT, BTN_H))
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

    # Alerts & button restore
    @objc.signature(b"v@:@")
    def _showAlert_(self, payload):
        title = payload.get("title", "Error")
        message = payload.get("message", "")
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message[:8000])
        alert.setAlertStyle_(NSAlertStyleInformational)
        alert.runModal()

    @objc.signature(b"v@:")
    def _restoreButton(self):
        self.btnReplace.setTitle_("Replace Audio")
        self.btnReplace.setEnabled_(True)

    @objc.signature(b"v@:@")
    def onQuit_(self, sender):
        NSApp.terminate_(None)

    @objc.signature(b"v@:@")
    def onReplace_(self, sender):
        if not (self.video_path and self.srt_path):
            payload = {"title": "Missing Files", "message": "Drop both a .mp4 and a .srt first."}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:", payload, False)
            return

        self.btnReplace.setTitle_("Processing…")
        self.btnReplace.setEnabled_(False)

        # show immediately on main thread
        self.set_status("Parsing subtitles…")

        self._read_controls()
        threading.Thread(target=self._do_replace, daemon=True).start()

    @python_method
    def _do_replace(self):
        try:
            append_log(f"Start replace v={self.video_path} s={self.srt_path} voice={self.voice}")

            timeline = build_timed_track_from_srt(self.srt_path, voice=self.voice, status_cb=self.set_status)

            self.set_status("Exporting narration…")
            if DEBUG_KEEP_FILES:
                debug_dir = Path.home() / "Desktop" / f"{APP_NAME}-Debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                out_wav = str(debug_dir / "narration.wav")
            else:
                td = tempfile.TemporaryDirectory()
                out_wav = os.path.join(td.name, "narration.wav")

            timeline.export(out_wav, format="wav")
            if DEBUG_KEEP_FILES: verify_audio(out_wav)

            self.set_status("Muxing into video…")
            out_mp4 = str(Path(self.video_path).with_name(Path(self.video_path).stem + "_tts_audio.mp4"))
            replace_video_audio(self.video_path, out_wav, out_mp4)

            self.set_status("Done")
            append_log(f"✅ Done: {out_mp4}")
            self._reveal_in_finder(out_mp4)

        except Exception as e:
            msg = str(e)
            append_log("ERROR: " + msg)
            payload = {"title": "Command Error", "message": msg}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:", payload, False)
            self.set_status("Idle")
        finally:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_restoreButton", None, False)

def main():
    NSApplication.sharedApplication()
    app = App.alloc().init()
    NSApp.run()

if __name__ == "__main__":
    main()
