# -*- coding: utf-8 -*-
"""Расшифровка дорожек созвона (#88): каждый webm — один говорящий.

Запускается сервером ПОД venv соседнего пайплайна dnd-transcribe
(GD_TRANSCRIBE_DIR): оттуда берутся onnx_asr, конфиг с путями к ffmpeg и
модели GigaAM и класс AsrEngine. Диаризации нет — дорожки уже пер-спикерные.
Выход — JSON реплик для поста в тред: [{uid, name, start, text}, ...].

    venv\\Scripts\\python transcribe_call.py --rec-dir data/calls/4
        --dnd-dir D:/Agent/dnd-transcribe --out data/calls/4/transcript.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

TARGET_SR = 16000
GAP_UTTER = 1.2   # пауза внутри дорожки, режущая речь на реплики, с
MAX_UTTER = 28.0  # предел длины реплики для RNN-T, с
GAP_MERGE = 30.0  # соседние реплики одного говорящего ближе этого — одно сообщение
GAP_PARA = 8.0    # ...с разрывом абзаца, если пауза больше этого


def to_wav(ffmpeg: str, src: Path, dst: Path) -> None:
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
                    "-ac", "1", "-ar", str(TARGET_SR), "-vn",
                    "-c:a", "pcm_s16le", str(dst)], check=True)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Расшифровка дорожек созвона")
    ap.add_argument("--rec-dir", required=True, help="каталог с {uid}_{session}.webm и meta.json")
    ap.add_argument("--dnd-dir", required=True, help="корень пайплайна dnd-transcribe")
    ap.add_argument("--out", required=True, help="куда писать JSON реплик")
    args = ap.parse_args()

    dnd = Path(args.dnd_dir)
    sys.path.insert(0, str(dnd))
    import onnx_asr
    import yaml
    cfg = yaml.safe_load((dnd / "config.yaml").read_text(encoding="utf-8"))
    from asr import AsrEngine
    print("[asr] загружаю GigaAM (CPU)…", file=sys.stderr)
    engine = AsrEngine(cfg["gigaam_model_dir"], cfg["asr_variant"],
                       ["CPUExecutionProvider"],
                       max_chunk_sec=cfg["asr"]["max_chunk_sec"],
                       min_silence_ms=cfg["asr"]["min_silence_ms"])
    vad = onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])

    rec = Path(args.rec_dir)
    joins: dict[tuple[int, int], dict] = {}
    meta_path = rec / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        joins = {(j["uid"], j["session"]): j for j in meta.get("joins", [])}

    utterances: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in sorted(rec.glob("*.webm")):
            m = re.fullmatch(r"(\d+)_(\d+)\.webm", f.name)
            if not m:
                continue
            uid, session = int(m.group(1)), int(m.group(2))
            j = joins.get((uid, session), {})
            base = float(j.get("offset", 0.0))
            name = j.get("name") or f"id{uid}"
            wav = Path(tmp) / (f.stem + ".wav")
            to_wav(cfg["ffmpeg"], f, wav)
            audio = read_wav(wav)
            # Сегменты VAD клеим в реплики и распознаём ЦЕЛЫМИ: пословный
            # transcribe_timeline и распознавание обрывков без контекста
            # заметно врут (сравнение на звонке 4, #88).
            segs = next(vad.segment_batch(audio[None, :],
                                          np.array([audio.size]), TARGET_SR))
            groups: list[list[float]] = []
            for s0, s1 in segs:
                a, b = s0 / TARGET_SR, s1 / TARGET_SR
                g = groups[-1] if groups else None
                if g and a - g[1] <= GAP_UTTER and b - g[0] <= MAX_UTTER:
                    g[1] = b
                else:
                    groups.append([a, b])
            print(f"[track] {f.name}: {audio.size / TARGET_SR:.0f} c, "
                  f"{len(groups)} реплик", file=sys.stderr)
            for a, b in groups:
                lo = max(0, int((a - 0.1) * TARGET_SR))
                hi = min(audio.size, int((b + 0.1) * TARGET_SR))
                text = engine.recognize(audio[lo:hi]).strip()
                if text:
                    utterances.append({"uid": uid, "name": name,
                                       "start": base + a, "end": base + b,
                                       "text": text})

    utterances.sort(key=lambda u: u["start"])
    merged: list[dict] = []
    for u in utterances:
        last = merged[-1] if merged else None
        if last is not None and last["uid"] == u["uid"] \
                and u["start"] - last["end"] <= GAP_MERGE:
            sep = "\n\n" if u["start"] - last["end"] > GAP_PARA else " "
            last["text"] += sep + u["text"]
            last["end"] = u["end"]
        else:
            merged.append(u)

    out = [{"uid": u["uid"], "name": u["name"],
            "start": round(u["start"], 1), "text": u["text"].strip()}
           for u in merged if u["text"].strip()]
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"[done] реплик: {len(out)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
