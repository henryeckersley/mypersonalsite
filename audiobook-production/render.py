#!/usr/bin/env python3
"""Render The Last Edition with a natural neural voice, QA every chapter, master a single offline audiobook."""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from pydub import AudioSegment

BASE = Path(__file__).resolve().parent
OUT = BASE / "rendered"
SPLITS = OUT / "segments"
CHAPTERS = OUT / "chapters"
for p in (OUT, SPLITS, CHAPTERS):
    p.mkdir(parents=True, exist_ok=True)

RAW = (BASE / "story.txt").read_text(encoding="utf-8")
HEADINGS = re.findall(r"(?m)^CHAPTER [A-Z]+\. [^\n]+", RAW)
assert len(HEADINGS) == 9, f"expected nine chapters, found {HEADINGS}"
RAW = RAW[RAW.index("CHAPTER ONE. "):].split("\nTHE END.")[0].strip()
SECTIONS = re.split(r"(?m)(?=^CHAPTER [A-Z]+\. )", RAW)
SECTIONS = [s.strip() for s in SECTIONS if s.strip()]
assert len(SECTIONS) == 9, [x[:50] for x in SECTIONS]

VOICE_LIST = ["en-US-AndrewMultilingualNeural", "en-US-BrianMultilingualNeural", "en-US-ChristopherNeural", "en-US-GuyNeural"]
VOICE = os.environ.get("VOICE", VOICE_LIST[0])
CHUNK_LIMIT = 1750

def chunk_text(text, limit=CHUNK_LIMIT):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    parts, buf = [], ""
    for para in paras:
        if len(para) > limit:
            units = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'\u201c])", para)
        else:
            units = [para]
        for unit in units:
            if len(unit) > limit:
                units2 = [unit[i:i+limit] for i in range(0, len(unit), limit)]
            else:
                units2 = [unit]
            for sub in units2:
                if len(buf) + len(sub) + 2 > limit and buf:
                    parts.append(buf)
                    buf = ""
                buf += ("\n\n" if buf else "") + sub
    if buf: parts.append(buf)
    assert "".join(parts).replace("\n","").replace(" ","") == "".join(paras).replace("\n","").replace(" ",""), "Text lost in splitting"
    return parts

segments = []
chapter_map = {}
for i, chapter in enumerate(SECTIONS, 1):
    blocks = chunk_text(chapter)
    chapter_map[i] = []
    for j, passage in enumerate(blocks, 1):
        path = SPLITS / f"ch{i:02d}_seg{j:02d}.mp3"
        chapter_map[i].append(path)
        segments.append((i, j, passage, path))

print("SCRIPT", len(RAW), "characters,", len(SECTIONS), "chapters,", len(segments), "audio segments", flush=True)
print("VOICE", VOICE, flush=True)

async def render_one(ch, idx, passage, dest):
    import edge_tts
    if dest.exists() and dest.stat().st_size > 1000:
        return
    err = None
    for voice in [VOICE] + [v for v in VOICE_LIST if v != VOICE]:
        for attempt in range(3):
            try:
                temp_dest = dest.with_suffix(".tmp")
                comm = edge_tts.Communicate(
                    passage,
                    voice=voice,
                    rate="-5%",
                    pitch="+0Hz",
                    volume="+0%",
                    boundary="SentenceBoundary",
                )
                await comm.save(str(temp_dest))
                if temp_dest.stat().st_size < 1200:
                    raise IOError("Suspiciously short MP3")
                temp_dest.replace(dest)
                print(f"OK ch {ch:02d} segment {idx:02d}: {dest.stat().st_size} bytes; voice={voice}", flush=True)
                return
            except Exception as exc:
                err = repr(exc)
                print("RETRY",ch,idx,voice,attempt+1,err,flush=True)
                await asyncio.sleep((attempt+1)*2)
    raise RuntimeError(f"All Edge speech attempts failed for chapter {ch}, seg {idx}: {err}")

async def synth():
    sem = asyncio.Semaphore(2)
    async def single(item):
        async with sem:
            await render_one(*item)
    await asyncio.gather(*(single(t) for t in segments))

asyncio.run(synth())

# Decode/validate each segment and assemble chapters with audible paragraph transitions.
chapter_starts = []
master = AudioSegment.silent(duration=520, frame_rate=44100).set_channels(1)
for ch in range(1,10):
    chapter_starts.append((HEADINGS[ch-1], len(master)))
    chapter_audio = AudioSegment.silent(duration=220, frame_rate=44100).set_channels(1)
    for i, path in enumerate(chapter_map[ch]):
        seg = AudioSegment.from_file(path, format="mp3").set_channels(1).set_frame_rate(44100)
        assert len(seg) > 1500, f"Bad audio segment {path}, {len(seg)}ms"
        chapter_audio += seg
        if i < len(chapter_map[ch])-1:
            chapter_audio += AudioSegment.silent(duration=370, frame_rate=44100)
    # mild dynamic range limiting with natural headroom; no noisy overcompression
    chapter_audio = chapter_audio.apply_gain(-2.5 - chapter_audio.max_dBFS)
    out_ch = CHAPTERS / f"{ch:02d}_{HEADINGS[ch-1].split('. ')[-1].replace(' ','_').replace('.','')}.mp3"
    chapter_audio.export(out_ch, format="mp3", bitrate="160k", parameters=["-ar","44100","-ac","1"])
    master += chapter_audio
    master += AudioSegment.silent(duration=(1000 if ch<9 else 2600), frame_rate=44100)
    print("CHAPTER",ch,len(chapter_audio),"ms",flush=True)

# Smooth global mastering: voice peak target -2dBFS, retain dynamics; no clipping.
if master.max_dBFS > -2:
    master = master.apply_gain(-2-master.max_dBFS)
output = OUT / "The_Last_Edition_Neural_Narration.mp3"
master.export(output,format="mp3",bitrate="160k",tags={
    "title":"The Last Edition",
    "artist":"Original fiction / Neural audiobook narration",
    "album":"The Last Edition — Flight Audiobook",
    "genre":"Audiobook",
    "comment":"Complete nine-chapter story. Original text preserved.",
},parameters=["-ar","44100","-ac","1"])

metadata = {
    "voice":VOICE,
    "chapters": [{"title":label, "start_ms":ms} for label,ms in chapter_starts],
    "duration_ms":len(master),
    "script_chars":len(RAW),
    "segment_count":len(segments),
    "file_bytes":output.stat().st_size,
}
(OUT / "chapters.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
assert len(master)>20*60*1000, f"Unexpectedly short audiobook: {len(master)} ms"
print("COMPLETE",json.dumps(metadata),flush=True)
