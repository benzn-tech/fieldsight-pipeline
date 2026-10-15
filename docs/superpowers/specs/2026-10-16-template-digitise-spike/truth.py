import docx,json,re,sys
YN=re.compile(r'^yes\s*no\s*n/?a$',re.I)
HDR=re.compile(r'(yes\s*/\s*no|complies)',re.I)
def ucells(r):
    out=[];prev=None
    for c in r.cells:
        if c._tc is prev: continue
        prev=c._tc; out.append(' '.join(c.text.split()))
    return out
def extract(path):
    d=docx.Document(path); secs=[]
    for t in d.tables:
        rows=[ucells(r) for r in t.rows]
        if not any(len(r)>1 and YN.match(r[1]) for r in rows): continue
        for r in rows:
            if len(r)>1 and YN.match(r[1]):
                if not secs: secs.append({"title":"","items":[]})
                secs[-1]["items"].append(r[0])
            elif len(r)>1 and HDR.search(r[1]):
                # header row: first cell is a section title unless it's a generic column header
                if r[0].upper()!='ACTIVITY & REQUIREMENTS': secs.append({"title":r[0],"items":[]})
            elif len(r)==1 and r[0]:
                secs.append({"title":r[0],"items":[]})
    return {"sections":secs}
for name,p in [("bicl","C:/Users/camil/Dropbox/ADE/12.15 - Site Establishment Checklist (BICL).docx"),("penina","C:/Users/camil/Dropbox/ADE/Penina Site Inspection Checklist.docx")]:
    t=extract(p); json.dump(t,open(f"truth_{name}.json","w",encoding="utf-8"),indent=1,ensure_ascii=False)
    print(name,len(t["sections"]),"sections",sum(len(s["items"]) for s in t["sections"]),"items")
    for s in t["sections"]: print("  ",s["title"],len(s["items"]))
