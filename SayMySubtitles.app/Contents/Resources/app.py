#!/usr/bin/env python3
# app.py — Drag & drop .mp4 + .srt → synthesize timed speech and replace video audio
# Uses /usr/bin/say (rate locked to 200 WPM) and bundled ffmpeg.

import os, re, shutil, subprocess, tempfile, threading, datetime
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
RATE_WPM = 200            # fixed speaking rate
DEBUG_KEEP_FILES = False  # when False, do NOT write to LOGFILE at all
UI_TITLE = "SayMySubtitles"

# ---------- logging & helpers ----------

LOGFILE = str(Path.home() / "Library/Logs/SRTTimedSpeech.log")

def _ts():
    return datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def append_log(txt: str):
    """Write a line to the logfile only when DEBUG_KEEP_FILES=True."""
    if not DEBUG_KEEP_FILES:
        return
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"{_ts()} {txt}\n")
    except Exception:
        pass

def run(cmd, log_cmd=True):
    """
    Run a subprocess command (list of args). Raises RuntimeError on non-zero exit.
    Returns CompletedProcess with .stdout/.stderr as decoded text.
    """
    if log_cmd:
        append_log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def _dec(b):
        try:
            return b.decode("utf-8", errors="strict")
        except Exception:
            try:
                return b.decode("utf-8", errors="replace")
            except Exception:
                return b.decode("latin-1", errors="replace")
    out = _dec(p.stdout)
    err = _dec(p.stderr)
    if p.returncode != 0:
        details = (
            "Command failed:\n"
            f"$ {' '.join(cmd)}\n\nSTDOUT:\n{out}\n\nSTDERR:\n{err}"
        )
        append_log(details)
        p.stdout, p.stderr = out, err
        raise RuntimeError(details)
    p.stdout, p.stderr = out, err
    return p

def which_ffmpeg():
    here = Path(__file__).resolve().parent
    cand1 = here / "bin" / "ffmpeg"
    cand2 = here / "Contents" / "Resources" / "bin" / "ffmpeg"
    if cand1.exists(): return str(cand1)
    if cand2.exists(): return str(cand2)
    return shutil.which("ffmpeg") or "ffmpeg"

def which_say():
    p = Path("/usr/bin/say")
    return str(p) if p.exists() else shutil.which("say") or "say"

FFMPEG = which_ffmpeg()
SAY = which_say()
AudioSegment.converter = FFMPEG  # used by pydub

# ---------- voice discovery (en_US only, Samantha first) ----------

VOICE_LINE_LOCALE_RE = re.compile(r'\b([a-z]{2}_[A-Z]{2})\b')

def _collect_say_voice_dump():
    # try multiple forms; merge stdout+stderr
    outs = []
    cmds = [
        [SAY, "-v", "?"],
        [SAY, "--voice", "?"],
        [SAY, "-v?"],
    ]
    for c in cmds:
        try:
            p = run(c, log_cmd=True)
            outs.append(p.stdout)
            if p.stderr.strip():
                outs.append(p.stderr)
        except Exception as e:
            append_log(f"voice dump attempt failed: {e}")
    return "\n".join(outs).strip()

def parse_say_voice_lines(text: str):
    rows = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"): continue
        pre = line.split('#', 1)[0].rstrip()
        m = VOICE_LINE_LOCALE_RE.search(pre)
        locale = None
        if m:
            locale = m.group(1)
            name = pre[:m.start()].strip()
        else:
            name = pre
            if "(English (US))" in pre:
                locale = "en_US"
            if not locale:
                for tok in pre.split():
                    if VOICE_LINE_LOCALE_RE.fullmatch(tok):
                        locale = tok
                        break
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            rows.append((name, locale, raw))
    # dedupe by name
    seen = set(); dedup = []
    for n,l,r in rows:
        if n in seen: continue
        seen.add(n); dedup.append((n,l,r))
    return dedup

def voices_en_us():
    try:
        dump = _collect_say_voice_dump()
        if not dump:
            raise RuntimeError("empty say -v ? output")
        rows = parse_say_voice_lines(dump)
        en = [n for (n,l,_r) in rows if l == "en_US"]
        en_extra = [n for (n,l,r) in rows if (l is None and "(English (US))" in r)]
        for v in en_extra:
            if v not in en:
                en.append(v)
        # Samantha first, rest sorted
        if "Samantha" in en:
            en = ["Samantha"] + [v for v in en if v != "Samantha"]
        if len(en) > 1:
            en[1:] = sorted(en[1:])
        if not en:
            en = ["Samantha", "Alex"]
        return en
    except Exception as e:
        append_log(f"voices_list() failed: {e}")
        return ["Samantha", "Alex"]

# ---------- audio core ----------

def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def ms(td) -> int:
    return int(td.total_seconds() * 1000)

def mac_say_to_aiff(text: str, out_path: str, voice: str = None):
    """Use macOS 'say' to create AIFF at fixed -r RATE_WPM. Retry without -v if voice missing."""
    def build_cmd(use_voice: bool):
        cmd = [SAY, "-o", out_path]
        if use_voice and voice:
            cmd += ["-v", voice]
        cmd += ["-r", str(RATE_WPM)]
        cmd += [text]
        return cmd
    append_log(f"TTS voice={voice or '(default)'} text='{text[:60]}'")
    try:
        run(build_cmd(use_voice=True))
    except Exception as e:
        msg = str(e)
        if "Voice" in msg or "voice" in msg or "Invalid" in msg:
            append_log("Retrying /usr/bin/say without -v …")
            run(build_cmd(use_voice=False))
        else:
            raise
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    append_log(f"TTS OK: {out_path} ({size} bytes)")

def aiff_to_wav(aiff_path: str, wav_path: str):
    run([FFMPEG, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "2", "-acodec", "pcm_s16le", wav_path], log_cmd=True)

def verify_audio(wav_path: str):
    run([FFMPEG, "-v", "error", "-i", wav_path, "-f", "null", "-"], log_cmd=True)
    append_log("✅ verify_audio OK: %s size=%d bytes" % (wav_path, os.path.getsize(wav_path)))

def time_stretch_to_duration(in_wav: str, out_wav: str, target_ms: int):
    # create silence if needed
    if target_ms <= 0:
        run([FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{max(target_ms/1000.0, 0.001):.6f}", out_wav])
        return
    # measure input duration
    try:
        seg = AudioSegment.from_wav(in_wav)
        cur_ms = len(seg)
    except Exception:
        cur_ms = 0
    if cur_ms <= 0:
        run([FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", f"{target_ms/1000.0:.6f}", out_wav])
        return

    factor = (target_ms / 1000.0) / (cur_ms / 1000.0)

    def stage(infile, outfile, f):
        # split into chained atempo steps within [0.5, 2.0]
        steps = []
        r = f
        while r > 2.0 or r < 0.5:
            if r > 2.0:
                steps.append(2.0); r /= 2.0
            else:
                steps.append(0.5); r /= 0.5
        steps.append(r)
        filt = ",".join(f"atempo={s:.6f}" for s in steps)
        run([FFMPEG, "-y", "-i", infile, "-af", filt, outfile])

    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "st.wav")
        stage(in_wav, tmp, factor)
        # hard trim/pad to exact target
        run([FFMPEG, "-y", "-i", tmp, "-t", f"{target_ms/1000.0:.6f}", out_wav])

def build_timed_track_from_srt(srt_path: str, voice: str = None, status_cb=None) -> AudioSegment:
    # --- STATUS: Parsing subtitles… ---
    if status_cb: status_cb("Parsing subtitles…")
    with open(srt_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        subs = list(srt.parse(f.read()))
    if not subs:
        raise ValueError("No subtitles found in SRT.")
    # --- STATUS: Parsed N subtitle(s) ---
    if status_cb: status_cb(f"Parsed {len(subs)} subtitle(s)")
    append_log(f"Parsing SRT: {srt_path}\nSRT cues: {len(subs)}")

    total_ms = ms(subs[-1].end) + 500
    timeline = AudioSegment.silent(duration=total_ms, frame_rate=48000).set_channels(2)

    with tempfile.TemporaryDirectory() as td:
        for i, cue in enumerate(subs, start=1):
            text = compact(cue.content)
            if not text:
                continue
            # --- STATUS: Generating speech i/N (P%) ---
            if status_cb:
                pct = int(round(i * 100.0 / len(subs)))
                status_cb(f"Generating speech: {i}/{len(subs)} ({pct}%)")

            aiff = os.path.join(td, f"{i:04d}.aiff")
            wav  = os.path.join(td, f"{i:04d}.wav")
            fit  = os.path.join(td, f"{i:04d}_fit.wav")

            mac_say_to_aiff(text, aiff, voice=voice)
            aiff_to_wav(aiff, wav)
            verify_audio(wav)

            target = ms(cue.end - cue.start)
            target = max(target, 120)  # minimum audibility
            time_stretch_to_duration(wav, fit, target)
            verify_audio(fit)

            start = ms(cue.start)
            seg = AudioSegment.from_wav(fit)
            timeline = timeline.overlay(seg, position=start)

    return timeline

def pick_mux_encoders():
    try:
        enc = run([FFMPEG, "-hide_banner", "-encoders"]).stdout
        has_aac_at = " aac_at " in enc
        has_aac    = re.search(r'^\s*A\.*\s+aac\s', enc, re.MULTILINE) is not None
        encs = []
        if has_aac_at: encs.append(("aac_at", []))
        if has_aac:    encs.append(("aac", []))
        encs.append(("aac", ["-strict", "-2"]))
        return encs
    except Exception:
        return [("aac_at", []), ("aac", []), ("aac", ["-strict", "-2"])]

def replace_video_audio(in_video: str, in_audio: str, out_video: str):
    encoders = pick_mux_encoders()
    append_log(f"Mux encoders: {encoders}")
    last_err = None
    for enc, extra in encoders:
        try:
            cmd = [
                FFMPEG, "-y",
                "-i", in_video, "-i", in_audio,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", enc, "-b:a", "192k",
                "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart",
                "-shortest", out_video
            ]
            if extra:
                cmd = cmd[:-1] + extra + [cmd[-1]]
            run(cmd)
            append_log(f"✅ mux ok {enc} -> {out_video}")
            return
        except Exception as e:
            last_err = e
            append_log(f"mux with {enc} failed: {e}")
    if last_err:
        raise last_err

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
        self.voices = voices_en_us()
        self._build_ui()
        return self

    # ---- Main-thread UI helpers ----

    @python_method
    def _reveal_in_finder(self, path: str):
        NSWorkspace.sharedWorkspace().performSelectorOnMainThread_withObject_waitUntilDone_(
            "activateFileViewerSelectingURLs:", [NSURL.fileURLWithPath_(path)], False
        )

    # REMOVED: _setStatusOnMain_ trampoline

    @python_method
    def setStatus(self, txt: str):
        """Update status label safely from any thread by calling the label's setter on the main thread."""
        try:
            if txt is None:
                txt = ""
            # Send directly to the NSTextField on the main thread
            self.statusLbl.performSelectorOnMainThread_withObject_waitUntilDone_("setStringValue:", str(txt), False)
        except Exception:
            pass

    # ---- Build UI ----

    @python_method
    def _build_ui(self):
        W, H = 640, 210
        scr = NSScreen.mainScreen().frame()
        x = (scr.size.width - W) / 2.0
        y = (scr.size.height - H) / 2.0

        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), 15, 2, False
        )
        self.win.setTitle_(UI_TITLE)

        c = self.win.contentView()

        # Top status line
        self.statusLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-30, W-24, 22))
        self.statusLbl.setBezeled_(False); self.statusLbl.setEditable_(False); self.statusLbl.setDrawsBackground_(False)
        self.statusLbl.setStringValue_("Idle")
        c.addSubview_(self.statusLbl)

        info = NSTextField.alloc().initWithFrame_(NSMakeRect(12, H-52, W-24, 18))
        info.setBezeled_(False); info.setEditable_(False); info.setDrawsBackground_(False)
        info.setStringValue_("Drop a .mp4 and a .srt. Pick a voice, then Replace Audio. (Rate fixed at 200 WPM)")
        c.addSubview_(info)

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

        vLbl = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 10, 44, 18))
        vLbl.setBezeled_(False); vLbl.setEditable_(False); vLbl.setDrawsBackground_(False)
        vLbl.setStringValue_("Voice:")
        c.addSubview_(vLbl)

        self.voicePop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(54, 6, 200, 24), False)

        # Populate voices (en_US only), prefer Samantha
        for v in self.voices:
            self.voicePop.addItemWithTitle_(v)
        if "Samantha" in self.voices:
            self.voicePop.selectItemWithTitle_("Samantha")
        elif self.voices:
            self.voicePop.selectItemAtIndex_(0)
        c.addSubview_(self.voicePop)

        # Buttons (bottom-right aligned)
        BTN_W_REP, BTN_W_QUIT, BTN_H, GAP, M = 160, 80, 24, 8, 12
        quit_x = W - M - BTN_W_QUIT
        rep_x  = quit_x - GAP - BTN_W_REP

        self.btnReplace = NSButton.alloc().initWithFrame_(NSMakeRect(rep_x, 6, BTN_W_REP, BTN_H))
        self.btnReplace.setTitle_("Replace Audio")
        self.btnReplace.setTarget_(self)
        self.btnReplace.setAction_("onReplace:")
        c.addSubview_(self.btnReplace)

        self.btnQuit = NSButton.alloc().initWithFrame_(NSMakeRect(quit_x, 6, BTN_W_QUIT, BTN_H))
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
        out_mp4 = None
        try:
            append_log("— App launched —")
            append_log(f"FFMPEG={FFMPEG}\nSAY={SAY}")

            # --- STATUS: Parsing subtitles… + per-cue updates inside builder ---
            self.setStatus("Parsing subtitles…")
            timeline = build_timed_track_from_srt(
                self.srt_path,
                voice=self.voice,
                status_cb=self.setStatus
            )

            with tempfile.TemporaryDirectory() as td:
                out_dir = Path.home() / "Desktop" / "SayMySubtitles-Debug"
                if DEBUG_KEEP_FILES:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    narr_path = out_dir / "narration.wav"
                else:
                    narr_path = Path(td) / "narration.wav"

                # --- STATUS: Exporting narration… ---
                self.setStatus("Exporting narration…")
                timeline.export(str(narr_path), format="wav")
                verify_audio(str(narr_path))

                # --- STATUS: Muxing into video… ---
                self.setStatus("Muxing into video…")
                out_mp4 = str(Path(self.video_path).with_name(Path(self.video_path).stem + "_tts_audio.mp4"))
                replace_video_audio(self.video_path, str(narr_path), out_mp4)

            # --- STATUS: Done ---
            self.setStatus("Done")
            if out_mp4:
                self._reveal_in_finder(out_mp4)

        except Exception as e:
            msg = str(e)
            append_log("ERROR: " + msg)
            payload = {"title": "Command Error", "message": msg}
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:", payload, False)
            self.setStatus("Error")
        finally:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_restoreButton", None, False)

# ---------- main ----------

def main():
    NSApplication.sharedApplication()
    app = App.alloc().init()
    NSApp.run()

if __name__ == "__main__":
    main()
