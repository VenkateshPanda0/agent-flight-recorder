"""Synthesize narration with Piper (MIT) and a voice model, then mux it into the recorded video.

  pip install piper-tts
  # voice: en_US-joe-medium (CC0) from https://huggingface.co/rhasspy/piper-voices
  python scripts/make_voiceover.py narration.json voice.onnx out_dir video.webm out.mp4

narration.json: [{"id": "...", "text": "..."}]. Writes narration_timed.json (with durations) next to it.
Run record_demo.js with that file, then run this again with the recorded video to mux.
"""
import json
import subprocess
import sys
import wave
from pathlib import Path

narr_path, voice_path, out_dir = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
segs = json.loads(narr_path.read_text())
out_dir.mkdir(parents=True, exist_ok=True)
from piper import PiperVoice
voice = PiperVoice.load(voice_path)
for s in segs:
    wav = out_dir / f"{s['id']}.wav"
    if not wav.exists():  # keep earlier takes so scene timing recorded against them stays valid
        with wave.open(str(wav), "wb") as w:
            voice.synthesize_wav(s["text"], w)
    with wave.open(str(wav)) as w:
        s["dur"] = w.getnframes() / w.getframerate()
(narr_path.with_name("narration_timed.json")).write_text(json.dumps(segs, indent=1))
print("durations:", {s["id"]: round(s["dur"], 1) for s in segs})

if len(sys.argv) >= 6:
    video, final = sys.argv[4], sys.argv[5]
    timings = json.loads((out_dir / "timings.json").read_text())
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", video]
    for s in segs:
        cmd += ["-i", str(out_dir / f"{s['id']}.wav")]
    delays = ";".join(f"[{i + 1}:a]adelay={int(timings[s['id']] * 1000)}|{int(timings[s['id']] * 1000)}[a{i}]"
                      for i, s in enumerate(segs))
    mix = "".join(f"[a{i}]" for i in range(len(segs))) + f"amix=inputs={len(segs)}:normalize=0,volume=0.85[aout]"
    cmd += ["-filter_complex", delays + ";" + mix, "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", final]
    subprocess.run(cmd, check=True)
    print("wrote", final)
