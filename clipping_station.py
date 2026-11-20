#!/usr/bin/env python3
"""
Clipping Station 2 - clip live streams and videos into Reels / Shorts / TikTok format.

LIVE STREAMS (Twitch, YouTube Live, Kick...): rolling buffer + manual clips + AUTO-CLIP when chat spikes.
VIDEOS: upload a file or paste a link (YouTube, Twitch VOD, Instagram, X, ...) and cut any part.
OUTPUT: 9:16, 4:5, 1:1 or 16:9 with layouts: split (cam + game boxes you draw), game only, cam only, blur fit.

AI: finds the best moments of a video (audio peaks + motion + speech), picks N clips, adds styled captions.
Needs: Python 3.8+, ffmpeg, streamlink (live), yt-dlp (links). Optional: faster-whisper (captions + speech AI),
keyboard (F8 hotkey), pytchat (YouTube chat), ANTHROPIC_API_KEY (AI ranking + titles).
Run:   python clipping_station.py      then open http://localhost:8787
"""
import argparse, collections, glob, importlib.util, json, math, mimetypes, os, random, re
import shutil, socket, ssl, subprocess, sys, threading, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
BUF, CLIPS, MEDIA, PREV = (os.path.join(BASE, d) for d in ("cs_buffer", "Clips", "Videos", "cs_preview"))
SEG, KEEP_SECONDS, HOTKEY = 2, 15 * 60, "f8"
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".flv")
for d in (BUF, CLIPS, MEDIA, PREV):
    os.makedirs(d, exist_ok=True)

lock = threading.Lock()
enc = threading.Semaphore(1)          # one encode at a time so auto-clips never choke the PC
state = {"running": False, "url": "", "message": "Idle", "error": "", "hotkey": False}
FMT = {"9x16": (1080, 1920), "4x5": (1080, 1350), "1x1": (1080, 1080), "16x9": (1920, 1080)}
QUAL = {"std": ("veryfast", "21", "160k"), "high": ("medium", "18", "192k"), "max": ("slow", "15", "256k")}
cfg = {"fmt": "9x16", "lay": "split", "split": 38, "camtop": True, "quality": "high", "raw": False,
       "cam": [70, 66, 28, 32], "game": [24.5, 0, 51, 100],
       "auto": False, "pre": 18, "tot": 52, "mult": 3.0, "minm": 8, "cool": 90,
       "cap": "none", "cappos": "bottom", "wmodel": "base"}
procs, tails, jobs = {"sl": None, "ff": None}, {"sl": [], "ff": []}, []
chat = {"token": 0, "status": "Chat off", "msgs": collections.deque(), "since": 0, "n10": 0,
        "base": 0.0, "last": 0.0, "note": ""}


def num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def box(d, p, default):
    x, y = num(d.get(p + "x"), 0, 95, default[0]), num(d.get(p + "y"), 0, 95, default[1])
    return [x, y, num(d.get(p + "w"), 5, 100 - x, default[2]), num(d.get(p + "h"), 5, 100 - y, default[3])]


def set_cfg(d):
    cfg["fmt"] = d.get("fmt") if d.get("fmt") in FMT else "9x16"
    lay = {"streamer": "cam"}.get(d.get("lay"), d.get("lay"))
    cfg["lay"] = lay if lay in ("split", "game", "cam", "blur") else "split"
    cfg["split"] = int(num(d.get("split"), 20, 60, 38))
    cfg["camtop"] = d.get("camtop") != "bottom"
    cfg["quality"] = d.get("quality") if d.get("quality") in QUAL else "high"
    cfg["cam"], cfg["game"] = box(d, "c", [70, 66, 28, 32]), box(d, "g", [24.5, 0, 51, 100])
    cfg["raw"], cfg["auto"] = bool(d.get("raw")), bool(d.get("auto"))
    cfg["pre"] = int(num(d.get("pre"), 5, 60, 18))
    cfg["tot"] = int(max(cfg["pre"] + 10, num(d.get("tot"), 20, 180, 52)))
    cfg["mult"] = {"high": 2.0, "med": 3.0, "low": 4.5}.get(d.get("sens"), 3.0)
    cfg["minm"], cfg["cool"] = int(num(d.get("minm"), 1, 500, 8)), int(num(d.get("cool"), 20, 900, 90))
    cfg["cap"] = d.get("cap") if d.get("cap") in ("none", "classic", "pop", "neon", "boxed", "minimal") else "none"
    cfg["cappos"] = d.get("cappos") if d.get("cappos") in ("bottom", "center") else "bottom"
    cfg["wmodel"] = d.get("wmodel") if d.get("wmodel") in ("off", "tiny", "base", "small") else "base"


# ------------------------------------------------------------ video layouts
def cover(b, tw, th):
    """Crop the box (percent of the frame), scale it to fill tw x th, trim the overflow. What you draw is what you get."""
    x, y, w, h = [v / 100.0 for v in b]
    return ("crop=w=iw*%.5f:h=ih*%.5f:x=iw*%.5f:y=ih*%.5f,scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
            "crop=%d:%d" % (w, h, x, y, tw, th, tw, th))


def region_sizes(c):
    W, H = FMT[c["fmt"]]
    if c["lay"] != "split":
        return {"cam": (W, H), "game": (W, H)}
    if W >= H:  # landscape: side by side
        cw = int(round(W * c["split"] / 100.0 / 2) * 2)
        return {"cam": (cw, H), "game": (W - cw, H)}
    ch = int(round(H * c["split"] / 100.0 / 2) * 2)
    return {"cam": (W, ch), "game": (W, H - ch)}


def build_vf(c, ass=None):
    W, H = FMT[c["fmt"]]
    lay, rs = c["lay"], region_sizes(c)
    if lay == "game":
        g = cover(c["game"], W, H)
    elif lay == "cam":
        g = cover(c["cam"], W, H)
    elif lay == "blur":
        g = ("split[a][b];[a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,boxblur=25:3[bg];"
             "[b]scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
             % (W, H, W, H, W, H))
    else:
        order = "[a][b]" if c["camtop"] else "[b][a]"
        g = "split=2[c][g];[c]%s[a];[g]%s[b];%s%s" % (cover(c["cam"], *rs["cam"]), cover(c["game"], *rs["game"]),
                                                      order, "hstack" if W >= H else "vstack")
    return g + ",fps=30" + (",ass=" + ass if ass else "") + ",format=yuv420p"


def probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def new_job(label):
    j = {"id": time.time(), "label": label, "status": "waiting", "out": "", "t0": time.time(), "t1": 0}
    with lock:
        jobs.append(j)
        del jobs[:-10]
    return j


def finish(j, status, out="", err=""):
    j["status"], j["out"], j["t1"] = status, out, time.time()
    with lock:
        if err:
            state["error"] = err
        elif out:
            state["message"] = "Saved " + out


def encode(j, src, ss, dur, tag, meta=""):
    c = dict(cfg, cam=list(cfg["cam"]), game=list(cfg["game"]))
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    name = "%s_%s_%s.mp4" % (tag, stamp, c["fmt"])
    out = os.path.join(CLIPS, name)
    pre = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.2f" % ss, "-i", src, "-t", "%.2f" % dur]
    pset, crf, abr = QUAL[c["quality"]]
    ass = None
    with enc:
        j["status"] = "encoding"
        if c["cap"] != "none":
            j["status"] = "captioning"
            ass = write_captions(src, ss, dur, c)
            j["status"] = "encoding"
        cmd = pre + ["-vf", build_vf(c, ass), "-c:v", "libx264", "-preset", pset, "-crf", crf, "-profile:v", "high",
                     "-c:a", "aac", "-b:a", abr, "-movflags", "+faststart", out]
        if c["raw"]:
            subprocess.run(pre + ["-c", "copy", os.path.join(CLIPS, "%s_%s_original.mp4" % (tag, stamp))],
                           capture_output=True)
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=BUF)  # cwd keeps the subtitle path simple
        if ass:
            try:
                os.remove(os.path.join(BUF, ass))
            except OSError:
                pass
    if r.returncode != 0 or not os.path.exists(out):
        return finish(j, "error", err="Encode failed: " + (r.stderr.strip()[-250:] or "unknown ffmpeg error"))
    if meta:
        with open(out[:-4] + ".txt", "w", encoding="utf-8") as fh:
            fh.write(meta)
    finish(j, "done", out=name)


# ------------------------------------------------------------ live buffer
def streamlink_cmd():
    exe = shutil.which("streamlink")
    if exe:
        return [exe]
    return [sys.executable, "-m", "streamlink"] if importlib.util.find_spec("streamlink") else None


def segments():
    return sorted(glob.glob(os.path.join(BUF, "seg_*.ts")))


def buffer_seconds():
    return min(len(segments()) * SEG, KEEP_SECONDS)


def drain(proc, key):
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                tails[key] = (tails[key] + [line])[-6:]
    except Exception:
        pass


def kill_procs():
    for k in ("sl", "ff"):
        p = procs[k]
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        procs[k] = None


def start_buffer(url, quality):
    sl = streamlink_cmd()
    if not sl:
        return "streamlink is not installed. Run: pip install streamlink"
    if not shutil.which("ffmpeg"):
        return "ffmpeg is not installed or not on PATH. See the setup guide."
    if not url.startswith(("http://", "https://")):
        return "Paste a full stream link that starts with https://"
    stop_buffer(quiet=True)
    for f in segments():
        try:
            os.remove(f)
        except OSError:
            pass
    tails["sl"], tails["ff"] = [], []
    try:
        p1 = subprocess.Popen(sl + ["--stdout", "--loglevel", "error", url, quality],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v?",
                               "-map", "0:a?", "-c", "copy", "-f", "segment", "-segment_time", str(SEG),
                               "-segment_format", "mpegts", os.path.join(BUF, "seg_%06d.ts")],
                              stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        p1.stdout.close()
    except Exception as e:
        return "Could not start capture: %s" % e
    procs["sl"], procs["ff"] = p1, p2
    for p, k in ((p1, "sl"), (p2, "ff")):
        threading.Thread(target=drain, args=(p, k), daemon=True).start()
    with lock:
        state.update(running=True, url=url, error="", message="Connecting to stream...")
    threading.Thread(target=monitor, args=(p1, p2), daemon=True).start()
    start_chat(url)
    return ""


def monitor(p1, p2):
    while state["running"] and procs["sl"] is p1:
        if p1.poll() is not None or p2.poll() is not None:
            break
        time.sleep(1)
    if procs["sl"] is not p1:
        return
    time.sleep(0.5)
    problem = " ".join(tails["sl"][-2:]) or " ".join(tails["ff"][-2:])
    kill_procs()
    chat["token"] += 1
    with lock:
        state["running"] = False
        if segments():
            state["message"] = "Stream ended or dropped. The buffer is still clippable until you start again."
        else:
            state["message"], state["error"] = "Could not capture the stream.", problem or "Is the channel live?"


def stop_buffer(quiet=False):
    with lock:
        state["running"] = False
        if not quiet:
            state["message"] = "Stopped"
    chat["token"] += 1
    chat["status"] = "Chat off"
    kill_procs()


def cleaner():
    keep = KEEP_SECONDS // SEG
    while True:
        time.sleep(5)
        if state["running"]:
            for f in segments()[:-keep]:
                try:
                    os.remove(f)
                except OSError:
                    pass


def live_clip(before, after, label):
    """Wait `after` seconds, then cut exactly (before+after) seconds ending now, in the chosen layout."""
    before, after = max(5, int(before)), max(0, int(after))
    if not segments():
        return "Nothing buffered yet. Start the buffer and wait a few seconds."
    j = new_job("%s (%ds before, %ds after)" % (label, before, after))

    def work():
        try:
            if after:
                time.sleep(after)
            total = before + after
            pick = segments()[-(math.ceil(total / SEG) + 3):]
            listing, tmp = [os.path.join(BUF, "tmp_%d.%s" % (j["id"] * 1000, e)) for e in ("txt", "ts")]
            with open(listing, "w") as fh:
                for s in pick:
                    fh.write("file '%s'\n" % s.replace("\\", "/").replace("'", "'\\''"))
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", listing, "-c", "copy", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                return finish(j, "error", err="Could not read the buffer: " + r.stderr.strip()[-200:])
            d = probe(tmp) or len(pick) * SEG
            encode(j, tmp, max(0.0, d - total), min(total, d), "live")
            for f in (listing, tmp):
                try:
                    os.remove(f)
                except OSError:
                    pass
        except Exception as e:
            finish(j, "error", err="Clip failed: %s" % e)
    threading.Thread(target=work, daemon=True).start()
    return ""


# ------------------------------------------------------------ chat spike detection
def parse_chat_target(url):
    try:
        u = urlparse(url)
        host, parts = u.netloc.lower().replace("www.", "").replace("m.", ""), [p for p in u.path.split("/") if p]
        if host == "twitch.tv" and parts:
            return "twitch", parts[0].lower()
        if host == "youtube.com":
            vid = (parse_qs(u.query).get("v") or [""])[0] or (parts[1] if parts[:1] == ["live"] and len(parts) > 1 else "")
            return ("youtube", vid) if vid else (None, "")
        if host == "youtu.be" and parts:
            return "youtube", parts[0]
    except Exception:
        pass
    return None, ""


def start_chat(url):
    chat["token"] += 1
    tok = chat["token"]
    chat["msgs"].clear()
    chat["since"], chat["n10"], chat["base"] = 0, 0, 0.0
    kind, target = parse_chat_target(url)
    if kind == "twitch":
        chat["status"] = "Connecting to #%s chat..." % target
        threading.Thread(target=twitch_chat, args=(target, tok), daemon=True).start()
    elif kind == "youtube":
        chat["status"] = "Connecting to YouTube chat..."
        threading.Thread(target=yt_chat, args=(target, tok), daemon=True).start()
    else:
        chat["status"] = "Chat spike detection supports Twitch links and YouTube watch?v= links only."


def twitch_chat(chan, tok):
    while chat["token"] == tok:
        try:
            raw = socket.create_connection(("irc.chat.twitch.tv", 6697), 10)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
            s.settimeout(30)
            s.sendall(("PASS SCHMOOPIIE\r\nNICK justinfan%d\r\nJOIN #%s\r\n" % (random.randint(10000, 99999), chan)).encode())
            chat["status"] = "Chat connected: #%s" % chan
            chat["since"] = chat["since"] or time.time()
            buf = ""
            while chat["token"] == tok:
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode("utf-8", "replace")
                *lines, buf = buf.split("\r\n")
                for ln in lines:
                    if ln.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in ln:
                        chat["msgs"].append(time.time())
            s.close()
        except Exception as e:
            chat["status"] = "Chat reconnecting (%s)" % e
            time.sleep(5)


def yt_chat(vid, tok):
    try:
        import pytchat
    except ImportError:
        chat["status"] = "YouTube chat needs: pip install pytchat"
        return
    try:
        try:
            c = pytchat.create(video_id=vid, interruptable=False)  # no Ctrl+C handler: we run in a background thread
        except TypeError:
            c = pytchat.create(video_id=vid)
        chat["status"], chat["since"] = "Chat connected (YouTube)", chat["since"] or time.time()
        while chat["token"] == tok and c.is_alive():
            for _ in c.get().sync_items():
                chat["msgs"].append(time.time())
            time.sleep(1)
    except Exception as e:
        chat["status"] = "YouTube chat error: %s" % e


def auto_clip(reason):
    pre, tot = cfg["pre"], cfg["tot"]
    err = live_clip(pre, tot - pre, reason)
    if not err:
        chat["last"], chat["note"] = time.time(), "%s at %s" % (reason, time.strftime("%H:%M:%S"))
    return err


def analyzer():
    while True:
        time.sleep(1)
        now, q = time.time(), chat["msgs"]
        while q and q[0] < now - 140:
            q.popleft()
        n10 = sum(1 for t in q if t > now - 10)
        old = sum(1 for t in q if now - 130 < t <= now - 10)
        chat["n10"], chat["base"] = n10, max(old / 12.0, 2.0)
        ready = chat["since"] and now - chat["since"] >= 70
        if (cfg["auto"] and state["running"] and ready and now - chat["last"] > cfg["cool"]
                and n10 >= max(cfg["minm"], cfg["mult"] * chat["base"])):
            auto_clip("Chat spike (%d msgs/10s vs ~%.0f normal)" % (n10, chat["base"]))


# ------------------------------------------------------------ videos
def list_files(folder, exts):
    fs = [f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith(exts)]
    return [{"name": os.path.basename(f), "mb": round(os.path.getsize(f) / 1048576, 1)}
            for f in sorted(fs, key=os.path.getmtime, reverse=True)]


def download_video(url):
    if not url.startswith(("http://", "https://")):
        return "Paste a full video link that starts with https://"
    exe = [shutil.which("yt-dlp")] if shutil.which("yt-dlp") else (
        [sys.executable, "-m", "yt_dlp"] if importlib.util.find_spec("yt_dlp") else None)
    if not exe:
        return "yt-dlp is not installed. Run: pip install yt-dlp"
    j = new_job("Download " + url[:60])

    def work():
        j["status"] = "downloading"
        r = subprocess.run(exe + ["--no-playlist", "--restrict-filenames", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                                  "--merge-output-format", "mp4", "-o", os.path.join(MEDIA, "%(title).60B-%(id)s.%(ext)s"), url],
                           capture_output=True, text=True)
        if r.returncode != 0:
            finish(j, "error", err="Download failed: " + (r.stderr.strip()[-250:] or "unknown error"))
        else:
            finish(j, "done", out="download complete")
    threading.Thread(target=work, daemon=True).start()
    return ""


def video_clip(name, start, end):
    src = os.path.join(MEDIA, os.path.basename(name))
    if not os.path.isfile(src):
        return "Pick a video first."
    start, end = max(0.0, float(start)), float(end)
    if end <= start + 1:
        return "End time must be after the start time."
    j = new_job("Video clip %s (%ds)" % (os.path.basename(name)[:30], end - start))
    threading.Thread(target=encode, args=(j, src, start, min(end - start, 300), "video"), daemon=True).start()
    return ""


def grab_frame(src, t=0):
    """Save one full frame (1280 px wide) for the box editor: from the live buffer or from a video at time t."""
    if src == "live":
        segs = segments()
        if not segs:
            return "Start a stream buffer first, or pick a video."
        path, ss = segs[-2] if len(segs) > 1 else segs[-1], 0.0
    else:
        path, ss = os.path.join(MEDIA, os.path.basename(src)), max(0.0, float(t or 0))
        if not os.path.isfile(path):
            return "Pick a video first."
    dest = os.path.join(PREV, "frame.jpg")
    for at in ([ss, 0.0] if ss else [0.0]):
        if os.path.exists(dest):
            os.remove(dest)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + (["-ss", "%.2f" % at] if at else []) +
                           ["-i", path, "-frames:v", "1", "-update", "1", "-vf", "scale=1280:-2", "-q:v", "3", dest],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return ""
    return "Could not grab a frame: " + (r.stderr.strip()[-150:] or "unknown error")


def verify():
    """Render the current layout on the loaded frame with the real ffmpeg filters (captions not included)."""
    frame, dest = os.path.join(PREV, "frame.jpg"), os.path.join(PREV, "result.jpg")
    if not os.path.exists(frame):
        return "Load a frame first."
    if os.path.exists(dest):
        os.remove(dest)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30", "-t", "1",
                        "-i", frame, "-frames:v", "1", "-update", "1", "-vf", build_vf(cfg), "-q:v", "2", dest],
                       capture_output=True, text=True)  # looped so multi-input filters (stack/overlay) get frames
    return "" if r.returncode == 0 and os.path.exists(dest) else r.stderr.strip()[-200:]


# ------------------------------------------------------------ AI: speech, captions, best-moment finder
transcripts = {}
suggest = {"file": "", "items": [], "ver": 0}
_wm = {"name": None, "model": None}
AI_MODEL = os.environ.get("CS_AI_MODEL", "claude-sonnet-5-5")
HYPE = ("oh my god", "omg", "no way", "wow", "let's go", "lets go", "insane", "crazy", "clutch", "what the",
        "holy", "help", "run", "wasted", "shoot", "police", "scream", "unbelievable", "amazing", "dude", "bro",
        "win", "won", "lost", "dead", "kill", "yes", "!")
HSTYLES = {  # (audio, motion, speech) weights
    "balanced": (0.40, 0.30, 0.30), "action": (0.40, 0.50, 0.10),
    "talk": (0.20, 0.05, 0.75), "funny": (0.50, 0.10, 0.40)}
CAPS = {  # font, size, primary, outline, outline width, shadow, border style, chunk words, uppercase, highlight (BGR hex)
    "classic": ("Arial", 66, "&H00FFFFFF", "&H00000000", 5, 1, 1, 4, False, None),
    "pop": ("Arial", 92, "&H00FFFFFF", "&H00000000", 8, 2, 1, 3, True, "00E5FF"),
    "neon": ("Arial", 78, "&H00D6E62E", "&H009A4FFF", 6, 3, 1, 3, True, None),
    "boxed": ("Arial", 62, "&H00FFFFFF", "&H99000000", 16, 0, 3, 4, False, None),
    "minimal": ("Arial", 50, "&H00FFFFFF", "&H00000000", 3, 0, 1, 6, False, None)}


def have_whisper():
    return importlib.util.find_spec("faster_whisper") is not None


def has_ass():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        return " ass " in r.stdout
    except Exception:
        return False


def transcribe(src, ss, dur, size):
    """Speech to words with timestamps, in the source video's own timeline."""
    from faster_whisper import WhisperModel
    wav = os.path.join(BUF, "tr_%d.wav" % int(time.time() * 1000))
    cut = ["-ss", "%.2f" % ss, "-t", "%.2f" % dur] if dur else []
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + cut +
                       ["-i", src, "-vn", "-ac", "1", "-ar", "16000", wav], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("could not read audio")
    try:
        if _wm["name"] != size:
            _wm["model"], _wm["name"] = WhisperModel(size, device="cpu", compute_type="int8"), size
        segs, _ = _wm["model"].transcribe(wav, word_timestamps=True, vad_filter=True)
        return [(w.start + ss, w.end + ss, w.word.strip()) for sg in segs for w in (sg.words or []) if w.word.strip()]
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def words_for(src, ss, dur, size):
    """Words inside [ss, ss+dur], times relative to the clip start. Reuses a full-video transcript if we have one."""
    full = transcripts.get((src, os.path.getmtime(src)))
    words = full if full is not None else transcribe(src, ss, dur, size)
    return [(s - ss, e - ss, w) for s, e, w in words if ss <= s < ss + dur]


def ass_time(t):
    t = max(0.0, t)
    return "%d:%02d:%05.2f" % (t // 3600, (t % 3600) // 60, t % 60)


def make_ass(words, c):
    font, size, prim, outl, ow, sh, bs, chunk, upper, hi = CAPS[c["cap"]]
    center = c.get("cappos") == "center"
    W, H = FMT[c["fmt"]]
    k = min(W, H) / 1080.0
    size, ow, mv = int(size * k), max(1, int(ow * k)), int(H * (0.156 if H >= W else 0.09))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\nStyle: Default,%s,%d,%s,%s,%s,&H00000000,-1,0,0,0,100,100,0,0,%d,%d,%d,%d,%d,%d,%d,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            % (W, H, font, size, prim, prim, outl, bs, ow, int(sh * k), 5 if center else 2, int(60 * k), int(60 * k),
               0 if center else mv))

    def clean(w):
        w = re.sub(r"[{}\\\n]", "", w).strip()
        return w.upper() if upper else w
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= chunk or w[0] - cur[-1][1] > 0.8):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    lines = []
    for g in groups:
        if hi:  # one line per word, with the spoken word highlighted
            for i, w in enumerate(g):
                end = g[i + 1][0] if i + 1 < len(g) else w[1] + 0.15
                text = " ".join(("{\\c&H%s&}%s{\\r}" % (hi, clean(x[2])) if k == i else clean(x[2])) for k, x in enumerate(g))
                lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (ass_time(w[0]), ass_time(max(end, w[0] + 0.08)), text))
        else:
            lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (
                ass_time(g[0][0]), ass_time(g[-1][1] + 0.15), " ".join(clean(x[2]) for x in g)))
    return head + "\n".join(lines) + "\n"


def write_captions(src, ss, dur, c):
    def skip(msg):
        with lock:
            state["error"] = "Captions skipped: " + msg
        return None
    if c["wmodel"] == "off" or not have_whisper():
        return skip("install faster-whisper (pip install faster-whisper) and set Speech model to tiny/base/small.")
    if not has_ass():
        return skip("your ffmpeg build has no 'ass' filter (libass). Use a full ffmpeg build.")
    try:
        words = words_for(src, ss, dur, c["wmodel"])
    except Exception as e:
        return skip("speech recognition failed (%s)." % e)
    if not words:
        return skip("no speech found in this clip.")
    name = "cap_%d.ass" % int(time.time() * 1000000)
    with open(os.path.join(BUF, name), "w", encoding="utf-8") as fh:
        fh.write(make_ass(words, c))
    return name


# ---- signals
def smooth(a, k):
    n, pre, h = len(a), [0.0], k // 2
    for x in a:
        pre.append(pre[-1] + x)
    return [(pre[min(n, i + h + 1)] - pre[max(0, i - h)]) / (min(n, i + h + 1) - max(0, i - h)) for i in range(n)]


def norm01(a):
    if not a:
        return a
    s = sorted(a)
    lo, hi = s[int(len(s) * 0.05)], s[min(len(s) - 1, int(len(s) * 0.97))]
    return [0.0] * len(a) if hi - lo < 1e-9 else [max(0.0, min(1.0, (x - lo) / (hi - lo))) for x in a]


def audio_levels(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af",
                        "aresample=16000,asetnsamples=n=16000:p=0,astats=metadata=1:reset=1,"
                        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"],
                       capture_output=True, text=True)
    return [-90.0 if "inf" in m else max(-90.0, float(m)) for m in re.findall(r"RMS_level=(\S+)", r.stdout)]


def motion_levels(path, n):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
                        "fps=2,scale=160:-2,select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=-",
                        "-f", "null", "-"], capture_output=True, text=True)
    sums, cnt, t = [0.0] * n, [0] * n, None
    for line in r.stdout.splitlines():
        if "pts_time:" in line:
            t = float(line.split("pts_time:")[1].split()[0])
        elif "scene_score=" in line and t is not None and int(t) < n:
            sums[int(t)] += float(line.split("=")[1])
            cnt[int(t)] += 1
    return [s / c if c else 0.0 for s, c in zip(sums, cnt)]


def speech_levels(words, n):
    buckets = [[] for _ in range(n)]
    for s, e, w in words:
        if int(s) < n:
            buckets[int(s)].append(w.lower())
    out = []
    for b in buckets:
        text = " ".join(b)
        out.append(len(b) + 3 * sum(1 for k in HYPE if k in text) + 2 * text.count("!"))
    return out


def pick_windows(score, L, count, gap=5):
    n = len(score)
    L = max(5, min(L, n))
    sm, pre = smooth(score, 3), [0.0]
    for x in sm:
        pre.append(pre[-1] + x)
    cands = sorted(((0.6 * (pre[s + L] - pre[s]) / L + 0.4 * max(sm[s:s + L]), s) for s in range(0, n - L + 1)), reverse=True)
    chosen = []
    for sc, s in cands:
        if all(abs(s - c[1]) >= L + gap for c in chosen):
            chosen.append((sc, s))
            if len(chosen) >= count:
                break
    return chosen, L


def snap(start, end, words, dur, L):
    """Nudge cuts to natural pauses in speech (within 4 s) so clips do not start or end mid-sentence."""
    if not words:
        return start, end
    starts = [w[0] for i, w in enumerate(words) if i == 0 or w[0] - words[i - 1][1] > 0.6]
    ends = [w[1] for i, w in enumerate(words) if i == len(words) - 1 or words[i + 1][0] - w[1] > 0.6]
    s = min((x for x in starts if abs(x - start) <= 4), key=lambda x: abs(x - start), default=start)
    e = min((x for x in ends if abs(x - end) <= 4), key=lambda x: abs(x - end), default=end)
    if e - s < 10 or e - s > L + 8:
        return start, end
    return max(0.0, s), min(float(dur), e)


def fmt_t(t):
    return "%d:%02d" % (int(t) // 60, int(t) % 60)


def llm_rank(items, words, key):
    """Ask Claude to rate each candidate as a short-form clip and write a title + hashtags. Returns {id: dict}."""
    lines = []
    for it in items:
        text = " ".join(w for s, e, w in words if it["start"] <= s < it["end"])[:900]
        lines.append("[%d] %s-%s: %s" % (it["id"], fmt_t(it["start"]), fmt_t(it["end"]), text or "(no speech)"))
    prompt = ("You pick highlights for TikTok / Reels / YouTube Shorts. Below are candidate clips with transcripts. "
              "Rate each 1-10 for short-form potential (strong hook, emotion, humor, story payoff, makes sense on its own) "
              "and write a catchy title (max 70 chars) and 3-5 hashtags. Reply with ONLY a JSON array like "
              '[{"id":0,"rating":7,"title":"...","hashtags":["#a","#b"]}].\n\n' + "\n".join(lines))
    body = json.dumps({"model": AI_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        text = "".join(b.get("text", "") for b in json.load(r)["content"])
    arr = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    return {int(x["id"]): x for x in arr}


def analyze(j, name, o):
    try:
        src = os.path.join(MEDIA, os.path.basename(name))
        dur = probe(src)
        n = int(dur)
        if n < 15:
            raise RuntimeError("The video is too short to analyze.")
        j["status"] = "analyzing audio"
        a = audio_levels(src)
        a = (a + [a[-1] if a else -90.0] * n)[:n]
        a = norm01([x - b for x, b in zip(a, smooth(a, 61))])        # loud compared with its surroundings
        j["status"] = "analyzing motion"
        m = norm01(motion_levels(src, n))
        words, note = [], ""
        if cfg["wmodel"] != "off" and have_whisper():
            j["status"] = "transcribing (slow on long videos)"
            words = transcribe(src, 0, None, cfg["wmodel"])
            transcripts[(src, os.path.getmtime(src))] = words
        else:
            note = " (speech skipped: install faster-whisper and pick a Speech model)"
        wa, wm, ws = HSTYLES.get(o.get("hstyle"), HSTYLES["balanced"])
        sp = norm01(smooth(speech_levels(words, n), 5)) if words else [0.0] * n
        if not words:
            wa, wm, ws = wa / (wa + wm), wm / (wa + wm), 0.0
        j["status"] = "choosing moments"
        score = [wa * x + wm * y + ws * z for x, y, z in zip(a, m, sp)]
        count = int(num(o.get("count"), 1, 20, 5))
        L = int(num(o.get("length"), 10, 180, 45))
        key = (o.get("key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        want_ai = bool(o.get("ai")) and words and key
        if o.get("ai") and not want_ai:
            note += " (AI ranking skipped: needs an API key and a transcript)"
        chosen, L = pick_windows(score, L, min(15, count * 3) if want_ai else count)
        best = max([c[0] for c in chosen] or [1.0])
        items = []
        for i, (sc, s) in enumerate(chosen):
            s2, e2 = snap(float(s), float(s + L), words, dur, L)
            snippet = " ".join(w for st, en, w in words if s2 <= st < e2)
            items.append({"id": i, "start": round(s2, 1), "end": round(e2, 1), "sig": sc / best,
                          "score": round(100 * sc / best), "title": "", "tags": [], "text": snippet[:160]})
        if want_ai:
            j["status"] = "asking Claude to rank"
            try:
                rank = llm_rank(items, words, key)
                for it in items:
                    r = rank.get(it["id"], {})
                    it["title"], it["tags"] = str(r.get("title", ""))[:90], list(r.get("hashtags", []))[:6]
                    it["score"] = round(50 * it["sig"] + 5 * float(r.get("rating", 5)))
                items.sort(key=lambda x: -x["score"])
                items = items[:count]
            except Exception as e:
                note += " (AI ranking failed: %s)" % str(e)[:80]
                items = sorted(items, key=lambda x: -x["sig"])[:count]
        if o.get("order") == "time":
            items.sort(key=lambda x: x["start"])
        for k, it in enumerate(items):
            it["id"] = k
            it["title"] = it["title"] or "Moment %d" % (k + 1)
        suggest.update(file=os.path.basename(name), items=items, ver=suggest["ver"] + 1)
        finish(j, "done", out="")
        with lock:
            state["message"] = "Found %d moments%s" % (len(items), note)
    except Exception as e:
        finish(j, "error", err="Analysis failed: %s" % e)


def start_analyze(o):
    name = str(o.get("file", ""))
    if not os.path.isfile(os.path.join(MEDIA, os.path.basename(name))):
        return "Pick a video first."
    j = new_job("AI analysis: " + name[:35])
    threading.Thread(target=analyze, args=(j, name, o), daemon=True).start()
    return ""


def render_suggestions(ids):
    src = os.path.join(MEDIA, os.path.basename(suggest["file"]))
    todo = [it for it in suggest["items"] if ids == "all" or it["id"] in ids]
    if not todo or not os.path.isfile(src):
        return "Nothing to render. Run the AI finder first."
    queued = [(it, new_job("AI clip %d (%s-%s)" % (it["id"] + 1, fmt_t(it["start"]), fmt_t(it["end"])))) for it in todo]

    def work():
        for it, j in queued:
            meta = "\n".join(x for x in (it["title"], " ".join(it["tags"])) if x)
            encode(j, src, it["start"], it["end"] - it["start"], "ai%02d" % (it["id"] + 1), meta)
    threading.Thread(target=work, daemon=True).start()
    return ""


# ------------------------------------------------------------ web server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            data = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/status":
            with lock:
                s = dict(state)
                s["jobs"] = [dict(j) for j in jobs][::-1]
            s.update(buffer=buffer_seconds(), clips=len(list_files(CLIPS, (".mp4",))), sver=suggest["ver"],
                     whisper=have_whisper(), now=time.time(),
                     media=len(list_files(MEDIA, VIDEO_EXT)),
                     chat={"status": chat["status"], "n10": chat["n10"], "base": round(chat["base"], 1),
                           "ready": bool(chat["since"] and time.time() - chat["since"] >= 70), "note": chat["note"]})
            self.send_json(s)
        elif path == "/api/clips":
            self.send_json(list_files(CLIPS, (".mp4",)))
        elif path == "/api/suggestions":
            self.send_json(suggest)
        elif path == "/api/media":
            self.send_json(list_files(MEDIA, VIDEO_EXT))
        elif path.startswith(("/clips/", "/media/", "/preview/")):
            folder = {"clips": CLIPS, "media": MEDIA, "preview": PREV}[path.split("/")[1]]
            self.serve_file(os.path.join(folder, os.path.basename(unquote(path))))
        else:
            self.send_json({"error": "not found"}, 404)

    def serve_file(self, fp):
        if not os.path.isfile(fp):
            return self.send_json({"error": "not found"}, 404)
        size = os.path.getsize(fp)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                start, end, code = int(a) if a else 0, min(int(b) if b else size - 1, size - 1), 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(fp)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if u.path == "/api/upload":
            return self.upload(unquote((parse_qs(u.query).get("name") or [""])[0]), n)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            b = {}
        p, err = u.path, ""
        try:
            if p == "/api/settings":
                set_cfg(b)
            elif p == "/api/start":
                err = start_buffer(str(b.get("url", "")).strip(), str(b.get("quality", "best")))
            elif p == "/api/stop":
                stop_buffer()
            elif p == "/api/clip":
                err = live_clip(float(b.get("before", 30)), float(b.get("after", 0)), "Manual clip")
            elif p == "/api/auto_now":
                err = auto_clip("Test auto-clip")
            elif p == "/api/download":
                err = download_video(str(b.get("url", "")).strip())
            elif p == "/api/vclip":
                err = video_clip(str(b.get("file", "")), b.get("start", 0), b.get("end", 0))
            elif p == "/api/analyze":
                err = start_analyze(b)
            elif p == "/api/render":
                ids = "all" if b.get("all") else [int(i) for i in b.get("ids", [])]
                err = render_suggestions(ids)
            elif p == "/api/frame":
                err = grab_frame(str(b.get("src", "live")), b.get("t", 0))
            elif p == "/api/verify":
                err = verify()
            elif p == "/api/delete":
                folder = MEDIA if b.get("kind") == "media" else CLIPS
                fp = os.path.join(folder, os.path.basename(str(b.get("name", ""))))
                if os.path.isfile(fp):
                    os.remove(fp)
            else:
                return self.send_json({"error": "not found"}, 404)
        except (TypeError, ValueError):
            err = "Please enter valid numbers."
        if err:
            with lock:
                state["error"] = err
        self.send_json({"error": err})

    def upload(self, name, n):
        name = os.path.basename(name)
        if not name.lower().endswith(VIDEO_EXT):
            self.rfile.read(n)
            return self.send_json({"error": "Unsupported file type."})
        safe = re.sub(r"[^\w.\- ()]", "_", name)
        with open(os.path.join(MEDIA, safe), "wb") as fh:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        self.send_json({"error": "", "name": safe})


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clipping Station</title>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0d0919;--bg2:#120d22;--panel:#1a1330;--panel2:#241b40;--line:#30265a;--text:#f3ecf7;--muted:#a99dc6;--pink:#ff4f9a;--teal:#2ee6d6;--warn:#ffb347;--ok:#4ade80;
--display:'Unbounded','Arial Black',system-ui,sans-serif;--body:'Figtree',system-ui,-apple-system,'Segoe UI',sans-serif;color-scheme:dark}
@media(prefers-color-scheme:light){:root{--bg:#f4edf8;--bg2:#fbf7fd;--panel:#ffffff;--panel2:#f1e8f7;--line:#ddd0ea;--text:#26183a;--muted:#6c5d85;--pink:#d6277a;--teal:#0a8f86;--warn:#b86a00;--ok:#15803d;color-scheme:light}}
*{box-sizing:border-box}html,body{margin:0}[hidden]{display:none!important}
body{background:var(--bg);color:var(--text);font:15px/1.55 var(--body)}
:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
.app{display:grid;grid-template-columns:232px minmax(0,1fr) 330px;min-height:100vh}
.side{position:sticky;top:0;height:100vh;padding:22px 14px;border-right:1px solid var(--line);background:var(--bg2);display:flex;flex-direction:column;gap:6px}
.brand{font:700 19px/1.15 var(--display);letter-spacing:-.02em;padding:2px 10px 18px;background:linear-gradient(90deg,var(--pink),var(--warn) 55%,var(--teal));-webkit-background-clip:text;background-clip:text;color:transparent}
.nav{display:flex;flex-direction:column;gap:4px}
.nav button{display:flex;gap:12px;align-items:center;width:100%;text-align:left;background:transparent;color:var(--muted);padding:11px 12px;border-radius:12px;font-weight:600}
.nav button svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
.nav button:hover{background:var(--panel)}
.nav button.on{background:var(--panel2);color:var(--text);box-shadow:inset 3px 0 0 var(--pink)}
.sidefoot{margin-top:auto;font-size:12px;color:var(--muted);padding:10px}
main{padding:24px 28px 60px;min-width:0}
.top{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.top h1{font:700 24px/1.2 var(--display);margin:0;letter-spacing:-.01em}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;gap:7px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:99px;padding:5px 12px;font-size:13px;color:var(--muted)}
.pill i{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}.pill.on i{background:var(--ok)}.pill.hot i{background:var(--pink)}.pill.bad i{background:var(--warn)}
.rail{border-left:1px solid var(--line);padding:24px 16px;position:sticky;top:0;height:100vh;overflow:auto;background:var(--bg2)}
@media(max-width:1180px){.app{grid-template-columns:210px minmax(0,1fr)}.rail{grid-column:1/-1;position:static;height:auto;border-left:0;border-top:1px solid var(--line)}}
@media(max-width:820px){.app{display:block}.side{position:static;height:auto;padding:12px}.nav{flex-direction:row;flex-wrap:wrap}.nav button{width:auto}.sidefoot{display:none}main{padding:16px}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:18px}
.card h2{font:600 14px var(--display);margin:0 0 4px;letter-spacing:-.01em}
.card .lead{color:var(--muted);margin:0 0 14px;font-size:14px;max-width:70ch}
.cols{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:18px;align-items:start}
.cols2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;align-items:start}
@media(max-width:1000px){.cols,.cols2{grid-template-columns:1fr}}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.row+.row{margin-top:12px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.grow{flex:1 1 240px;min-width:0}
label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--muted);font-weight:600}
label.ck{flex-direction:row;align-items:center;gap:8px;font-size:14px;color:var(--text);font-weight:500}
input[type=text],input[type=number],input[type=password],select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 11px;font:inherit;max-width:100%;width:100%}
input[type=range]{width:100%;accent-color:var(--pink)}input[type=checkbox]{accent-color:var(--pink);width:16px;height:16px}
input[type=file]{font-size:13px;color:var(--muted)}
button{font:600 14px var(--body);border:0;border-radius:10px;padding:10px 16px;cursor:pointer;background:var(--panel2);color:var(--text);transition:filter .15s,transform .05s}
button:hover:not(:disabled){filter:brightness(1.18)}button:active:not(:disabled){transform:translateY(1px)}button:disabled{opacity:.4;cursor:not-allowed}
.btn-p{background:linear-gradient(135deg,var(--pink),#ff7a59);color:#fff}.btn-t{background:var(--teal);color:#04211e}.btn-g{background:transparent;border:1px solid var(--line)}
.seg{display:flex;gap:6px;flex-wrap:wrap}.seg button{flex:1 1 auto;background:var(--bg);border:1px solid var(--line);padding:9px 10px;text-align:center;line-height:1.25}
.seg button small{display:block;font-weight:500;font-size:11px;color:var(--muted)}.seg button.on{border-color:var(--teal);background:var(--panel2);box-shadow:0 0 0 2px #2ee6d633}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:10px}
.tile{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px 6px 9px;text-align:center}
.tile.on{border-color:var(--pink);box-shadow:0 0 0 2px #ff4f9a44}
.tile b{display:block;font:800 16px Arial,sans-serif;line-height:1.3}.tile small{display:block;color:var(--muted);font-size:11px;margin-top:5px;font-weight:500}
.ct-none b{color:var(--muted);font-weight:600}
.ct-pop b{color:#fff;text-transform:uppercase;-webkit-text-stroke:1px #000;text-shadow:0 2px 0 #000}.ct-pop span{color:#ffe500}
.ct-classic b{color:#fff;-webkit-text-stroke:1px #000;text-shadow:0 1px 0 #000}
.ct-neon b{color:#2ee6d6;text-transform:uppercase;text-shadow:0 0 9px #ff4f9a,0 0 2px #ff4f9a}
.ct-boxed b{display:inline-block;background:#000c;color:#fff;padding:1px 8px;border-radius:4px}
.ct-minimal b{color:#fff;font-weight:600;font-size:13px;text-shadow:0 1px 3px #000}
.ed{position:relative;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden;user-select:none;touch-action:none}
.ed img{position:absolute;inset:0;width:100%;height:100%}
.ed .ph{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:#cbbfe3;padding:24px}
.ed .ph b{display:block;font:600 16px var(--display);color:#fff;margin-bottom:6px}
.bx{position:absolute;border:2px solid var(--c);background:color-mix(in srgb,var(--c) 13%,transparent);cursor:move}
.bx>em{position:absolute;top:0;left:0;font:700 11px var(--body);font-style:normal;background:var(--c);color:#000;padding:1px 7px;border-radius:0 0 6px 0}
.bx .h{position:absolute;width:14px;height:14px;background:var(--c);border:2px solid #000;border-radius:50%}
.h[data-m=nw]{left:-8px;top:-8px;cursor:nwse-resize}.h[data-m=ne]{right:-8px;top:-8px;cursor:nesw-resize}
.h[data-m=sw]{left:-8px;bottom:-8px;cursor:nesw-resize}.h[data-m=se]{right:-8px;bottom:-8px;cursor:nwse-resize}
.legend{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:10px;font-size:13px;color:var(--muted)}
.dot{width:10px;height:10px;border-radius:3px;display:inline-block;margin-right:6px;vertical-align:-1px}
.phone{margin:0 auto;max-width:300px;padding:10px;border-radius:34px;background:linear-gradient(160deg,#2a2150,#120d22);border:1px solid var(--line);box-shadow:0 18px 50px #0007}
.phone canvas{display:block;width:100%;height:auto;border-radius:24px;background:#000}
.phone.wide{max-width:100%;border-radius:18px}.phone.wide canvas{border-radius:10px}
.vres{margin-top:12px;text-align:center}.vres img{max-width:100%;max-height:380px;border-radius:12px;border:1px solid var(--line)}
.meter{height:8px;border-radius:99px;background:var(--panel2);overflow:hidden;margin:12px 0 6px}
.meter div{height:100%;width:0;background:linear-gradient(90deg,var(--pink),var(--teal));transition:width .6s linear}
.meta{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:13px;flex-wrap:wrap}
.stage{aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden;display:grid;place-items:center;text-align:center;color:#cbbfe3;padding:16px}
.stage iframe,.stage video{width:100%;height:100%;border:0;background:#000}
.stage b{font:600 15px var(--display);display:block;color:#fff;margin-bottom:6px}
.hint{color:var(--muted);font-size:13px;margin:10px 0 0;max-width:80ch}
.chat{background:var(--bg);border:1px dashed var(--line);border-radius:12px;padding:10px 12px;font-size:13.5px;color:var(--muted)}.chat.spike{border-color:var(--pink);color:var(--text)}
.banner{position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:50;max-width:min(680px,92vw);padding:11px 18px;border-radius:12px;font-weight:600;box-shadow:0 10px 40px #0008;background:var(--warn);color:#1a1100}.banner.ok{background:var(--teal);color:#04211e}
.job{display:flex;justify-content:space-between;gap:8px;padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px}.job:last-child{border:0}
.job span:first-child{min-width:0;overflow-wrap:anywhere}.job span:last-child{white-space:nowrap;color:var(--muted)}
.s-done{color:var(--ok)!important}.s-error{color:var(--warn)!important}.s-encoding,.s-captioning,.s-downloading,.s-waiting{color:var(--pink)!important}
.empty{color:var(--muted);margin:0;font-size:14px}
.lib{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.clip{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:10px}
.clip video{width:100%;max-height:340px;border-radius:10px;background:#000;display:block}
.clip .nm{font-size:12.5px;color:var(--muted);margin:8px 0;word-break:break-all}
.clip .acts{display:flex;gap:8px}.clip a,.clip button{font-size:13px;padding:7px 12px;border-radius:9px;background:var(--panel2);color:var(--text);text-decoration:none;font-weight:600;border:0;cursor:pointer}
.sug{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-top:10px;background:var(--bg)}.sug .hd{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}.sug p{margin:6px 0 0;color:var(--muted);font-size:13px}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-weight:600;font-size:13px}
</style></head><body>
<div class="banner" id="banner" hidden></div>
<div class="app">
<aside class="side">
 <div class="brand">Clipping<br>Station</div>
 <nav class="nav">
  <button data-v="studio"><svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M4 11h16M12 11v10"/></svg>Studio</button>
  <button data-v="live"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="1.8"/><path d="M8 8a6 6 0 0 0 0 8M16 8a6 6 0 0 1 0 8M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14"/></svg>Live streams</button>
  <button data-v="videos"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9l5 3-5 3z"/></svg>Videos &amp; AI</button>
  <button data-v="library"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>Library</button>
 </nav>
 <div class="sidefoot" id="sidefoot">Local app. Nothing leaves your PC unless you use Claude ranking.</div>
</aside>

<main>
<div class="top"><h1 id="ttl">Studio</h1>
 <div class="pills"><span class="pill" id="pBuf"><i></i><span>Buffer 0s</span></span><span class="pill" id="pChat"><i></i><span>Chat off</span></span><span class="pill" id="pJobs"><i></i><span>Idle</span></span><span class="pill" id="pAI"><i></i><span>Speech AI</span></span></div></div>

<!-- STUDIO -->
<section class="view" id="v-studio">
 <div class="cols">
  <div class="card">
   <h2>1. Mark your face cam and game</h2>
   <p class="lead">Load a frame, then drag the <b style="color:var(--teal)">cam</b> and <b style="color:var(--pink)">game</b> boxes over the footage you want. Drag a corner to resize. With "Lock shape" on, each box keeps the exact shape of its area in the final video, so what you frame is what you get.</p>
   <div class="row">
    <button class="btn-t" id="fLive">Grab frame from live stream</button>
    <button id="fVid">Grab frame from selected video</button>
    <label style="width:110px">At time (mm:ss)<input type="text" id="ft" placeholder="player time"></label>
   </div>
   <div class="ed" id="ed" style="margin-top:14px"><img id="frame" alt="" hidden><div class="ph" id="ph"><div><b>No frame loaded</b>Start a live buffer or pick a video, then grab a frame.</div></div></div>
   <div class="legend"><span><i class="dot" style="background:var(--teal)"></i>Face cam</span><span><i class="dot" style="background:var(--pink)"></i>Game footage</span>
    <label class="ck"><input type="checkbox" id="lock" checked> Lock shape</label>
    <button class="btn-g" id="fit">Fit boxes to shape</button><button class="btn-g" id="reset">Reset boxes</button></div>
   <p class="hint" id="shape"></p>
   <details><summary>Exact numbers (percent of frame)</summary>
    <div class="fields" style="margin-top:10px">
     <label>Cam left<input type="number" id="cx" min="0" max="95" step="0.1" value="70"></label><label>Cam top<input type="number" id="cy" min="0" max="95" step="0.1" value="66"></label>
     <label>Cam width<input type="number" id="cw" min="5" max="100" step="0.1" value="28"></label><label>Cam height<input type="number" id="ch" min="5" max="100" step="0.1" value="32"></label>
     <label>Game left<input type="number" id="gx" min="0" max="95" step="0.1" value="24.5"></label><label>Game top<input type="number" id="gy" min="0" max="95" step="0.1" value="0"></label>
     <label>Game width<input type="number" id="gw" min="5" max="100" step="0.1" value="51"></label><label>Game height<input type="number" id="gh" min="5" max="100" step="0.1" value="100"></label>
    </div></details>
  </div>

  <div class="card">
   <h2>2. Live preview</h2>
   <p class="lead">Updates instantly as you change anything.</p>
   <div class="phone" id="phone"><canvas id="mock" width="540" height="960"></canvas></div>
   <div class="row" style="justify-content:center;margin-top:14px"><label class="ck"><input type="checkbox" id="safe"> Show platform safe zones</label></div>
   <div class="row" style="justify-content:center"><button class="btn-g" id="verify">Check with real ffmpeg</button></div>
   <div class="vres" id="vres" hidden><img id="vimg" alt="ffmpeg result"><p class="hint">Rendered by ffmpeg with your exact settings (captions are shown only in the live preview).</p></div>
  </div>
 </div>

 <div class="cols2">
  <div class="card">
   <h2>Format and layout</h2>
   <p class="lead">Pick the platform shape and how cam and game are arranged.</p>
   <label>Format</label>
   <div class="seg" data-seg="fmt" style="margin-bottom:14px"><button data-v="9x16">9:16<small>Reels, Shorts, TikTok</small></button><button data-v="4x5">4:5<small>Instagram feed</small></button><button data-v="1x1">1:1<small>Square</small></button><button data-v="16x9">16:9<small>YouTube, landscape</small></button></div>
   <label>Layout</label>
   <div class="seg" data-seg="lay" style="margin-bottom:14px"><button data-v="split">Split<small>Cam + game</small></button><button data-v="game">Game only</button><button data-v="cam">Cam only</button><button data-v="blur">Full frame<small>Blurred fill</small></button></div>
   <div class="fields">
    <label>Cam area size: <span id="splitv">38</span>%<input type="range" id="split" min="20" max="60" value="38"></label>
    <label>Cam position<select id="camtop"><option value="top">Top (or left in 16:9)</option><option value="bottom">Bottom (or right)</option></select></label>
   </div>
  </div>
  <div class="card">
   <h2>Captions</h2>
   <p class="lead">Spoken words appear on screen with word-level timing.</p>
   <div class="tiles" data-seg="cap" style="margin-bottom:14px">
    <button class="tile ct-none" data-v="none"><b>No captions</b><small>Off</small></button>
    <button class="tile ct-pop" data-v="pop"><b>Let's <span>go</span></b><small>Pop</small></button>
    <button class="tile ct-classic" data-v="classic"><b>Let's go</b><small>Classic</small></button>
    <button class="tile ct-neon" data-v="neon"><b>Let's go</b><small>Neon</small></button>
    <button class="tile ct-boxed" data-v="boxed"><b>Let's go</b><small>Boxed</small></button>
    <button class="tile ct-minimal" data-v="minimal"><b>let's go</b><small>Minimal</small></button>
   </div>
   <div class="fields">
    <label>Position<select id="cappos"><option value="bottom">Lower third</option><option value="center">Centre</option></select></label>
    <label>Speech model<select id="wmodel"><option value="tiny">tiny (fastest)</option><option value="base">base (balanced)</option><option value="small">small (most accurate)</option><option value="off">Off</option></select></label>
   </div>
   <p class="hint" id="aihint"></p>
  </div>
 </div>
 <div class="card">
  <h2>Output quality</h2>
  <p class="lead">Higher quality means sharper footage and a bigger, slower export.</p>
  <div class="seg" data-seg="quality"><button data-v="std">Standard<small>Fast, smaller files</small></button><button data-v="high">High<small>Recommended</small></button><button data-v="max">Maximum<small>Slowest, best detail</small></button></div>
  <div class="row"><label class="ck"><input type="checkbox" id="raw"> Also keep an untouched copy of the original footage</label></div>
 </div>
</section>

<!-- LIVE -->
<section class="view" id="v-live" hidden>
 <div class="card">
  <h2>Stream buffer</h2>
  <p class="lead">Paste a live link and keep it buffering. Clips use the layout, format and captions from the Studio.</p>
  <div class="row"><label class="grow">Stream link<input type="text" id="url" placeholder="https://www.twitch.tv/channel  or  https://www.youtube.com/watch?v=..."></label>
   <label style="width:150px">Stream quality<select id="squal"><option value="best">Best</option><option value="720p60,720p,best">720p</option><option value="480p,best">480p</option></select></label>
   <button class="btn-t" id="start">Start buffer</button><button id="stop" disabled>Stop</button></div>
  <div class="stage" id="stage" style="margin-top:14px"><div><b>Watch here, clip here</b>Preview shows for Twitch and YouTube links.</div></div>
  <div class="meter" aria-hidden="true"><div id="fill"></div></div>
  <div class="meta"><span id="buf">Buffer: 0s</span><span id="msg">Idle</span></div>
  <div class="row" style="margin-top:14px"><button class="btn-p qc" data-b="15" disabled>Clip 15s</button><button class="btn-p qc" data-b="30" disabled>Clip 30s</button><button class="btn-p qc" data-b="60" disabled>Clip 60s</button><button class="btn-p qc" data-b="90" disabled>Clip 90s</button></div>
  <div class="row"><label style="width:130px">Seconds before<input type="number" id="cb" min="5" max="600" value="45"></label><label style="width:130px">Seconds after<input type="number" id="ca" min="0" max="120" value="10"></label><button class="btn-p" id="cc" disabled>Clip custom</button></div>
 </div>
 <div class="card">
  <h2>Auto-clip on chat spikes</h2>
  <p class="lead">When chat suddenly gets much busier than normal, a clip is saved automatically.</p>
  <div class="fields">
   <label class="ck" style="align-self:end"><input type="checkbox" id="auto"> Auto-clip enabled</label>
   <label>Start before spike (s)<input type="number" id="pre" min="10" max="30" value="18"></label>
   <label>Total clip length (s)<input type="number" id="tot" min="30" max="90" value="52"></label>
   <label>Sensitivity<select id="sens"><option value="high">High</option><option value="med" selected>Medium</option><option value="low">Low</option></select></label>
   <label>Min msgs / 10s<input type="number" id="minm" min="1" max="500" value="8"></label>
   <label>Cooldown (s)<input type="number" id="cool" min="20" max="900" value="90"></label>
  </div>
  <div class="row"><button id="autonow" disabled>Test auto-clip now</button></div>
  <div class="chat" id="chat" style="margin-top:12px">Chat: off</div>
  <p class="hint">A spike means the last 10 seconds of chat are several times busier than the previous two minutes. Chat needs about 70 seconds to learn what normal looks like. Works with Twitch links and YouTube watch?v= links.</p>
 </div>
</section>

<!-- VIDEOS -->
<section class="view" id="v-videos" hidden>
 <div class="cols">
  <div class="card">
   <h2>Your video</h2>
   <p class="lead">Upload a file or paste a link (YouTube, Twitch VOD, Instagram, X and more).</p>
   <div class="row"><label class="grow">Upload a file<input type="file" id="file" accept="video/*,.mkv,.ts"></label></div>
   <div class="row"><label class="grow">Or video link<input type="text" id="vurl" placeholder="https://..."></label><button class="btn-t" id="dl">Download</button></div>
   <div class="row"><label class="grow">Selected video<select id="media"></select></label><button class="btn-g" id="mdel">Remove</button></div>
   <div class="stage" id="vstage" style="margin-top:14px"><div><b>No video selected</b>Upload a file or download a link first.</div></div>
   <div class="row"><label style="width:110px">Start (mm:ss)<input type="text" id="vs" value="0:00"></label><button class="btn-g" id="setS">Use player time</button>
    <label style="width:110px">End (mm:ss)<input type="text" id="ve" value="0:45"></label><button class="btn-g" id="setE">Use player time</button><button class="btn-p" id="vgo" disabled>Make clip</button></div>
   <p class="hint">Make clip uses the layout, format, quality and captions from the Studio.</p>
  </div>
  <div class="card">
   <h2>AI auto clips</h2>
   <p class="lead">Finds the strongest moments from loudness, motion and speech.</p>
   <div class="fields">
    <label>Clips wanted<input type="number" id="acount" min="1" max="20" value="5"></label>
    <label>Clip length<select id="alen"><option value="20">20 s</option><option value="30">30 s</option><option value="45" selected>45 s</option><option value="60">60 s</option><option value="90">90 s</option></select></label>
    <label>Highlight style<select id="ahs"><option value="balanced">Balanced</option><option value="action">Action / gameplay</option><option value="funny">Funny / reactions</option><option value="talk">Talking / story</option></select></label>
    <label>Order<select id="aorder"><option value="score">Best first</option><option value="time">Timeline</option></select></label>
   </div>
   <div class="row"><label class="ck"><input type="checkbox" id="aai"> Claude ranking, titles and hashtags</label></div>
   <div class="row"><label class="grow">Anthropic API key (optional, never saved)<input type="password" id="akey" autocomplete="off" placeholder="or set ANTHROPIC_API_KEY before starting"></label></div>
   <div class="row"><button class="btn-p" id="afind" disabled>Find best moments</button></div>
   <div id="sugg"></div>
  </div>
 </div>
</section>

<!-- LIBRARY -->
<section class="view" id="v-library" hidden>
 <div class="card"><h2>Your clips</h2><p class="lead">Everything you export is also saved in the Clips folder next to the app.</p>
  <div class="lib" id="list"><p class="empty">No clips yet.</p></div></div>
</section>
</main>

<aside class="rail">
 <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:10px"><h2 style="font:600 14px var(--display);margin:0">Jobs</h2><label class="ck" style="font-size:12.5px"><input type="checkbox" id="jsec" checked> Seconds counter</label></div>
 <div id="jobs"><p class="empty">Nothing running.</p></div>
</aside>
</div>

<input type="hidden" id="fmt" value="9x16"><input type="hidden" id="lay" value="split"><input type="hidden" id="cap" value="none"><input type="hidden" id="quality" value="high">

<script>
const $=id=>document.getElementById(id);
let JOBS=[],skew=0,lastClips=-1,lastMedia=-1,lastEmbed='',lastSver=-1,lastErr='',lastMsg='',IMG=null,st=null,bt=null;
const FMT={'9x16':[1080,1920],'4x5':[1080,1350],'1x1':[1080,1080],'16x9':[1920,1080]};
const DEF={c:[70,66,28,32],g:[24.5,0,51,100]};
const F=['fmt','lay','cap','quality','cx','cy','cw','ch','gx','gy','gw','gh','split','camtop','cappos','wmodel','raw','lock','safe','auto','pre','tot','sens','minm','cool','acount','alen','ahs','aorder','aai'];
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:undefined);return r.json()}
function say(m,kind){if(!m)return;const b=$('banner');b.textContent=m;b.className='banner'+(kind==='ok'?' ok':'');b.hidden=false;clearTimeout(bt);bt=setTimeout(()=>b.hidden=true,kind==='ok'?2600:7000)}
function cfg(){const o={};F.forEach(k=>{const e=$(k);o[k]=e.type==='checkbox'?e.checked:e.value});return o}
function save(){clearTimeout(st);st=setTimeout(()=>{const o=cfg();localStorage.setItem('cs3',JSON.stringify(o));api('/api/settings',o)},250)}
function load(){try{const o=JSON.parse(localStorage.getItem('cs3')||'{}');F.forEach(k=>{if(k in o){const e=$(k);if(e.type==='checkbox')e.checked=o[k];else e.value=o[k]}})}catch(e){}$('splitv').textContent=$('split').value;syncSegs();api('/api/settings',cfg())}
function syncSegs(){document.querySelectorAll('[data-seg]').forEach(g=>g.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.v===$(g.dataset.seg).value)))}
// ---------- navigation
const TITLES={studio:'Studio',live:'Live streams',videos:'Videos & AI',library:'Library'};
function nav(v){document.querySelectorAll('.view').forEach(e=>e.hidden=e.id!=='v-'+v);document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));$('ttl').textContent=TITLES[v];localStorage.setItem('cs3v',v);if(v==='studio')render()}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>nav(b.dataset.v));
// ---------- geometry shared by editor and preview
function sizes(){const [W,H]=FMT[$('fmt').value],sp=+$('split').value;
 if($('lay').value==='split'){if(W>=H){const c=Math.round(W*sp/100/2)*2;return{c:[c,H],g:[W-c,H]}}const c=Math.round(H*sp/100/2)*2;return{c:[W,c],g:[W,H-c]}}
 return{c:[W,H],g:[W,H]}}
const AR=p=>{const s=sizes()[p];return s[0]/s[1]};
const imgAR=()=>IMG?IMG.naturalWidth/IMG.naturalHeight:16/9;
const getB=p=>['x','y','w','h'].map(k=>+$((p==='c'?'c':'g')+k).value);
function setB(p,b){['x','y','w','h'].forEach((k,i)=>$((p==='c'?'c':'g')+k).value=(+b[i]).toFixed(1));place(p)}
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function visible(p){const l=$('lay').value;return l==='split'||(l==='game'&&p==='g')||(l==='cam'&&p==='c')}
function place(p){const e=BX[p],b=getB(p);e.style.cssText+=';left:'+b[0]+'%;top:'+b[1]+'%;width:'+b[2]+'%;height:'+b[3]+'%;display:'+(visible(p)&&IMG?'block':'none')}
function refit(p){let [x,y,w,h]=getB(p);const k=imgAR()/AR(p);const cx=x+w/2,cy=y+h/2;h=w*k;if(h>100){h=100;w=h/k}w=clamp(w,5,100);h=clamp(h,5,100);x=clamp(cx-w/2,0,100-w);y=clamp(cy-h/2,0,100-h);setB(p,[x,y,w,h])}
function shapeText(){const s=sizes(),l=$('lay').value,[W,H]=FMT[$('fmt').value];
 $('shape').textContent=l==='split'?'Output '+W+'×'+H+'  |  Cam area '+s.c.join('×')+'  |  Game area '+s.g.join('×'):l==='blur'?'Full frame on blurred background: boxes are not used.':'Output '+W+'×'+H+'  |  Using the '+(l==='game'?'game':'cam')+' box only';
 $('phone').classList.toggle('wide',W>=H)}
// ---------- box editor
const BX={};
function makeBox(p,color,label){const e=document.createElement('div');e.className='bx';e.style.setProperty('--c',color);e.innerHTML='<em>'+label+'</em>'+['nw','ne','sw','se'].map(m=>'<span class="h" data-m="'+m+'"></span>').join('');
 e.addEventListener('pointerdown',ev=>{const m=ev.target.dataset.m;drag(ev,p,m||'move')});$('ed').appendChild(e);BX[p]=e}
function drag(ev,p,mode){ev.preventDefault();const r=$('ed').getBoundingClientRect(),b0=getB(p),sx=ev.clientX,sy=ev.clientY,lock=$('lock').checked,k=imgAR()/AR(p);
 const move=e=>{const dx=(e.clientX-sx)/r.width*100,dy=(e.clientY-sy)/r.height*100;let b;
  if(mode==='move')b=[clamp(b0[0]+dx,0,100-b0[2]),clamp(b0[1]+dy,0,100-b0[3]),b0[2],b0[3]];
  else{let x1=b0[0],y1=b0[1],x2=b0[0]+b0[2],y2=b0[1]+b0[3];
   if(mode.includes('w'))x1=clamp(x1+dx,0,x2-5);else x2=clamp(x2+dx,x1+5,100);
   if(mode.includes('n'))y1=clamp(y1+dy,0,y2-5);else y2=clamp(y2+dy,y1+5,100);
   let w=x2-x1,h=y2-y1;
   if(lock){h=w*k;const room=mode.includes('n')?y2:100-y1;if(h>room){h=room;w=h/k}
    if(mode.includes('w'))x1=x2-w;else x2=x1+w;if(mode.includes('n'))y1=y2-h;else y2=y1+h}
   b=[x1,y1,x2-x1,y2-y1]}
  setB(p,b);render()};
 const up=()=>{removeEventListener('pointermove',move);removeEventListener('pointerup',up);save()};
 addEventListener('pointermove',move);addEventListener('pointerup',up)}
makeBox('c','#2ee6d6','CAM');makeBox('g','#ff4f9a','GAME');
async function loadFrame(src,t){say('Loading frame...','ok');const r=await api('/api/frame',{src,t});if(r.error)return say(r.error);
 const im=new Image();im.onload=()=>{IMG=im;$('frame').src=im.src;$('frame').hidden=false;$('ph').hidden=true;$('ed').style.aspectRatio=im.naturalWidth+'/'+im.naturalHeight;place('c');place('g');render()};im.src='/preview/frame.jpg?t='+Date.now()}
$('fLive').onclick=()=>loadFrame('live',0);
$('fVid').onclick=()=>{const v=$('media').value;if(!v)return say('Pick a video on the Videos & AI page first.');const pv=$('vp');loadFrame(v,$('ft').value?secs($('ft').value):(pv?pv.currentTime:3))};
$('fit').onclick=()=>{['c','g'].forEach(refit);render();save()};
$('reset').onclick=()=>{setB('c',DEF.c);setB('g',DEF.g);['c','g'].forEach(refit);render();save()};
$('verify').onclick=async()=>{const o=cfg();await api('/api/settings',o);const r=await api('/api/verify',{});if(r.error)return say(r.error);$('vimg').src='/preview/result.jpg?t='+Date.now();$('vres').hidden=false};
// ---------- live preview (same maths as the ffmpeg filters)
function cover(x,b,dx,dy,dw,dh){const iw=IMG.naturalWidth,ih=IMG.naturalHeight,sx=b[0]/100*iw,sy=b[1]/100*ih,sw=b[2]/100*iw,sh=b[3]/100*ih,sc=Math.max(dw/sw,dh/sh),vw=dw/sc,vh=dh/sc;x.drawImage(IMG,sx+(sw-vw)/2,sy+(sh-vh)/2,vw,vh,dx,dy,dw,dh)}
const CAPS={classic:[66,'#fff','#000',5],pop:[92,'#fff','#000',8],neon:[78,'#2ee6d6','#ff4f9a',6],boxed:[62,'#fff','#000',16],minimal:[50,'#fff','#000',3]};
function caption(x,W,H){const s=$('cap').value;if(s==='none')return;const [fs,fc,oc,ow]=CAPS[s],k=Math.min(W,H)/1080,ctr=$('cappos').value==='center',y=ctr?H/2:H-H*(H>=W?0.156:0.09);
 let words=s==='pop'||s==='neon'?["LET'S","GO","THIS"]:s==='minimal'?['lets','go','this','is','insane']:["Let's","go","this","is","insane"];
 x.font='800 '+fs*k+'px Arial,sans-serif';x.textAlign='left';x.textBaseline=ctr?'middle':'alphabetic';x.lineJoin='round';
 const sp=x.measureText(' ').width,ws=words.map(w=>x.measureText(w).width),tot=ws.reduce((a,b)=>a+b,0)+sp*(words.length-1);let px=(W-tot)/2;
 if(s==='boxed'){x.fillStyle='rgba(0,0,0,.62)';const pad=ow*k;x.fillRect(px-pad,y-fs*k-pad*0.4,tot+pad*2,fs*k*1.2+pad*0.6)}
 words.forEach((w,i)=>{if(s!=='boxed'){x.lineWidth=ow*2*k;x.strokeStyle=oc;if(s==='neon'){x.shadowColor=oc;x.shadowBlur=20*k}x.strokeText(w,px,y);x.shadowBlur=0}
  x.fillStyle=(s==='pop'&&i===1)?'#ffe500':fc;x.fillText(w,px,y);px+=ws[i]+sp})}
function render(){const cv=$('mock'),[W,H]=FMT[$('fmt').value];cv.width=Math.round(W/2);cv.height=Math.round(H/2);const x=cv.getContext('2d');x.setTransform(.5,0,0,.5,0,0);x.fillStyle='#000';x.fillRect(0,0,W,H);shapeText();
 if(!IMG){x.fillStyle='#cbbfe3';x.font='600 '+Math.min(W,H)/18+'px Arial';x.textAlign='center';x.fillText('Load a frame to preview',W/2,H/2);return}
 const lay=$('lay').value,sp=+$('split').value,top=$('camtop').value==='top',s=sizes();
 if(lay==='game')cover(x,getB('g'),0,0,W,H);else if(lay==='cam')cover(x,getB('c'),0,0,W,H);
 else if(lay==='blur'){x.save();x.filter='blur(28px)';cover(x,[0,0,100,100],-40,-40,W+80,H+80);x.restore();const q=Math.min(W/IMG.naturalWidth,H/IMG.naturalHeight),w=IMG.naturalWidth*q,h=IMG.naturalHeight*q;x.drawImage(IMG,(W-w)/2,(H-h)/2,w,h)}
 else if(W>=H){const c=s.c[0];cover(x,getB('c'),top?0:W-c,0,c,H);cover(x,getB('g'),top?c:0,0,W-c,H)}
 else{const c=s.c[1];cover(x,getB('c'),0,top?0:H-c,W,c);cover(x,getB('g'),0,top?c:0,W,H-c)}
 caption(x,W,H);
 if($('safe').checked&&H>W){x.fillStyle='rgba(255,60,60,.28)';x.fillRect(0,0,W,H*.12);x.fillRect(0,H*.78,W,H*.22);x.fillStyle='#fff';x.font='600 '+W/30+'px Arial';x.textAlign='center';x.fillText('platform UI covers this area',W/2,H*.06);x.fillText('captions, buttons, description',W/2,H*.9)}}
// ---------- controls wiring
function changed(shape){if(shape&&$('lock').checked){['c','g'].forEach(refit)}else{place('c');place('g')}render();save()}
document.querySelectorAll('[data-seg]').forEach(g=>g.querySelectorAll('button').forEach(b=>b.onclick=()=>{$(g.dataset.seg).value=b.dataset.v;syncSegs();changed(['fmt','lay'].includes(g.dataset.seg))}));
$('split').oninput=()=>{$('splitv').textContent=$('split').value;changed(true)};
['camtop','cappos','wmodel','lock','safe','raw'].forEach(k=>$(k).addEventListener('change',()=>changed(k==='lock'&&false)));
['cx','cy','cw','ch','gx','gy','gw','gh'].forEach(k=>$(k).addEventListener('input',()=>changed(false)));
['auto','pre','tot','sens','minm','cool','acount','alen','ahs','aorder','aai'].forEach(k=>$(k).addEventListener('change',save));
// ---------- live view
function embed(u){try{const x=new URL(u),h=x.hostname.replace(/^(www|m)\./,''),p=x.pathname.split('/').filter(Boolean);
 if(h==='twitch.tv'&&p[0])return 'https://player.twitch.tv/?channel='+encodeURIComponent(p[0])+'&parent='+location.hostname+'&muted=true';
 let id='';if(h==='youtube.com')id=x.searchParams.get('v')||(p[0]==='live'?p[1]:'');if(h==='youtu.be')id=p[0];
 if(id)return 'https://www.youtube.com/embed/'+id+'?autoplay=1&mute=1'}catch(e){}return ''}
function setStage(u){const s=embed(u);if(s===lastEmbed)return;lastEmbed=s;
 $('stage').innerHTML=s?'<iframe allow="autoplay; fullscreen" allowfullscreen src="'+s+'"></iframe>':'<div><b>Buffering without preview</b>Preview works for Twitch and YouTube links. Clipping works for any supported site.</div>'}
async function post(p,b){const r=await api(p,b);if(r.error)say(r.error);poll();return r}
$('start').onclick=async()=>{await api('/api/settings',cfg());const u=$('url').value.trim();const r=await post('/api/start',{url:u,quality:$('squal').value});if(!r.error)setStage(u)};
$('stop').onclick=async()=>{await post('/api/stop',{});lastEmbed='';$('stage').innerHTML='<div><b>Stopped</b>Start the buffer to begin again.</div>'};
document.querySelectorAll('.qc').forEach(b=>b.onclick=async()=>{await api('/api/settings',cfg());post('/api/clip',{before:+b.dataset.b,after:0})});
$('cc').onclick=async()=>{await api('/api/settings',cfg());post('/api/clip',{before:+$('cb').value,after:+$('ca').value})};
$('autonow').onclick=async()=>{await api('/api/settings',cfg());post('/api/auto_now',{})};
document.addEventListener('keydown',e=>{if(e.repeat||/INPUT|SELECT/.test(document.activeElement.tagName))return;if(e.key.toLowerCase()==='c'&&!$('cc').disabled&&!$('v-live').hidden)post('/api/clip',{before:30,after:0})});
// ---------- videos view
const secs=t=>{const p=String(t).trim().split(':').map(Number);return p.some(isNaN)?0:p.reduce((a,x)=>a*60+x,0)};
const fmt=s=>{s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
$('file').onchange=async()=>{const f=$('file').files[0];if(!f)return;say('Uploading '+f.name+'...','ok');const r=await (await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f})).json();if(r.error)say(r.error);else say('Upload complete','ok');lastMedia=-1;await poll(r.name)};
$('dl').onclick=()=>post('/api/download',{url:$('vurl').value.trim()});
$('mdel').onclick=async()=>{if($('media').value){await api('/api/delete',{kind:'media',name:$('media').value});lastMedia=-1;poll()}};
$('media').onchange=showVid;
function showVid(){const n=$('media').value;$('vgo').disabled=!n;$('afind').disabled=!n;$('vstage').innerHTML=n?'<video controls id="vp" src="/media/'+encodeURIComponent(n)+'"></video>':'<div><b>No video selected</b>Upload a file or download a link first.</div>'}
$('setS').onclick=()=>{const v=$('vp');if(v)$('vs').value=fmt(v.currentTime)};
$('setE').onclick=()=>{const v=$('vp');if(v)$('ve').value=fmt(v.currentTime)};
$('vgo').onclick=async()=>{await api('/api/settings',cfg());post('/api/vclip',{file:$('media').value,start:secs($('vs').value),end:secs($('ve').value)})};
$('afind').onclick=async()=>{await api('/api/settings',cfg());post('/api/analyze',{file:$('media').value,count:+$('acount').value,length:+$('alen').value,hstyle:$('ahs').value,order:$('aorder').value,ai:$('aai').checked,key:$('akey').value})};
async function renderSugg(){const d=await api('/api/suggestions');const box=$('sugg');box.innerHTML='';if(!d.items.length)return;
 const h=document.createElement('div');h.className='row';h.style.cssText='justify-content:space-between;align-items:center;margin-top:14px';
 h.innerHTML='<b>'+d.items.length+' moments found</b><button class="btn-p" id="rall">Render all</button>';box.appendChild(h);
 $('rall').onclick=async()=>{await api('/api/settings',cfg());post('/api/render',{all:true})};
 d.items.forEach(it=>{const x=document.createElement('div');x.className='sug';
  x.innerHTML='<div class="hd"><span class="t"></span><span><button class="pl">Play</button> <button class="rd btn-t">Render</button></span></div><p class="sn"></p>';
  x.querySelector('.t').textContent='#'+(it.id+1)+'  '+fmt(it.start)+' - '+fmt(it.end)+'  |  score '+it.score+(it.title?'  |  '+it.title:'')+(it.tags.length?'  '+it.tags.join(' '):'');
  x.querySelector('.sn').textContent=it.text?'"'+it.text+'"':'';
  x.querySelector('.pl').onclick=()=>{const v=$('vp');if(v){v.currentTime=it.start;v.play()}};
  x.querySelector('.rd').onclick=async()=>{await api('/api/settings',cfg());post('/api/render',{ids:[it.id]})};box.appendChild(x)})}
// ---------- jobs, status, library
const el=t=>{t=Math.max(0,Math.floor(t));return t<60?t+'s':Math.floor(t/60)+'m '+String(t%60).padStart(2,'0')+'s'};
function renderJobs(){const box=$('jobs');box.innerHTML=JOBS.length?'':'<p class="empty">Nothing running.</p>';const now=Date.now()/1000+skew,on=$('jsec').checked;
 JOBS.forEach(j=>{const d=document.createElement('div');d.className='job';const a=document.createElement('span'),b=document.createElement('span');
  a.textContent=j.label;b.textContent=j.status+(on?' | '+el((j.t1||now)-j.t0):'');b.className='s-'+j.status.split(' ')[0];d.append(a,b);box.appendChild(d)})}
$('jsec').checked=localStorage.getItem('cs_jsec')!=='0';
$('jsec').onchange=()=>{localStorage.setItem('cs_jsec',$('jsec').checked?'1':'0');renderJobs()};
setInterval(renderJobs,1000);
function pill(id,txt,cls){const e=$(id);e.className='pill '+(cls||'');e.lastChild.textContent=txt}
async function renderMedia(sel){const m=await api('/api/media');const cur=sel||$('media').value;
 $('media').innerHTML=m.length?'':'<option value="">(no videos yet)</option>';
 m.forEach(k=>{const o=document.createElement('option');o.value=k.name;o.textContent=k.name+' ('+k.mb+' MB)';$('media').appendChild(o)});
 if(cur&&m.some(k=>k.name===cur))$('media').value=cur;if(!$('vp')||$('vp').getAttribute('src')!=='/media/'+encodeURIComponent($('media').value))showVid()}
async function poll(sel){let s;try{s=await api('/api/status')}catch(e){pill('pJobs','Disconnected','bad');return}
 const can=s.buffer>=4;$('start').disabled=s.running;$('stop').disabled=!s.running;
 document.querySelectorAll('.qc,#cc,#autonow').forEach(b=>b.disabled=!can);
 $('fill').style.width=Math.min(100,s.buffer/900*100)+'%';
 const bt_=Math.floor(s.buffer/60)+'m '+(s.buffer%60)+'s';$('buf').textContent='Buffer: '+bt_;$('msg').textContent=s.message+(s.hotkey?'  (F8 = clip 30s anywhere)':'');
 const c=s.chat,spk=c.ready&&c.n10>=Math.max(+$('minm').value,3*c.base),conn=c.status.startsWith('Chat connected')||c.status.includes('YouTube)');
 $('chat').className='chat'+(spk?' spike':'');$('chat').textContent=c.status+(conn?(' | last 10s: '+c.n10+' msgs | normal: ~'+c.base+(c.ready?'':' (learning baseline...)')+(c.note?' | Last auto-clip: '+c.note:'')):'');
 const act=s.jobs.filter(j=>!j.t1).length;
 pill('pBuf','Buffer '+bt_,s.running?'on':'');pill('pChat',conn?'Chat '+c.n10+'/10s':'Chat off',conn?(spk?'hot':'on'):'');
 pill('pJobs',act?act+' job'+(act>1?'s':'')+' running':'Idle',act?'hot':'');pill('pAI',s.whisper?'Speech AI ready':'Speech AI not installed',s.whisper?'on':'bad');
 $('aihint').textContent=s.whisper?'':'Captions and speech-aware AI need: pip install faster-whisper (everything else works without it).';
 JOBS=s.jobs;skew=s.now-Date.now()/1000;renderJobs();
 if(s.error&&s.error!==lastErr){lastErr=s.error;say(s.error)}
 if(s.message!==lastMsg){lastMsg=s.message;if(/^(Saved|Found)/.test(s.message))say(s.message,'ok')}
 if(s.running&&!lastEmbed&&s.url)setStage(s.url);
 if(s.sver!==lastSver){lastSver=s.sver;renderSugg()}
 if(s.media!==lastMedia){lastMedia=s.media;await renderMedia(typeof sel==='string'?sel:'')}
 if(s.clips!==lastClips){lastClips=s.clips;const l=await api('/api/clips');
  $('list').innerHTML=l.length?'':'<p class="empty">No clips yet.</p>';
  l.forEach(k=>{const d=document.createElement('div');d.className='clip';const u='/clips/'+encodeURIComponent(k.name);
   d.innerHTML='<video controls preload="metadata"></video><div class="nm"></div><div class="acts"><a download>Download</a><button>Delete</button></div>';
   d.querySelector('video').src=u;d.querySelector('.nm').textContent=k.name+' ('+k.mb+' MB)';d.querySelector('a').href=u;
   d.querySelector('button').onclick=async()=>{await api('/api/delete',{kind:'clip',name:k.name});lastClips=-1;poll()};$('list').appendChild(d)})}}
load();place('c');place('g');nav(localStorage.getItem('cs3v')||'studio');render();setInterval(poll,1500);poll();
</script></body></html>
"""

def main():
    ap = argparse.ArgumentParser(description="Clipping Station")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        import keyboard  # optional: global hotkey that works inside games
        keyboard.add_hotkey(HOTKEY, lambda: live_clip(30, 0, "Hotkey clip"))
        state["hotkey"] = True
    except Exception:
        pass
    threading.Thread(target=cleaner, daemon=True).start()
    threading.Thread(target=analyzer, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://localhost:%d" % args.port
    print("Clipping Station is running at %s  (Ctrl+C to quit)\nClips: %s\nVideos: %s" % (url, CLIPS, MEDIA))
    for tool, hint in (("ffmpeg", "see the setup guide"),):
        if not shutil.which(tool):
            print("WARNING: %s not found on PATH (%s)" % (tool, hint))
    if not streamlink_cmd():
        print("NOTE: streamlink not found (needed for live streams): pip install streamlink")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_buffer(quiet=True)
        srv.server_close()


if __name__ == "__main__":
    main()
