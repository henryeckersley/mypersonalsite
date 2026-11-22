#!/usr/bin/env python3
"""NO FLOOR - full audiobook narration. Each chapter is checked for text loss, failures, decoding and duration."""
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from pydub import AudioSegment

HERE=Path(__file__).resolve().parent
SRC=HERE/'story.txt'
OUT=HERE/'out'
PARTS=OUT/'parts'
PARTS.mkdir(parents=True,exist_ok=True)
VOICE=os.getenv('VOICE','en-US-ChristopherNeural')
RATE=os.getenv('RATE','-6%')
STORY=SRC.read_text(encoding='utf-8').replace('\r\n','\n')
HEADERS=re.findall(r'(?m)^CHAPTER [A-Z]+\. [^\n]+',STORY)
assert len(HEADERS)==8, f'Expected 8 chapters, found {len(HEADERS)}'
SECTIONS=re.split(r'(?m)(?=^CHAPTER [A-Z]+\. )',STORY)
SECTIONS=[x.strip().removesuffix('THE END.').strip() for x in SECTIONS if x.strip().startswith('CHAPTER ')]
assert len(SECTIONS)==8,len(SECTIONS)

def clean(s):
    s=s.replace('—',', ').replace('–',' to ').replace('…','...')
    s=s.replace('“','"').replace('”','"').replace('’',"'")
    return s

MAX_CHARS=925

def passages(text):
    paras=[x.strip() for x in re.split(r'\n\s*\n',text) if x.strip()]
    packed=[]; buff=[]; char_count=0
    for p in paras:
        if len(p)>MAX_CHARS:
            raise ValueError('A paragraph exceeds safe speech chunk length')
        if buff and char_count+len(p)+2>MAX_CHARS:
            packed.append(clean('\n\n'.join(buff)))
            buff=[];char_count=0
        buff.append(p);char_count+=len(p)+2
    if buff:packed.append(clean('\n\n'.join(buff)))
    norm=lambda x: re.sub(r'\s+','',x).lower()
    assert norm(''.join(packed))==norm(clean(''.join(paras))), 'Text loss in TTS segmentation'
    return packed

jobs=[];chapter_files={}
for ch,raw in enumerate(SECTIONS,1):
    raw = ('NO FLOOR. A TRUE STORY.\n\n'+raw) if ch==1 else raw
    items=passages(raw)
    chapter_files[ch]=[]
    for seq,part in enumerate(items,1):
        dest=PARTS/f'chapter{ch:02d}_segment{seq:02d}.mp3'
        jobs.append((ch,seq,part,dest))
        chapter_files[ch].append(dest)

print('CHAPTERS',len(SECTIONS),'CHUNKS',len(jobs),'WORDS',len(STORY.split()), 'VOICE',VOICE,flush=True)

async def one_text(text,path):
    import edge_tts
    tmp=path.with_suffix('.pending.mp3')
    for i in range(9):
        try:
            client=edge_tts.Communicate(text=text,voice=VOICE,rate=RATE,volume='+0%',pitch='-1Hz')
            await asyncio.wait_for(client.save(str(tmp)),timeout=130)
            if tmp.stat().st_size<1400:
                raise IOError('Tiny MP3 returned')
            await asyncio.to_thread(probe_duration,tmp)
            tmp.replace(path)
            return
        except Exception as exc:
            print('RETRY',path.name,i+1,repr(exc),flush=True)
            tmp.unlink(missing_ok=True)
            await asyncio.sleep(min(13,2+i*2))
    # The speech provider can reject occasional long chunks. Maintain the SAME voice.
    sentences=re.split(r'(?<=[.!?])\s+',text)
    pivot=max(1,len(sentences)//2)
    if pivot>=len(sentences):
        raise RuntimeError('Could not synthesize '+path.name+' in specified voice')
    a=path.with_suffix('.half-a.mp3');b=path.with_suffix('.half-b.mp3')
    # Defer to single-voice split, never splice in a different narrator.
    await one_text(' '.join(sentences[:pivot]),a)
    await one_text(' '.join(sentences[pivot:]),b)
    joined=AudioSegment.from_file(a)+AudioSegment.silent(duration=110)+AudioSegment.from_file(b)
    joined.export(path,format='mp3',bitrate='128k')
    a.unlink();b.unlink()

def probe_duration(path):
    d=subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],text=True,timeout=25)
    if not d.strip():raise ValueError('ffprobe could not read file')
    return float(d)

async def work(ch,seq,text,dest,sem):
    async with sem:
        if dest.exists() and dest.stat().st_size>1000:
            duration=probe_duration(dest)
        else:
            await one_text(text,dest)
            duration=probe_duration(dest)
        wc=len(text.split())
        if duration < max(2.5,wc/4.6) or duration > max(20,wc/1.15):
            print('WARNING unusual segment timing',ch,seq,wc,round(duration,2),flush=True)
        print('OK',ch,seq,wc,'words',round(duration,1),'sec',flush=True)
        return {'chapter':ch,'segment':seq,'words':wc,'seconds':duration,'bytes':dest.stat().st_size}

async def speak_all():
    sem=asyncio.Semaphore(3)
    return await asyncio.gather(*(work(*args,sem) for args in jobs))

manifest=asyncio.run(speak_all())

# Assemble a clean dry read: consistent pauses, voice only, no constant music over dialogue.
SR=44100
book=AudioSegment.silent(duration=300,frame_rate=SR).set_channels(1)
meta=[]
chapter_outputs=[]
for chapter in range(1,9):
    start=len(book)
    chapter_a=AudioSegment.silent(duration=130,frame_rate=SR).set_channels(1)
    for seq,path in enumerate(chapter_files[chapter]):
        wav=AudioSegment.from_file(path,format='mp3').set_frame_rate(SR).set_channels(1)
        if len(wav)<1000:raise ValueError(f'Broken speech: {path}')
        chapter_a+=wav
        if seq!=len(chapter_files[chapter])-1:
            chapter_a+=AudioSegment.silent(duration=320,frame_rate=SR)
    title=HEADERS[chapter-1].replace('CHAPTER ','CHAPTER ',1)
    chapter_outputs.append((chapter,title,len(chapter_a)))
    # Preserve narration dynamics; correct only dangerously loud chapters.
    if chapter_a.max_dBFS> -2.2:
        chapter_a=chapter_a.apply_gain(-2.2-chapter_a.max_dBFS)
    book+=chapter_a
    if chapter<8:
        book+=AudioSegment.silent(duration=1050,frame_rate=SR)
    else:
        book+=AudioSegment.silent(duration=1800,frame_rate=SR)
    meta.append({'chapter':chapter,'title':title,'start_ms':start,'length_ms':len(chapter_a)})
    print('ASSEMBLED',chapter,round(len(chapter_a)/1000,1),'secs',flush=True)
assert 20*60000 < len(book) < 60*60000, f'Bad duration {len(book)/1000}s'
master=OUT/'No_Floor_Clean_Narration.mp3'
book.export(master,format='mp3',bitrate='160k',tags={
 'title':'NO FLOOR: The Walk That Stopped New York',
 'artist':'Original narrative nonfiction',
 'album':'NO FLOOR - Complete Audiobook',
 'genre':'Audiobook',
 'comment':'An original narrative account of the August 7, 1974 World Trade Center high-wire walk.'
},parameters=['-ar','44100','-ac','1'])
subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(master),'-f','null','-'],check=True,timeout=200)
info={'title':'NO FLOOR','voice':VOICE,'rate':RATE,'total_ms':len(book),'chapters':meta,'chunks':manifest,'words':len(STORY.split()),'audio_bytes':master.stat().st_size}
(OUT/'manifest.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
print('FULLY VERIFIED',round(len(book)/60000,2),'MINUTES',master.stat().st_size,'BYTES',flush=True)