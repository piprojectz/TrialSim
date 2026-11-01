"""
TrialSim - Neural voices for the player (Kokoro + Chatterbox, runs locally)
---------------------------------------------------------------------------
Reads a run's transcript, gives each character a voice, and saves one audio clip per line into
runs/<run>/audio/. player.py picks the clips up automatically; without them the player falls
back to the browser's built-in voices.

Engines:
  Kokoro      stock voices, fast (most of the cast)
  Chatterbox  clones a voice from a sample recording (Wade: voices/wade.wav)

Setup (once):   pip install kokoro soundfile chatterbox-tts "setuptools<81"
                ffmpeg on PATH = small MP3s; without it, clips are saved as WAV (much bigger)
Usage:
    python voices.py                 # newest run
    python voices.py runs\\<folder>   # a specific run
    python voices.py --redo          # regenerate every clip (default: only new/changed lines)
    python voices.py --no-thoughts   # skip private thoughts (fewer clips, much faster)
    python voices.py --only pros     # only one character (pros, def, judge, witness, J1..J6)
    python voices.py --device cpu    # force CPU (default: GPU if this PyTorch build supports it)
Then:
    python player.py --open          # rebuild the player so it uses the clips
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import player

MP3_BITRATE = "96k"
VOICES_DIR = player.BASE / "voices"

# Kokoro voice ids: a* = American, b* = British; f = female, m = male.
# Chatterbox: "ref" = sample recording; exaggeration 0.25-2 (drama), cfg_weight 0-1 (lower = slower,
# more deliberate). Edit freely; only lines whose voice settings changed get regenerated.
CAST = {
    "judge": {"engine": "kokoro", "voice": "af_bella", "speed": 1.08},
    "pros": {"engine": "chatterbox", "ref": "wade.wav", "exaggeration": 1.2, "cfg_weight": 0.3,
             "think_exaggeration": 0.6, "think_cfg_weight": 0.4},      # inner voice: calmer
    "def": {"engine": "kokoro", "voice": "bm_george", "speed": 1.0},
}
WITNESS_VOICES = ["af_nicole", "am_adam", "af_sarah", "am_eric", "af_river", "am_michael"]
JUROR_VOICES = {1: "am_liam", 2: "af_jessica", 3: "am_echo", 4: "af_kore", 5: "am_puck", 6: "af_aoede"}
THOUGHT_SPEED = 1.05
CHUNK_CHARS = 280            # Chatterbox reads long text in sentence groups up to this size


def voice_for(b):
    k = b.get("key") or ""
    if k in CAST:
        v = dict(CAST[k])
    elif k == "witness":
        v = {"engine": "kokoro", "voice": WITNESS_VOICES[b.get("standIdx", 0) % len(WITNESS_VOICES)],
             "speed": 1.0}
    elif k.startswith("J"):
        v = {"engine": "kokoro", "voice": JUROR_VOICES.get(int(k[1:]), "am_adam"), "speed": 1.0}
    else:
        v = {"engine": "kokoro", "voice": "am_adam", "speed": 1.0}
    if b["t"] == "think":
        if v["engine"] == "kokoro":
            v["speed"] *= THOUGHT_SPEED
        else:
            v["exaggeration"] = v.get("think_exaggeration", v["exaggeration"])
            v["cfg_weight"] = v.get("think_cfg_weight", v["cfg_weight"])
    return v


def signature(v, text):
    if v["engine"] == "kokoro":
        key = f"k|{v['voice']}|{v['speed']:.3f}"
    else:
        ref = VOICES_DIR / v["ref"]
        st = ref.stat()
        key = f"c|{v['ref']}|{st.st_size}|{int(st.st_mtime)}|{v['exaggeration']}|{v['cfg_weight']}"
    return hashlib.sha1(f"{key}|{text}".encode()).hexdigest()[:12]


def label(v):
    return v["voice"] if v["engine"] == "kokoro" else f"chatterbox:{Path(v['ref']).stem}"


def spoken(text):
    """Small fixes so the TTS reads courtroom text naturally."""
    t = text.replace("Planning to spin or hide:", "Planning to spin or hide.")
    t = t.replace("Worried about:", "Worried about.").replace("Honesty check:", "Honesty check.")
    t = t.replace("Lines I crossed:", "Lines I crossed.")
    return t


def chunks_of(text, limit=CHUNK_CHARS):
    sentences = re.findall(r"[^.!?\n]+[.!?]*[\"')\]]*\s*", text) or [text]
    out, cur = [], ""
    for s in (x.strip() for x in sentences):
        if not s:
            continue
        if cur and len(cur) + len(s) + 1 > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def pick_device():
    """GPU only if this PyTorch build can actually run on it (e.g. RTX 50-series needs CUDA 12.8+)."""
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        (torch.ones(8, device="cuda") * 2).sum().item()        # fails if no kernels for this GPU
        lstm = torch.nn.LSTM(4, 4).cuda()                          # cuDNN path Chatterbox uses
        lstm(torch.zeros(1, 1, 4, device="cuda"))
        return "cuda"
    except Exception as e:
        print(f"  (GPU not usable with this PyTorch build, using CPU: {str(e).splitlines()[0]})")
        return "cpu"


class Engines:
    def __init__(self, device="auto"):
        self.kokoro, self.cb, self.cb_sr = {}, None, 24000
        self.device = device if device != "auto" else pick_device()
        print(f"  (voices running on {self.device.upper()})")

    def say(self, v, text):
        """Returns (samples float32, sample_rate)."""
        if v["engine"] == "kokoro":
            from kokoro import KPipeline
            lang = v["voice"][0]                       # 'a' American, 'b' British
            if lang not in self.kokoro:
                self.kokoro[lang] = KPipeline(lang_code=lang, device=self.device)
            parts = [np.asarray(a, dtype=np.float32)
                     for _, _, a in self.kokoro[lang](text, voice=v["voice"], speed=v["speed"])
                     if a is not None]
            return (np.concatenate(parts) if parts else None), 24000
        if self.cb is None:
            from chatterbox.tts import ChatterboxTTS
            print(f"  (loading Chatterbox on {self.device.upper()}"
                  f"{'; this is slow on CPU' if self.device == 'cpu' else ''})")
            self.cb = ChatterboxTTS.from_pretrained(device=self.device)
            self.cb_sr = self.cb.sr
        gap = np.zeros(int(self.cb_sr * 0.18), dtype=np.float32)
        parts = []
        for c in chunks_of(text):
            w = self.cb.generate(c, audio_prompt_path=str(VOICES_DIR / v["ref"]),
                                 exaggeration=v["exaggeration"], cfg_weight=v["cfg_weight"])
            parts += [w.squeeze().cpu().numpy().astype(np.float32), gap]
        return (np.concatenate(parts[:-1]) if parts else None), self.cb_sr


def main():
    ap = argparse.ArgumentParser(description="Generate voice clips for a TrialSim run.")
    ap.add_argument("run", nargs="?", help="run folder (default: newest)")
    ap.add_argument("--redo", action="store_true", help="regenerate every clip")
    ap.add_argument("--no-thoughts", action="store_true", help="skip private thoughts")
    ap.add_argument("--only", help="only this character key: pros, def, judge, witness, J1..J6")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                    help="where to run the voice models (default: GPU if it works, else CPU)")
    a = ap.parse_args()

    run_dir = player.find_run(a.run)
    recs = [json.loads(l) for l in
            (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    result = player.load_json(run_dir / "result.json")
    data = player.build(recs, player.load_case(run_dir, result), result)

    for k, v in CAST.items():
        if v["engine"] == "chatterbox" and not (VOICES_DIR / v["ref"]).exists():
            raise SystemExit(f"Missing voice sample for {k}: {VOICES_DIR / v['ref']}")

    out_dir = run_dir / "audio"
    out_dir.mkdir(exist_ok=True)
    man_path = out_dir / "manifest.json"
    old = (player.load_json(man_path) or {}) if not a.redo else {}
    ffmpeg = shutil.which("ffmpeg")
    ext = ".mp3" if ffmpeg else ".wav"
    if not ffmpeg:
        print("ffmpeg not found on PATH: saving WAV clips (large).")

    eng = Engines(a.device)
    manifest = dict(old) if a.only else {}
    made = kept = 0
    todo = [(i, b) for i, b in enumerate(data["beats"])
            if (b["t"] == "say" or (b["t"] == "think" and not a.no_thoughts))
            and (not a.only or b.get("key") == a.only)]
    started = time.time()
    for n, (i, b) in enumerate(todo, 1):
        v = voice_for(b)
        text = spoken(b["text"])
        sig = signature(v, text)
        name = f"b{i:03d}_{sig}{ext}"
        prev = old.get(str(i))
        if prev and prev.get("file") == name and (out_dir / name).exists():
            manifest[str(i)] = prev
            kept += 1
            continue
        t0 = time.time()
        wave, sr = eng.say(v, text)
        if wave is None or not len(wave):
            print(f"  [{n}/{len(todo)}] beat {i}: no audio produced, skipped")
            continue
        wav_path = out_dir / f"b{i:03d}_{sig}.wav"
        sf.write(wav_path, wave, sr)
        if ffmpeg:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-ac", "1",
                            "-b:a", MP3_BITRATE, str(out_dir / name)], check=True)
            wav_path.unlink()
        if prev and prev.get("file") != name:
            (out_dir / prev["file"]).unlink(missing_ok=True)   # line changed: drop the old clip
        manifest[str(i)] = {"file": name, "secs": round(len(wave) / sr, 2), "voice": label(v)}
        made += 1
        man_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")   # resume-safe
        print(f"  [{n}/{len(todo)}] {b.get('name', '')}: {label(v)} "
              f"({manifest[str(i)]['secs']}s audio, took {time.time() - t0:.0f}s)")

    man_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    total = sum(m["secs"] for m in manifest.values())
    size = sum((out_dir / m["file"]).stat().st_size for m in manifest.values()
               if (out_dir / m["file"]).exists()) / 1e6
    print(f"\n{made} new clip(s), {kept} reused in {(time.time() - started) / 60:.1f} min · "
          f"{total / 60:.1f} min of audio · {size:.1f} MB -> {out_dir}\n"
          f"Now run: python player.py {run_dir} --open")


if __name__ == "__main__":
    main()
