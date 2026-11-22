#!/usr/bin/env python3
"""Produce all speech for The Part I Sold, six chapters, consistent voices by POV. All segments are verified."""
import asyncio, hashlib, json, os, re, subprocess, sys
from pathlib import Path

BASE=Path(__file__).resolve().parent
OUT=BASE/'speech'
OUT.mkdir(exist_ok=True,parents=True)
RAY_VOICE=os.environ.get('RAY_VOICE','en-US-AndrewMultilingualNeural')
DINA_VOICE=os.environ.get('DINA_VOICE','en-US-AvaMultilingualNeural')
RATE=os.environ.get('RATE','-4%')
source=(BASE/'story.txt').read_text(encoding='utf-8').replace('\r\n','\n')
headings=list(re.finditer(r'(?m)^CHAPTER ([A-Z]+)\. (.+)$',source))
assert len(headings)==6,(len(headings),[m.group() for m in headings])

LIMIT=880

def pack_paragraph(s):
    s=re.sub(r'\s+',' ',s).strip()
    if len(s)<=LIMIT:return [s]
    # never break speech in the middle of a word or sentence
    sentences=re.split(r'(?<=[.!?])\s+(?=[A-Z"\'])',s)
    if len(sentences)==1:
        words=s.split()
        sentences=[]
        buf=[]
        for w in words:
            if len(' '.join(buf+[w]))>LIMIT and buf:
                sentences.append(' '.join(buf));buf=[]
            buf.append(w)
        if buf:sentences.append(' '.join(buf))
    chunks=[];buff=[];size=0
    for sent in sentences:
        if len(sent)>LIMIT:raise ValueError('Single sentence too long: '+sent[:100])
        if buff and size+len(sent)+1>LIMIT:
            chunks.append(' '.join(buff));buff=[];size=0
        buff.append(sent);size+=len(sent)+1
    if buff:chunks.append(' '.join(buff))
    assert len(chunks)>=2,('Expected actual split',len(s))
    return chunks

entries=[]
for ci,m in enumerate(headings,1):
    end=headings[ci].start() if ci<len(headings) else len(source)
    content=source[m.start():end].strip()
    paragraphs=[x.strip() for x in re.split(r'\n\s*\n',content) if x.strip()]
    assert paragraphs[0].startswith('CHAPTER '),paragraphs[0]
    events=[]
    buffer=[]
    def flush(after=200):
        if buffer:
            # Newlines are kept for natural paragraph and dialogue breathing.
            events.append({'kind':'speech','text':'\n\n'.join(buffer),'after_ms':after})
            buffer.clear()
    for index,para in enumerate(paragraphs):
        cue=re.fullmatch(r'\[AUDIO:([A-Z_]+)\]',para)
        if cue:
            flush(340)
            events.append({'kind':'cue','name':cue.group(1)})
            continue
        if index==0:
            flush()
            events.append({'kind':'speech','text':para,'after_ms':570})
            continue
        chunks=pack_paragraph(para)
        for chunk in chunks:
            used=len('\n\n'.join(buffer))
            if buffer and used+len(chunk)+2>LIMIT:
                flush(160)
            buffer.append(chunk)
    flush(190)
    entries.append({'chapter':ci,'heading':m.group(0),'events':events})

jobs=[]
for chapter in entries:
    for i,e in enumerate(chapter['events'],1):
        if e['kind']!='speech':continue
        e['name']=f"ch{chapter['chapter']:02d}_part{i:03d}.mp3"
        jobs.append((e['text'],OUT/e['name'],chapter['chapter'],i))

transcript=' '.join(e['text'] for ch in entries for e in ch['events'] if e['kind']=='speech')
input_transcript=re.sub(r'\s+',' ',re.sub(r'\[AUDIO:[A-Z_]+\]','',source[headings[0].start():])).strip()
# token-by-token fidelity check, punctuation included in speech source
assert re.sub(r'\s+',' ',transcript)==input_transcript,('Transcript differs from original',len(transcript),len(input_transcript))
print('BEGIN',len(entries),'chapters',len(jobs),'speech parts',len(transcript.split()),'words','narrators',RAY_VOICE,DINA_VOICE,flush=True)

def duration(path):
    return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],text=True,timeout=25).strip())

async def synth(text,dest,ch,i):
    import edge_tts
    voice = DINA_VOICE if ch==6 else RAY_VOICE
    tmp=dest.with_suffix('.pending.mp3')
    for attempt in range(9):
        try:
            if tmp.exists():tmp.unlink()
            tts=edge_tts.Communicate(text=text,voice=voice,rate=RATE,volume='+0%',pitch='-1Hz')
            await asyncio.wait_for(tts.save(str(tmp)), timeout=110)
            if tmp.stat().st_size<1800: raise IOError('unreasonably small audio')
            seconds=await asyncio.to_thread(duration,tmp)
            if seconds<1.2: raise IOError('unreasonably short audio')
            tmp.replace(dest)
            return seconds
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            print('RETRY',ch,i,attempt+1,type(exc).__name__,str(exc)[:120],flush=True)
            await asyncio.sleep(min(14,2+attempt*2))
    # fallback: split a difficult segment at sentence boundary, *same* voice
    units=re.split(r'(?<=[.!?])\s+(?=[A-Z"\'])',text)
    if len(units)<2:
        raise RuntimeError(f'Could not render chapter {ch} part {i}; refusing substitute narrator')
    mid=len(units)//2
    a=dest.with_suffix('.half1.mp3');b=dest.with_suffix('.half2.mp3')
    await synth(' '.join(units[:mid]),a,ch,f'{i}a')
    await synth(' '.join(units[mid:]),b,ch,f'{i}b')
    from pydub import AudioSegment
    joined=AudioSegment.from_file(a)+AudioSegment.silent(duration=90)+AudioSegment.from_file(b)
    joined.export(dest,format='mp3',bitrate='160k')
    a.unlink();b.unlink()
    return duration(dest)

async def worker(job,sem):
    text,dest,ch,i=job
    async with sem:
        if dest.exists() and dest.stat().st_size>1500:
            sec=duration(dest)
        else:sec=await synth(text,dest,ch,i)
        words=len(text.split())
        speed=words/(sec/60)
        if words>=20 and not 70<speed<260:raise ValueError(f'suspicious narration duration ch{ch} {i}: {speed:.1f}wpm')
        print('OK',ch,i,words,'words',round(sec,2),'sec',round(speed,1),'wpm',flush=True)
        return {'ch':ch,'i':i,'voice':DINA_VOICE if ch==6 else RAY_VOICE,'word_count':words,'duration_s':round(sec,3),'bytes':dest.stat().st_size,'file':dest.name}

async def main():
    sem=asyncio.Semaphore(3)
    result=await asyncio.gather(*(worker(job,sem) for job in jobs))
    manifest={'title':'THE PART I SOLD','source_sha256':hashlib.sha256(source.encode()).hexdigest(),'narrators':{'Ray':RAY_VOICE,'Dina':DINA_VOICE},'rate':RATE,'chapters':entries,'parts':result,'words':len(transcript.split())}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('COMPLETE',len(result),'segments',sum(x['duration_s'] for x in result),'speech seconds',flush=True)

if __name__=='__main__':asyncio.run(main())