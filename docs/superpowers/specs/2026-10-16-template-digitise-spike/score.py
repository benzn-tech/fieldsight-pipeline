import json,re,html,difflib,glob,bisect
from html.parser import HTMLParser
YN=re.compile(r'yes.*no.*n/?a',re.I|re.S)
HDR=re.compile(r'^(yes\s*/\s*no|complies)',re.I)
LABEL=re.compile(r'^\s*(\d+[.)]|[a-z]{1,3}[.)])\s+',re.I)
class T(HTMLParser):
    def __init__(s): super().__init__(); s.rows=[]; s.cell=None
    def handle_starttag(s,t,a):
        if t=='tr': s.rows.append([])
        if t=='td' or t=='th': s.cell=[]
    def handle_endtag(s,t):
        if t in('td','th') and s.cell is not None: s.rows[-1].append(' '.join(''.join(s.cell).split())); s.cell=None
    def handle_data(s,d):
        if s.cell is not None: s.cell.append(d)
def ade_to_sections(md):
    secs=[]
    for tb in re.findall(r'<table.*?</table>',md,re.S):
        p=T(); p.feed(tb)
        for r in p.rows:
            r=[c for c in r]
            if len(r)>1 and YN.search(r[1]) and not HDR.match(r[1]):
                if not secs: secs.append({"title":"","items":[]})
                secs[-1]["items"].append(LABEL.sub('',r[0]))
            elif len(r)>1 and HDR.match(r[1]):
                t=LABEL.sub('',r[0])
                if t.upper()=='ACTIVITY & REQUIREMENTS': continue
                if secs and secs[-1]["title"]==t: continue   # header repeated after page break
                secs.append({"title":t,"items":[]})
            elif len(r)==1 and r[0] and not re.search(r'comment|signature|confirmation|non-conformance',r[0],re.I):
                secs.append({"title":r[0],"items":[]})
    return {"sections":[s for s in secs if s["items"]]}
def norm(s): return ' '.join(LABEL.sub('',html.unescape(s)).replace('\u2019',"'").lower().split())
def raw(s): return " ".join(LABEL.sub("",html.unescape(s)).split())
def lis(a):
    t=[]
    for x in a:
        i=bisect.bisect_left(t,x)
        if i==len(t): t.append(x)
        else: t[i]=x
    return len(t)
def score(truth,pred):
    T=[(norm(i),norm(s["title"])) for s in truth["sections"] for i in s["items"]]
    P=[(norm(i),norm(s.get("title",""))) for s in pred["sections"] for i in s["items"]]
    used=set(); match=[]; exact=0; errs=[]; sec_ok=0
    for ti,(t,ts) in enumerate(T):
        best=None;br=0
        for pi,(p,ps) in enumerate(P):
            if pi in used: continue
            r=1.0 if p==t else difflib.SequenceMatcher(None,t,p).ratio()
            if r>br: br,best=r,pi
            if r==1.0: break
        if best is not None and br>=0.8:
            used.add(best); match.append(best)
            if br==1.0: exact+=1
            else: errs.append(("WORDING",t,P[best][0]))
            if P[best][1]==ts: sec_ok+=1
        else: errs.append(("MISSED",t,None))
    for pi,(p,ps) in enumerate(P):
        if pi not in used: errs.append(("EXTRA",None,p))
    RT={raw(i) for s_ in truth["sections"] for i in s_["items"]}; RP=[raw(i) for s_ in pred["sections"] for i in s_["items"]]
    strict=sum(1 for x in RP if x in RT)/len(T)
    tt=[norm(s["title"]) for s in truth["sections"]]; pt=[norm(s.get("title","")) for s in pred["sections"]]
    return {"truth_items":len(T),"pred_items":len(P),"recall":len(match)/len(T),"precision":len(match)/max(1,len(P)),
            "exact":exact/len(T),"strict":strict,"order":lis(match)/max(1,len(match)),
            "sec_titles":f"{sum(1 for x in tt if x in pt)}/{len(tt)} (pred {len(pt)})","item_in_right_section":sec_ok/len(T),"errs":errs}
if __name__=="__main__":
    calls={c["run"]+("_ade" if c["vendor"]=="ade" else "_llm"):c for c in json.load(open("calls.json")) if c.get("ok")}
    rows=[]
    for doc in ["bicl","penina"]:
        truth=json.load(open(f"truth_{doc}.json",encoding="utf-8"))
        runs=[("docx","python-docx",truth)]
        for v in ["digital","photo"]+(["harsh"] if doc=="penina" else []):
            a=ade_to_sections(open(f"ade_{doc}_{v}.md",encoding="utf-8").read())
            json.dump(a,open(f"ade_{doc}_{v}.sections.json","w",encoding="utf-8"),indent=1,ensure_ascii=False); runs.append((v,"ADE",a))
        for v in ["clean","photo"]+(["harsh"] if doc=="penina" else []):
            runs.append((v,"Gemini",json.load(open(f"llm_{doc}_{v}.json",encoding="utf-8"))))
        for v,route,pred in runs:
            s=score(truth,pred); key=f"{doc}_{v}_"+("ade" if route=="ADE" else "llm")
            c=calls.get(key,{}); s.update(doc=doc,variant=v,route=route,sec=c.get("sec"),cost=(c.get("usage") or {}).get("cost"),credits=(c.get("meta") or {}).get("credit_usage") if isinstance(c.get("meta"),dict) else None)
            rows.append(s)
    json.dump(rows,open("scores.json","w",encoding="utf-8"),indent=1,ensure_ascii=False)
    print(f"{'doc':7}{'route':8}{'input':8}{'items':>8}{'recall':>8}{'prec':>7}{'exact':>7}{'strict':>7}{'order':>7}{'secTitle':>14}{'inSec':>7}{'sec':>6}")
    for s in rows:
        print(f"{s['doc']:7}{s['route']:8}{s['variant']:8}{s['pred_items']:>4}/{s['truth_items']:<3}{s['recall']:>8.3f}{s['precision']:>7.3f}{s['exact']:>7.3f}{s['strict']:>7.3f}{s['order']:>7.3f}{s['sec_titles']:>14}{s['item_in_right_section']:>7.3f}{str(s['sec']):>6}")
    for s in rows:
        if s["errs"]:
            print("\n##",s["doc"],s["route"],s["variant"])
            for e in s["errs"][:12]: print("  ",e)
