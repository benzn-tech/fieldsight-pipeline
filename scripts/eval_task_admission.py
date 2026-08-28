"""Score the task-admission prompt against the user's per-item ground truth.

The strategy-session A/B showed 3/0/2/2 tasks across four runs of ONE prompt, so
a single run of each variant says nothing. This runs each variant N times and
scores every run against what the user actually judged, item by item.
"""
import os, sys, io, json, time, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'C:/Users/camil/Dropbox/worktrees/brief-fuse/src')
os.environ.setdefault('S3_BUCKET', 'fieldsight-data-509194952652')
os.environ.setdefault('AWS_DEFAULT_REGION', 'ap-southeast-2')
import boto3

env = boto3.client('lambda', region_name='ap-southeast-2').get_function_configuration(
    FunctionName='fieldsight-prod-extract-session')['Environment']['Variables']
for k in ('LLM_PROVIDER', 'QWEN_API_KEY', 'QWEN_BASE_URL', 'QWEN_MODEL', 'LLM_TEMPERATURE'):
    if env.get(k):
        os.environ[k] = env[k]
os.environ['LLM_HTTP_TIMEOUT'] = '540'

import lambda_extract_session as ex, session_brief, llm_utils

V2_RULE_START = "5. **A task needs TWO people"
V2_RULE_END = "6. Write in the language"
V1_RULE = """5. **tasks: only what a specific person could finish and tick off.** Test the
   verb. "consult Xiao Han", "replace the damaged doors", "call the electrician"
   are tasks. "focus on X", "consider Y", "explore Z" are directions -- nobody can
   ever tick them, and they belong in a bullet. A discussion that reached no act
   yields NO tasks; an empty array is correct, and two real tasks are worth more
   than six invented ones, because invented ones bury the real ones.

"""

# What the user judged, item by item, on this session.
TRUE_ITEMS = {
    'A Outlook 日历集成':      [r'outlook', r'ics', r'日历', r'calendar'],
    'B AI 知识库':             [r'knowledge base', r'知识库', r'术语', r'建筑.*库', r'ai.*librar'],
    'C 给 James 发设备':       [r'james', r'南岛', r'south island'],
    'D 周五 2 点 downtown':    [r'downtown', r'proposal review', r'星期五', r'周五', r'friday'],
    'E 两台设备给 Clement':    [r'clement'],
}
FALSE_ITEMS = {
    'F Josh 权限':   [r'josh'],
    'G Deon Jay 培训': [r'deon'],
    'H AWS credit':  [r'aws'],
}
UNSURE = {'I 无人机跟进': [r'无人机', r'drone', r'right.of.way', r'lindis', r'camera stick']}


def hits(text, pats):
    return any(re.search(p, text, re.I) for p in pats)


def score(tasks):
    blob = [t.get('text', '') + ' ' + (t.get('why') or '') for t in tasks]
    found_true = {k for k, p in TRUE_ITEMS.items() if any(hits(b, p) for b in blob)}
    found_false = {k for k, p in FALSE_ITEMS.items() if any(hits(b, p) for b in blob)}
    found_unsure = {k for k, p in UNSURE.items() if any(hits(b, p) for b in blob)}
    return found_true, found_false, found_unsure


BUCKET, SID = 'fieldsight-data-509194952652', 'sid93396a6ac8434fdf908c25a50cc7e167'
s3 = boto3.client('s3', region_name='ap-southeast-2')
keys = sorted(o['Key'] for o in s3.list_objects_v2(
    Bucket=BUCKET, Prefix='transcripts/Ben_UCPK2/2026-08-27/').get('Contents', []) if SID in o['Key'])
turns, _s, _a = ex.assemble_session_turns(BUCKET, keys)

variant, n_runs = sys.argv[1], int(sys.argv[2])
base = session_brief.build_brief_prompt
if variant == 'v1':
    def build_v1(t):
        p = base(t)
        i, j = p.index(V2_RULE_START), p.index(V2_RULE_END)
        return p[:i] + V1_RULE + p[j:]
    session_brief.build_brief_prompt = build_v1

out = []
for r in range(n_runs):
    t0 = time.perf_counter()
    b = session_brief.brief_from_turns(turns)
    el = time.perf_counter() - t0
    tasks = (b or {}).get('tasks') or []
    ft, ff, fu = score(tasks)
    out.append({'run': r + 1, 'n': len(tasks), 'true': sorted(ft), 'false': sorted(ff),
                'unsure': sorted(fu), 'secs': round(el, 1),
                'tasks': [t.get('text', '') for t in tasks]})
    print(f"[{variant} run{r+1}] {len(tasks)} 条 / {el:.0f}s  真 {len(ft)}/5  假 {len(ff)}/3  存疑 {len(fu)}",
          file=sys.stderr, flush=True)
    for t in tasks:
        print(f"    - {t.get('text','')[:88]}", file=sys.stderr, flush=True)

json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 f'eval_{variant}.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)
tt = sum(len(o['true']) for o in out) / len(out)
tf = sum(len(o['false']) for o in out) / len(out)
print(f"\n== {variant}: 平均 真 {tt:.1f}/5  假 {tf:.1f}/3 ==", file=sys.stderr)
