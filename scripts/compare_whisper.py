"""Compare faster-whisper models on Bhanu's own push-to-talk recordings.

The widget keeps each clip when "voice": {"keep_recordings": true} is set in the per-user config
(<data>/voice_samples/, with samples.jsonl). Read the commands in voice_test_commands.txt aloud,
one push-to-talk hold each, in order; then run this with Hermes's Python, which has faster-whisper:

    <hermes venv>\\Scripts\\python.exe scripts\\compare_whisper.py [--models base small] [--last 20]

Each clip is transcribed by every model with the vocabulary hint Hermes uses (stt.prompt), on the
CPU as the twin runs it, and scored by word error rate against the command it should be. Clips
are paired with the list in order: the last N recordings against the N commands.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path

DATA = Path(os.environ.get("STUDENT_TWIN_HOME") or Path(os.environ["LOCALAPPDATA"]) / "student-twin")
SAMPLES = DATA / "voice_samples"
COMMANDS = Path(__file__).with_name("voice_test_commands.txt")
HERMES_CONFIG = Path(os.environ.get("HERMES_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes") / "config.yaml"


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("-", " ")).split()


def wer(ref: str, hyp: str) -> float:
    """Word error rate: word-level edit distance over the reference length."""
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(1, len(r))


def stt_settings() -> tuple[str, str]:
    import yaml
    stt = (yaml.safe_load(HERMES_CONFIG.read_text(encoding="utf-8")) or {}).get("stt", {}) or {}
    return stt.get("prompt", "") or "", stt.get("language", "en") or "en"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["base", "small"])
    ap.add_argument("--last", type=int, default=0, help="use the last N clips (default: as many as commands)")
    args = ap.parse_args()
    commands = [c.strip() for c in COMMANDS.read_text(encoding="utf-8").splitlines() if c.strip() and not c.startswith("#")]
    rows = [json.loads(line) for line in (SAMPLES / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    n = args.last or len(commands)
    rows = rows[-n:]
    pairs = list(zip(commands[:len(rows)], rows))
    prompt, language = stt_settings()
    from faster_whisper import WhisperModel
    results = {m: [] for m in args.models}
    for m in args.models:
        model = WhisperModel(m, device="cpu", compute_type="int8")
        for expected, row in pairs:
            t0 = time.perf_counter()
            segments, _ = model.transcribe(str(SAMPLES / row["file"]), language=language, initial_prompt=prompt or None,
                                           vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
            results[m].append((text, wer(expected, text), time.perf_counter() - t0))
    print(f"{len(pairs)} clips; prompt {'on' if prompt else 'off'}; language {language}\n")
    for i, (expected, row) in enumerate(pairs):
        print(f"{i + 1:2}. said:  {expected}")
        for m in args.models:
            text, e, _ = results[m][i]
            print(f"    {m:6} {'ok ' if e == 0 else f'{e:.0%}'.rjust(3)} {text}")
    print()
    for m in args.models:
        rs = results[m]
        exact = sum(1 for _, e, _ in rs if e == 0)
        print(f"{m:6} word error rate {sum(e for _, e, _ in rs) / max(1, len(rs)):.1%}, exactly right {exact}/{len(rs)}, "
              f"{sum(t for _, _, t in rs) / max(1, len(rs)):.2f} s a clip")


if __name__ == "__main__":
    main()
