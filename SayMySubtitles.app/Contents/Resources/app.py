#!/usr/bin/env python3
# app.py — Drag & drop .mp4 + .srt → synthesize timed speech and replace video audio
# - Uses /usr/bin/say (fixed 200 WPM) and bundled ffmpeg
# - Adds UTF-8 safe logging, ffmpeg-only audio verification, and adaptive AAC encoders

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
RATE_WPM = 200
DEBUG_KEEP_FILES = False 
DEBUG_DIR = Path.home() / "Desktop" / "SayMySubtitles-Debug"

LOGFILE = str(Path.home() / "Library/Logs/SRTTimedSpeech.log")
def append_log(txt: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {txt}\n")
    except Exception:
        pass

# ---------- helpers ----------

def run(cmd):
    append_log(f"$ {' '.join(cmd)}")
    p = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    )
    if p.returncode != 0:
        details = f"Command failed:\n$ {' '.join(cmd)}\n\nSTDERR:\n{p.stderr}"
        append_log(details)
        raise RuntimeError(details)
    if p.stderr:
        append_log("\n".join(p.stderr.splitlines()[:8]))
    return p

def which_ffmpeg():
    here = Path(__file__).resolve().parent
    bundled = here / "bin" / "ffmpeg"
    return str(bundled) if bundled.exists() else shutil.which("ffmpeg") or "ffmpeg"

def which_say():
    say_path = Path("/usr/bin/say")
    return str(say_path) if say_path.exists() else shutil.which("say") or "say"

FFMPEG = which_ffmpeg()
SAY = which_say()
AudioSegment.converter = FFMPEG
os.environ.setdefault("LC_ALL", "en_US.UTF-8")
os.environ.setdefault("LANG", "en_US.UTF-8")

# ---------- ffmpeg-based verify ----------

def _parse_duration_ms(stderr: str) -> int:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    if not m: return 0
    h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return int(((h*3600)+(mnt*60)+s)*1000)

# REPLACE your verify_audio() with this version
import re

def _parse_duration_ms(stderr: str) -> int:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    if not m:
        return 0
    h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return int(((h*3600) + (mnt*60) + s) * 1000)

def verify_audio(path: str, min_size=2048, min_ms=50):
    pth = Path(path)
    if not pth.exists():
        raise RuntimeError(f"Missing audio file: {path}")
    if pth.stat().st_size < min_size:
        raise RuntimeError(f"Audio file too small: {path} ({pth.stat().st_size} bytes)")

    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", path, "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    )
    stderr = probe.stderr or ""
    # NOTE: case-insensitive now
    has_audio = re.search(r"\baudio:\b", stderr, re.IGNORECASE) is not None
    dur_ms = _parse_duration_ms(stderr)

    # Log a helpful head of stderr for debugging
    append_log("[verify_audio stderr head]\n" + "\n".join(stderr.splitlines()[:20]))

    if not has_audio:
        raise RuntimeError(f"No decodable audio stream found in {path}")
    if dur_ms < min_ms:
        raise RuntimeError(f"Audio duration too short in {path} (dur_ms={dur_ms})")

    append_log(f"✅ verify_audio OK: {path} size={pth.stat().st_size} bytes, dur_ms≈{dur_ms}")


# ---------- audio generation ----------

def compact(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()

def ms(td) -> int:
    return int(td.total_seconds() * 1000)

def mac_say_to_aiff(text: str, out_path: str, voice: str=None):
    def cmd(use_voice):
        c=[SAY,"-o",out_path]
        if use_voice and voice: c+=["-v",voice]
        c+=["-r",str(RATE_WPM),text]; return c
    append_log(f"TTS voice={voice or '(default)'} text={text[:60]!r}")
    run(cmd(True))
    if not Path(out_path).exists() or Path(out_path).stat().st_size<1024:
        append_log("Retrying without -v …"); run(cmd(False))
    if not Path(out_path).exists() or Path(out_path).stat().st_size<1024:
        raise RuntimeError("TTS output empty; check voice packs")
    append_log(f"TTS OK: {out_path}")

# In aiff_to_wav(), REPLACE the run() line with this:
def aiff_to_wav(aiff_path: str, wav_path: str):
    run([FFMPEG, "-y", "-i", aiff_path, "-ar", "48000", "-ac", "2", "-acodec", "pcm_s16le", wav_path])
    verify_audio(wav_path)


def time_stretch_to_duration(in_wav: str, out_wav: str, target_ms: int):
    """
    Stretch/compress to exactly target_ms using a single ffmpeg invocation.
    Uses chained atempo filters (each between 0.5 and 2.0). No in-place writes.
    """
    # Probe current duration (ms)
    try:
        seg = AudioSegment.from_file(in_wav)
        cur_ms = len(seg)
    except Exception:
        cur_ms = 0

    # Edge cases: empty/invalid → synth silence of target length
    if target_ms <= 0 or cur_ms <= 0:
        run([
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{max(target_ms/1000.0, 0.001):.6f}",
            out_wav
        ])
        return

    # Overall speed factor we need
    overall = (target_ms / 1000.0) / (cur_ms / 1000.0)

    # Build a chain of atempo filters, each within [0.5, 2.0].
    # We do it multiplicatively until the remainder is in range, then append final remainder.
    chain = []
    remainder = overall

    # If we need to compress a lot (overall > 2.0), keep adding 2.0 links
    while remainder > 2.0:
        chain.append(2.0)
        remainder /= 2.0

    # If we need to stretch a lot (overall < 0.5), keep adding 0.5 links
    while remainder < 0.5 and remainder > 0:  # guard remainder==0
        chain.append(0.5)
        remainder /= 0.5  # i.e., *2

    # Add the final remainder if it’s not ~1.0
    if not (0.999 <= remainder <= 1.001):
        # Remainder will be within [0.5, 2.0] now
        chain.append(remainder)

    # Compose ffmpeg filter string, e.g. "atempo=2.0,atempo=2.0,atempo=1.33"
    afilters = ",".join(f"atempo={x:.6f}" for x in chain) if chain else "anull"

    # We also clamp with -t to land exactly on target_ms
    run([
        FFMPEG, "-y",
        "-i", in_wav,
        "-af", afilters,
        "-t", f"{target_ms/1000.0:.6f}",
        out_wav
    ])

def build_timed_track_from_srt(srt_path,voice=None,status_cb=None):
    append_log(f"Parsing SRT {srt_path}")
    subs=list(srt.parse(Path(srt_path).read_text(encoding="utf-8",errors="ignore")))
    if not subs: raise ValueError("No subtitles found")
    total_ms=ms(subs[-1].end)+500
    tl=AudioSegment.silent(duration=total_ms,frame_rate=48000).set_channels(2)
    with tempfile.TemporaryDirectory() as td:
        for i,cue in enumerate(subs,1):
            text=compact(cue.content)
            if not text: continue
            if status_cb: status_cb(f"TTS {i}/{len(subs)}")
            aiff=os.path.join(td,f"{i:04d}.aiff")
            wav=os.path.join(td,f"{i:04d}.wav")
            fit=os.path.join(td,f"{i:04d}_fit.wav")
            mac_say_to_aiff(text,aiff,voice)
            aiff_to_wav(aiff,wav)
            time_stretch_to_duration(wav,fit,ms(cue.end-cue.start))
            tl=tl.overlay(AudioSegment.from_wav(fit),position=ms(cue.start))
    append_log(f"Built track {len(tl)} ms")
    return tl

def list_aac_encoders():
    out=run([FFMPEG,"-hide_banner","-encoders"]).stdout
    return [l.strip() for l in out.splitlines() if " aac" in l.lower()]

def replace_video_audio(in_v,in_a,out_v):
    encs=[("aac_at",[]),("aac",[]),("aac",["-strict","-2"])]
    append_log(f"Mux encoders: {encs}")
    for enc,extra in encs:
        try:
            run([FFMPEG,"-y","-i",in_v,"-i",in_a,
                 "-map","0:v:0","-map","1:a:0",
                 "-c:v","copy","-c:a",enc,"-b:a","192k",
                 "-ar","48000","-ac","2",
                 "-movflags","+faststart","-shortest",out_v]+extra)
            probe=subprocess.run(
                [FFMPEG,"-hide_banner","-i",out_v,"-f","null","-"],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding="utf-8",errors="replace")
            if "Audio:" in probe.stderr:
                append_log(f"✅ mux ok {enc} -> {out_v}"); return
            raise RuntimeError("no audio stream")
        except Exception as e:
            append_log(f"Encoder {enc} failed: {e}")
    raise RuntimeError("All encoders failed")

# ---------- UI ----------

class DropView(NSView):
    def initWithOwner_(self,owner):
        self=objc.super(DropView,self).init()
        if self is None:return None
        self.owner=owner
        self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        return self
    def draggingEntered_(self,s): return NSDragOperationCopy
    def performDragOperation_(self,s):
        p=s.draggingPasteboard(); NSURL_cls=objc.lookUpClass("NSURL")
        urls=p.readObjectsForClasses_options_([NSURL_cls],{NSPasteboardURLReadingFileURLsOnlyKey:True})
        self.owner.handleDropped([u.path() for u in (urls or [])]); return True

class App(NSObject):
    def init(self):
        self=objc.super(App,self).init()
        if self is None:return None
        self.video_path=self.srt_path=self.voice=None
        self.voices=self._voices_list(); self._build_ui(); return self
    @python_method
    def _voices_list(self):
        try:
            out=run([SAY,"-v","?"]).stdout.splitlines()
            names=[]
            for l in out:
                token=re.sub(r"[^\w\-]","",l.split()[0])
                if token and token not in names: names.append(token)
            append_log(f"Voices: {names[:10]}")
            return names or ["Samantha","Alex"]
        except Exception as e:
            append_log(f"voices_list fail {e}")
            return ["Samantha","Alex"]
    @python_method
    def _build_ui(self):
        W,H=600,180; scr=NSScreen.mainScreen().frame()
        x=(scr.size.width-W)/2; y=(scr.size.height-H)/2
        self.win=NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(x,y,W,H),15,2,False)
        self.win.setTitle_("SayMySubtitles"); c=self.win.contentView()
        t=NSTextField.alloc().initWithFrame_(NSMakeRect(12,H-36,W-24,22))
        t.setBezeled_(False); t.setEditable_(False); t.setDrawsBackground_(False)
        t.setStringValue_("Drop a .mp4 and .srt, pick a voice, then Replace Audio."); c.addSubview_(t)
        self.drop=DropView.alloc().initWithOwner_(self); self.drop.setFrame_(NSMakeRect(12,56,W-24,88)); c.addSubview_(self.drop)
        self.vidLbl=NSTextField.alloc().initWithFrame_(NSMakeRect(12,38,W-24,16))
        self.vidLbl.setBezeled_(False); self.vidLbl.setEditable_(False); self.vidLbl.setDrawsBackground_(False); self.vidLbl.setStringValue_("Video: —"); c.addSubview_(self.vidLbl)
        self.srtLbl=NSTextField.alloc().initWithFrame_(NSMakeRect(12,22,W-24,16))
        self.srtLbl.setBezeled_(False); self.srtLbl.setEditable_(False); self.srtLbl.setDrawsBackground_(False); self.srtLbl.setStringValue_("Subtitles: —"); c.addSubview_(self.srtLbl)
        vLbl=NSTextField.alloc().initWithFrame_(NSMakeRect(12,4,44,18))
        vLbl.setBezeled_(False); vLbl.setEditable_(False); vLbl.setDrawsBackground_(False); vLbl.setStringValue_("Voice:"); c.addSubview_(vLbl)
        self.voicePop=NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(54,0,160,24),False)
        for v in self.voices:self.voicePop.addItemWithTitle_(v)
        if "Samantha" in self.voices:self.voicePop.selectItemWithTitle_("Samantha")
        c.addSubview_(self.voicePop)
        BW1,BW2,BH,G,M=160,80,24,8,12; qx=W-M-BW2; rx=qx-G-BW1
        self.btnReplace=NSButton.alloc().initWithFrame_(NSMakeRect(rx,0,BW1,BH))
        self.btnReplace.setTitle_("Replace Audio"); self.btnReplace.setTarget_(self); self.btnReplace.setAction_("onReplace:"); c.addSubview_(self.btnReplace)
        self.btnQuit=NSButton.alloc().initWithFrame_(NSMakeRect(qx,0,BW2,BH))
        self.btnQuit.setTitle_("Quit"); self.btnQuit.setTarget_(self); self.btnQuit.setAction_("onQuit:"); c.addSubview_(self.btnQuit)
        append_log("— App launched —")
        append_log(f"FFMPEG={FFMPEG}\nSAY={SAY}")
        self.win.makeKeyAndOrderFront_(None); NSApp.activateIgnoringOtherApps_(True)
    @python_method
    def handleDropped(self,paths):
        for p in paths:
            e=Path(p).suffix.lower()
            if e==".mp4": self.video_path=p; self.vidLbl.setStringValue_(f"Video: {p}")
            elif e==".srt": self.srt_path=p; self.srtLbl.setStringValue_(f"Subtitles: {p}")
    @python_method
    def _read_controls(self): self.voice=self.voicePop.titleOfSelectedItem()
    @typedSelector(b"v@:@")
    def _showAlert_(self,p):
        a=NSAlert.alloc().init(); a.setMessageText_(p.get("title","Error")); a.setInformativeText_(p.get("message","")); a.runModal()
    @typedSelector(b"v@:")
    def _restoreButton(self): self.btnReplace.setTitle_("Replace Audio"); self.btnReplace.setEnabled_(True)
    @typedSelector(b"v@:@")
    def onQuit_(self,s): append_log("Quit pressed"); NSApp.terminate_(None)
    @typedSelector(b"v@:@")
    def onReplace_(self,s):
        if not (self.video_path and self.srt_path):
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:",{"title":"Missing","message":"Drop both .mp4 and .srt"},False); return
        self.btnReplace.setTitle_("Adding Audio…"); self.btnReplace.setEnabled_(False)
        self._read_controls(); threading.Thread(target=self._do_replace,daemon=True).start()
    @python_method
    def _reveal_in_finder(self,p): NSWorkspace.sharedWorkspace().performSelectorOnMainThread_withObject_waitUntilDone_("activateFileViewerSelectingURLs:",[NSURL.fileURLWithPath_(p)],False)
    @python_method
    def _do_replace(self):
        try:
            append_log(f"Start replace v={self.video_path} s={self.srt_path} voice={self.voice}")
            tl=build_timed_track_from_srt(self.srt_path,voice=self.voice,status_cb=append_log)
            if DEBUG_KEEP_FILES: DEBUG_DIR.mkdir(exist_ok=True); temp_wav=str(DEBUG_DIR/"narration.wav")
            else: td=tempfile.TemporaryDirectory(); temp_wav=os.path.join(td.name,"narration.wav")
            tl=tl.set_frame_rate(48000).set_channels(2).set_sample_width(2)
            tl.export(temp_wav,format="wav",parameters=["-acodec","pcm_s16le"])
            verify_audio(temp_wav)
            out_mp4=str(Path(self.video_path).with_name(Path(self.video_path).stem+"_tts_audio.mp4"))
            replace_video_audio(self.video_path,temp_wav,out_mp4)
            append_log(f"✅ Done: {out_mp4}")
            self._reveal_in_finder(out_mp4)
        except Exception as e:
            append_log(f"ERROR: {e}")
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_showAlert:",{"title":"Error","message":str(e)+"\nSee log "+LOGFILE},False)
        finally:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("_restoreButton",None,False)

def main():
    NSApplication.sharedApplication(); app=App.alloc().init(); NSApp.run()

if __name__=="__main__": main()
