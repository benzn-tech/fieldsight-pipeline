import sys,json,time,glob,os,base64,requests
KEY=json.load(open("env.json"))["QWEN_API_KEY"]
MODEL=os.environ.get("M","google/gemini-3.8-flash")
PROMPT="""These page images are a construction site checklist form (one document, pages in order).
Digitise it into a reusable checklist TEMPLATE.
Return STRICT JSON only: {"sections":[{"title":"...","items":["...", "..."]}]}
Rules:
- An item is one checklist row/question that the inspector answers (e.g. with Yes/No/N/A boxes).
- A section is the heading that groups those rows (a section header row or table heading). Keep sections in page order.
- Copy every item's wording EXACTLY as printed (same words, spelling, punctuation, capitalisation, including typos). Join lines that wrap inside one cell with a single space.
- Drop only the leading list label (like "1." or "a)") from titles and items.
- Preserve the original order. Do not invent, merge, split, paraphrase or reorder items.
- Do NOT include: column headers (Yes/No/NA, Comment, Due by...), answers/ticks, filled-in comments, names/dates, signature blocks, page footers, the project details box, or free-text observation registers.
- A table repeated across a page break with the same heading is ONE section."""
log=json.load(open("calls.json"))
for name in sys.argv[1:]:
    doc,variant=name.split("_")
    files=sorted(glob.glob(f"{doc}_p*_{variant}.*"),key=lambda s:int(s.split("_p")[1].split("_")[0]))
    content=[{"type":"text","text":PROMPT}]
    for f in files:
        mime="image/png" if f.endswith(".png") else "image/jpeg"
        content.append({"type":"image_url","image_url":{"url":f"data:{mime};base64,"+base64.b64encode(open(f,"rb").read()).decode()}})
    body={"model":MODEL,"messages":[{"role":"user","content":content}],"temperature":0,"response_format":{"type":"json_object"},"usage":{"include":True}}
    t=time.time()
    try:
        r=requests.post("https://openrouter.ai/api/v1/chat/completions",headers={"Authorization":f"Bearer {KEY}"},json=body,timeout=300)
        dt=time.time()-t; j=r.json()
        if r.status_code!=200 or "choices" not in j: raise Exception(f"{r.status_code} {str(j)[:300]}")
        txt=j["choices"][0]["message"]["content"]
        open(f"llm_{name}.json","w",encoding="utf-8").write(txt)
        u=j.get("usage",{})
        log.append({"vendor":"openrouter","model":j.get("model"),"run":name,"ok":True,"sec":round(dt,1),"pages":len(files),"usage":u})
        print(name,"ok",round(dt,1),len(files),"pages",{k:u.get(k) for k in ["prompt_tokens","completion_tokens","cost"]})
    except Exception as e:
        log.append({"vendor":"openrouter","run":name,"ok":False,"err":str(e)[:300]}); print(name,"ERR",str(e)[:300])
    json.dump(log,open("calls.json","w"),indent=1,default=str)
