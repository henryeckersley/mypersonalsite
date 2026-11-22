#!/usr/bin/env python3
"""Nine-chapter premium neural audiobook with local Kokoro 82M (no paid TTS service)."""
import json,os,re,subprocess,sys,time
from pathlib import Path
import numpy as np
import soundfile as sf
from kokoro import KPipeline

BASE=Path(__file__).resolve().parent
OUT=BASE/"rendered-kokoro"
OUT.mkdir(parents=True,exist_ok=True)
story=(BASE/"story.txt").read_text(encoding="utf-8")
story=story[story.index("CHAPTER ONE. "):].split("\nTHE END.")[0].strip()
chapters=[s.strip() for s in re.split(r"(?m)(?=^CHAPTER [A-Z]+\. )",story) if s.strip()]
assert len(chapters)==8, len(chapters)
chapters[0] = "THE LONGEST MINUTE.\n\n" + chapters[0]
voice=os.getenv("VOICE","am_michael")
print("KOKORO",voice,len(story),"chars",flush=True)
pipeline=KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
sr=24000
all_wav=[]
chapter_meta=[]
elapsed=0
for i,ch in enumerate(chapters,1):
    title=ch.split("\n",1)[0]
    chunks=[]
    count=0
    t=time.time()
    for gs,ps,audio in pipeline(ch,voice=voice,speed=0.94,split_pattern=r"\n\n"):
        a=audio.numpy() if hasattr(audio,"numpy") else np.asarray(audio)
        if len(a)>1000:
            chunks.append(a.astype(np.float32))
            chunks.append(np.zeros(int(.20*sr),dtype=np.float32))
            count+=1
            if count%10==0: print("CH",i,"chunks",count,"seconds",round(time.time()-t,1),flush=True)
    assert count>1, f"Chapter {i} synthesis empty"
    x=np.concatenate(chunks)
    mx=np.max(np.abs(x))
    if mx>0.94: x*=0.94/mx
    # gentle pauses at chapter boundaries
    ch_path=OUT/f"chapter_{i:02d}.wav"
    sf.write(ch_path,x,sr)
    chapter_meta.append({"chapter":title,"start_ms":int(elapsed/sr*1000)})
    all_wav.append(np.zeros(sr if i>1 else int(sr*.45),dtype=np.float32))
    all_wav.append(x)
    elapsed+=len(x)+len(all_wav[-2])
    print("DONE CH",i,title,"sec",round(len(x)/sr,1),"chunks",count,flush=True)
all_wav.append(np.zeros(sr*2,dtype=np.float32))
master=np.concatenate(all_wav)
assert len(master)/sr>18*60,f"Unexpectedly short audio {len(master)/sr}"
wav=OUT/"book_master.wav"
sf.write(wav,master,sr,subtype="PCM_16")
mp3=OUT/"The_Longest_Minute_Complete_Neural_Audiobook.mp3"
subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(wav),"-af","loudnorm=I=-18:TP=-1.5:LRA=11","-ac","1","-ar","44100","-codec:a","libmp3lame","-b:a","160k",str(mp3)],check=True)
meta={"voice":voice,"chapter_starts":chapter_meta,"duration_ms":int(len(master)/sr*1000),"script_characters":len(story),"book_title":"The Longest Minute","file_bytes":mp3.stat().st_size}
(OUT/"chapters.json").write_text(json.dumps(meta,indent=2))
print("COMPLETE",json.dumps(meta),flush=True)
