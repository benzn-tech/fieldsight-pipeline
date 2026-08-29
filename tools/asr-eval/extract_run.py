import json,os,sys,glob,time
sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))  # load_sites/config use repo-relative paths
from transcript_utils import normalize_transcript
import lambda_extract_session as EX, llm_utils
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
FN="ben_ucpk2_2026-08-27_11-06-35_sid93396a6ac8434fdf908c25a50cc7e167_c0000_off0.0_to1596.0_srcwav.json"
which=sys.argv[1]
if which=="batched":
    t=[]
    for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
        n=normalize_transcript(json.load(open(p)),os.path.basename(p))
        if n: t+=[x for x in n['speaker_turns'] if x.get('abs_start')]
    n_seg=14
else:
    n=normalize_transcript(json.load(open(os.path.join(W,"full_transcribe_shaped.json"))),FN)
    t=[x for x in n['speaker_turns'] if x.get('abs_start')]
    n_seg=1
t.sort(key=lambda x:x['abs_start']); t=EX._dedup_turn_boundaries(t)
prompt=EX.build_extraction_prompt("Ben_UCPK2","2026-08-27","sid93396a6ac8434fdf908c25a50cc7e167",t,n_seg)
open(os.path.join(W,f"prompt_{which}.txt"),"w",encoding="utf-8").write(prompt)
print(f"{which}: turns={len(t)} prompt_chars={len(prompt)}",flush=True)
t0=time.time()
raw,err=llm_utils.call_llm(prompt,max_tokens=16384,force_json=True,enable_thinking=True)
el=time.time()-t0
assert raw, err
data=llm_utils.extract_json(raw)
open(os.path.join(W,f"raw_{which}.txt"),"w",encoding="utf-8").write(raw)
json.dump({"elapsed_sec":round(el,1),"data":data},open(os.path.join(W,f"extract_{which}.json"),"w"),ensure_ascii=False,indent=1)
print(f"{which}: LLM {el:.1f}s topics={len(data.get('topics',[]))}")
