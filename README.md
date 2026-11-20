# Clipping Station

Clip live streams and videos into **Reels / Shorts / TikTok (9:16)** format, with **automatic clips when chat spikes**.
Runs locally on your PC with a simple web interface. No accounts, no cloud.

## Features

- **Live stream buffer**: records Twitch, YouTube Live, Kick and other streams (via streamlink) into a rolling 15-minute buffer.
- **One-click clips**: save the last 15 / 30 / 60 / 90 seconds, or a custom "seconds before + seconds after".
- **Auto-clip on chat spikes**: detects when chat suddenly gets busy and saves a clip that starts 15-20 s before the spike and runs 45-60 s in total.
- **Video clipper**: upload a file or paste a link (YouTube, Twitch VODs, Instagram, X and more via yt-dlp), choose start and end, export.
- **AI auto clips for videos**: scores every second by loudness, motion and speech, picks the best moments, and lets you choose **how many clips**, **clip length**, **highlight style** (balanced, action, funny, talking) and **order**. Review the suggestions, play them, then render one or all.
- **Styled captions**: speech-to-text (local Whisper) with word-level timing. Styles: Pop (spoken word highlighted), Classic, Neon, Boxed, Minimal, in lower-third or centre.
- **Optional Claude ranking**: rates candidate clips for short-form potential and writes a title and hashtags for each (saved as a `.txt` next to the clip).
- **Studio with manual framing**: load a frame, then drag a **face cam** box and a **game footage** box exactly where you want them. Boxes can lock to the shape of the final area, so what you frame is what you get.
- **Live preview**: a phone-style preview updates instantly with your layout, format and caption style, plus optional platform safe zones. A "Check with real ffmpeg" button renders the exact result.
- **Formats**: 9:16 (Reels, Shorts, TikTok), 4:5 (Instagram feed), 1:1 (square) and 16:9 (YouTube).
- **Quality presets**: Standard, High (recommended) and Maximum, using high-quality scaling and x264 encoding.
- **Layouts**:
  - Split: streamer cam on top, gameplay below
  - Character focus: gameplay crop with X/Y position and zoom
  - Streamer focus: webcam only
  - Full frame on blurred background
  - Original 16:9 (no re-encode)
- **Layout preview** on a live stream or a selected video before exporting.
- Optional **F8 global hotkey** to clip the last 30 s from inside any app.

## Requirements

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/download.html) on your PATH
- A modern browser (Chrome, Edge, Firefox)

## Install

```bash
git clone https://github.com/vishalkayasth998/clipping-station.git
cd clipping-station
pip install -r requirements.txt
# optional extras (captions + speech AI, F8 hotkey, YouTube chat):
pip install -r requirements-optional.txt
```

Install ffmpeg:

| OS | Command |
|----|---------|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Check it with `ffmpeg -version`.

## Run

```bash
python clipping_station.py
```

Or double-click `run.bat` (Windows) / run `./run.sh` (macOS, Linux). Your browser opens `http://localhost:8787`.
Options: `--port 9000`, `--no-browser`.

## Usage

### Live streams

1. Open the **Live streams** tab and paste a stream link, e.g. `https://www.twitch.tv/channelname`.
2. Click **Start buffer**.
3. Click a clip button (or press `C` on the page, or `F8` if the hotkey is installed).
4. Clips appear on the right and in the `Clips/` folder.

### Auto-clip on chat spikes

1. Tick **Auto-clip enabled**.
2. Set **Start before spike** (default 18 s) and **Total clip length** (default 52 s).
3. Tune **Sensitivity**, **Min msgs / 10s** and **Cooldown**.
4. Use **Test auto-clip now** to check that the pipeline works.

How it works: every second the app compares the number of chat messages in the last 10 seconds with the average of the previous two minutes.
A clip is triggered when the current rate is at least the sensitivity multiplier (2x / 3x / 4.5x) of normal **and** above your minimum message count.
It needs about 70 seconds of chat to learn the baseline, and the cooldown prevents duplicate clips.
Chat detection supports Twitch links and YouTube `watch?v=` links (needs `pytchat`).

### Videos

1. Open the **Videos** tab.
2. Upload a file, or paste a link and click **Download**.
3. Select the video, set start and end (use **Use player time**), choose a layout, click **Make clip**.

### AI auto clips

1. Open the **Videos** tab, upload a file or paste a link, and select the video.
2. Under **AI auto clips**, choose **Clips wanted**, **Clip length**, **Highlight style** and **Order**.
3. Click **Find best moments**. Suggestions appear with timestamps, a score and a transcript snippet.
4. Use **Play** to check a moment, then **Render** for one clip or **Render all**.
5. Set your boxes, **Layout**, **Caption style** and **Speech model** in the Studio before rendering.

How it picks moments:

| Signal | What it measures |
|--------|------------------|
| Audio | Loudness compared with the surrounding minute (shouts, explosions, crowd reactions) |
| Motion | How much the picture changes (action, fast cuts) |
| Speech | Word density plus excited words ("no way", "let's go", "!"), needs faster-whisper |

Highlight styles change how those signals are weighted. Windows are chosen so clips never overlap, and cuts snap to pauses in speech.

**Optional Claude ranking:** tick *AI ranking + titles + hashtags* and paste an Anthropic API key (or set `ANTHROPIC_API_KEY` before starting). The key is only held in memory for the request. Only short transcript snippets are sent, never video. Override the model with the `CS_AI_MODEL` environment variable.

### Captions

Choose a **Caption style** in the Studio. Captions work on every layout except Original 16:9, on manual clips, live clips, auto-clips and AI clips. They need `faster-whisper` and an ffmpeg build with libass (standard builds have it). Speech models: `tiny` is fastest, `base` is balanced, `small` is most accurate.

### Studio: framing your cam and game

1. Open **Studio** and click **Grab frame from live stream**, or pick a video on the Videos & AI page and click **Grab frame from selected video** (it uses the player time, or type a time).
2. Drag the teal **CAM** box over the streamer's webcam and the pink **GAME** box over the gameplay you want to show. Drag a corner to resize.
3. Choose a **Format** and **Layout** (Split, Game only, Cam only, Full frame on blur). Use the **Cam area size** slider to give more or less room to the cam.
4. Watch the **Live preview** update. Tick *Show platform safe zones* to see where app buttons and captions cover the picture.
5. Pick a **Caption style** and **Output quality**. Every clip you make from Live, Videos, AI or auto-clip uses these settings.

Tips: keep **Lock shape** on for pixel-accurate framing; use **Fit boxes to shape** after changing format or layout; in 16:9 the split places cam and game side by side.

### Layout settings (legacy numbers)

- The boxes are stored as percentages of the frame. You can type exact values under *Exact numbers* in the Studio.

## Project layout

```
clipping-station/
  clipping_station.py        the whole app (server + web UI)
  requirements.txt           streamlink, yt-dlp
  requirements-optional.txt  keyboard, pytchat
  run.bat / run.sh           launchers
  docs/                      extra documentation
```

Runtime folders (`cs_buffer/`, `Clips/`, `Videos/`, `cs_preview/`) are created next to the script and ignored by git.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `streamlink is not installed` | `pip install streamlink` |
| `ffmpeg is not installed` | Install ffmpeg and reopen the terminal |
| Stream will not start | Check the channel is live; try another quality |
| No Twitch preview in the page | The preview is an embed; clipping still works without it |
| YouTube chat does not connect | `pip install pytchat` and use a `watch?v=` link |
| Captions skipped | `pip install faster-whisper`, set Speech model to tiny/base/small, and use a full ffmpeg build |
| AI finder is slow | Long videos take time: transcription is the slowest step. Use the `tiny` model or turn Speech model off |
| F8 hotkey does nothing | `pip install keyboard` (Windows; admin rights on macOS/Linux) |
| Clips are slow to export | Vertical clips are re-encoded; close heavy apps or pick a lower stream quality |

## Responsible use

Only clip and post content you have the right to use. Respect streamers' and platforms' terms of service and give credit.
This tool does not upload anything anywhere; posting is manual.

## Roadmap ideas

- Automatic face detection to place the cam box
- Character tracking for the game crop
- Multi-language caption translation
- Vision-based highlight detection (kills, crashes, scoreboard changes)
- Direct upload helpers

## License

MIT. See [LICENSE](LICENSE).
