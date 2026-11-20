# Changelog

Notable changes. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/).

## 0.1.0 - initial public release

First public release of ClipStudio.

- Long video in, ready-to-post 9:16 clips out: ingest (YouTube link or file upload),
  transcription, AI highlight detection, speaker-aware reframing, burned-in captions,
  and export at 9:16, 1:1 or 16:9.
- Free-tier friendly defaults: Groq (Whisper) for transcription and NVIDIA build for
  the language model. Anthropic, OpenAI-compatible endpoints, Gemini and Ollama are
  also supported.
- Automatic editing: silence trimming, filler-word removal, punch-in zooms, keyword
  emphasis, progress bar, loudness normalisation and optional background music.
- Web UI with live job progress, clip review (approve, trim, edit captions), job
  queue, channel watching and per-job campaign briefs.
- First-run setup screen that asks for the free API keys and checks them before saving.
- `clipstudio doctor` diagnoses ffmpeg, GPU, dependencies and provider reachability.
- Resumable pipeline: artifacts are stored per job, so a retry restarts at the stage
  that failed.
- Built for modest hardware: no GPU required, low-priority ffmpeg, light analysis
  settings.
