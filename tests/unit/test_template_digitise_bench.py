"""Template-digitise model bench (spec 2026-10-31 Part B1): scorer, gate, plan, env.

Fakes only: no network, no model, no Pillow / pymupdf / numpy. The modules are
loaded by path under tdb_* names (bare `run` / `score` are other tools' tests).
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = os.path.join(REPO, "tools", "template-digitise-bench")


def _load(name):
    key = "tdb_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(BENCH, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load("score")
R = _load("run")
RASTER = _load("raster")
W = R._load_web("ledger")

TRUTH = {"sections": [
    {"title": "Site set-up", "items": ["1. Site fence erected and signed",
                                       "2. Site office established with first aid kit",
                                       "3. Toilets provided for all workers on site"]},
    {"title": "Hazards", "items": ["a) Hazard board up to date",
                                   "b) Underground services located and marked"]}]}


def _pred(*items, title="Site set-up"):
    return [{"title": title, "items": list(items)}]


# ---------------------------------------------------------------- scorer

def test_exact_copy_scores_full():
    m = S.score(TRUTH, TRUTH["sections"])
    assert (m["recall"], m["invented"], m["wording"], m["missed"]) == (1.0, 0, 0, 0)
    assert m["sec_titles_ok"] == 2 and m["sec_truth"] == 2


def test_paraphrase_is_a_wording_change_not_an_invention():
    pred = [{"title": "Site set-up", "items": [
        "Site fence erected and signed",
        "Site office established with first aid kit",
        "Portaloos supplied for every worker",                # a loose paraphrase of 3
    ]}, {"title": "Hazards", "items": ["Hazard board up to date",
                                       "Underground services located and marked"]}]
    m = S.score(TRUTH, pred)
    # Under 0.8 (ratio 0.53): item 3 is not matched, but the extra paraphrases it
    # (>= 0.5): a wording change, not an invention.
    assert m["recall"] == pytest.approx(4 / 5)
    assert (m["paraphrased"], m["invented"], m["missed"], m["wording"]) == (1, 0, 0, 1)
    # An unrelated extra in its place (ratio 0.13) is an invention, and 3 is missed.
    pred[0]["items"][2] = "Crane hook latch checked"
    m = S.score(TRUTH, pred)
    assert (m["paraphrased"], m["invented"], m["missed"], m["wording"]) == (0, 1, 1, 0)
    # A close rewording (>= 0.8) is a matched wording change.
    pred[0]["items"][2] = "Toilet provided for all the workers onsite"
    m = S.score(TRUTH, pred)
    assert m["recall"] == 1.0 and m["invented"] == 0 and m["wording"] == 1


def test_paraphrase_pairing_at_half_ratio():
    pred = _pred("Site fence erected and signed",
                 "Site office established with first aid kit",
                 "Toilets for workers") + \
        [{"title": "Hazards", "items": ["Hazard board up to date",
                                        "Underground services located and marked"]}]
    m = S.score(TRUTH, pred)
    assert m["recall"] == pytest.approx(4 / 5)       # under 0.8: not a match
    assert m["paraphrased"] == 1 and m["invented"] == 0 and m["missed"] == 0
    assert m["wording"] == 1


def test_true_invention_is_counted():
    pred = TRUTH["sections"] + [{"title": "Extra", "items": [
        "Crane operator holds a current certificate of competency"]}]
    m = S.score(TRUTH, pred)
    assert m["recall"] == 1.0 and m["invented"] == 1 and m["wording"] == 0


def test_dropped_item_with_no_extra_is_missed_not_invented():
    pred = _pred("Site fence erected and signed") + TRUTH["sections"][1:]
    m = S.score(TRUTH, pred)
    assert m["missed"] == 2 and m["invented"] == 0
    assert m["recall"] == pytest.approx(3 / 5)


def test_probe_must_match_a_truth_item():
    assert S.matches_any(TRUTH, _pred("Site fence erected and signed"))
    assert not S.matches_any(TRUTH, _pred("I cannot see any image in this message"))
    assert not S.matches_any(TRUTH, [])


def test_bad_shapes_do_not_raise():
    m = S.score(TRUTH, [{"title": None, "items": ["Hazard board up to date", 3]}, "junk"])
    assert m["pred_items"] == 1


# ---------------------------------------------------------------- gate and winner

def _rows(arm, recall=1.0, invented=0, wording=0, latency=20.0, reps=3, inputs=None,
          empty=False, cut=False):
    inputs = inputs or list(R.IMAGE_INPUTS)
    return [{"arm": arm, "input": i, "rep": r, "latency_s": latency, "cut_off": cut,
             "ok": not empty, "recall": recall, "invented": invented, "wording": wording,
             "sec_titles_ok": 2, "sec_truth": 2, "pred_items": 0 if empty else 5,
             "empty": empty, "cost_usd": 0.001}
            for r in range(1, reps + 1) for i in inputs]


def _summ(arm, **kw):
    reps = kw.get("reps", 3)
    inputs = kw.get("inputs") or list(R.IMAGE_INPUTS)
    return S.arm_summary(_rows(arm, **kw), inputs, reps)


def test_gate():
    assert _summ("muse_low")["passes"]
    assert not _summ("muse_low", recall=0.97)["passes"]
    assert not _summ("muse_low", invented=1)["passes"]
    assert not _summ("muse_low", latency=61.0)["passes"]
    assert not _summ("muse_low", empty=True)["passes"]
    assert not _summ("muse_low", cut=True)["passes"]
    # 2 of 3 reps is not 3 / 3.
    s = S.arm_summary(_rows("muse_low", reps=2), list(R.IMAGE_INPUTS), 3)
    assert not s["complete"] and not s["passes"]
    # The shipped gemini runs 1 rep: a baseline, never a pass.
    assert not _summ("gemini_shipped", reps=1)["passes"]


def test_winner_ordering():
    arms = {"muse_low": _summ("muse_low", wording=1, latency=30),
            "muse_medium": _summ("muse_medium", wording=0, latency=50),
            "deepseek_off": _summ("deepseek_off", recall=0.99, latency=5),
            "deepseek_low": _summ("deepseek_low", invented=1, latency=1),
            "gemini_shipped": _summ("gemini_shipped", reps=1)}
    d = S.pick(arms)
    # pass > recall > fewer wording changes > p50
    assert d["ranking"] == ["muse_medium", "muse_low", "deepseek_off", "deepseek_low"]
    assert d["winner"] == "muse_medium"
    arms["muse_low"] = _summ("muse_low", wording=0, latency=10)
    assert S.pick(arms)["winner"] == "muse_low"


def test_no_pass_stays_on_gemini_and_pdf_coupling():
    none = {a: _summ(a, recall=0.9) for a in S.CANDIDATES}
    d = S.pick(none)
    assert d["winner"] is None and d["image"].startswith("gemini")
    ds = {a: _summ(a, recall=0.9) for a in S.CANDIDATES}
    ds["deepseek_off"] = _summ("deepseek_off")
    ds["muse_pdf_low"] = _summ("muse_pdf_low", inputs=list(R.PDF_INPUTS))
    d = S.pick(ds)
    assert d["winner"] == "deepseek_off" and d["pdf"] == "muse_pdf_low"
    ds["muse_pdf_low"] = _summ("muse_pdf_low", inputs=list(R.PDF_INPUTS), invented=1)
    assert "owner's call" in S.pick(ds)["pdf"]


# ---------------------------------------------------------------- plan, env, PR-2

NO_PR2 = {"reasoning": False, "provider": False}
PR2 = {"reasoning": True, "provider": True}


def test_plan_is_58_reads_plus_3_probes():
    p = R.plan("nowhere", PR2)
    assert p["reads"] == 58 == 4 * 12 + 4 + 6
    assert [a["reads"] for a in p["arms"]] == [12, 12, 12, 12, 4, 6]
    assert len(p["probes"]) == 3
    assert {x["key"] for x in p["probes"]} == {
        "meta/muse-spark-1.3-contributor", "deepseek/deepseek-v4.1-flash@coreweave/fp8",
        "google/gemini-3.8-flash"}
    assert p["runnable_reads"] == 58 and p["usd_upper"] < 10.0
    q = R.plan("nowhere", NO_PR2)
    assert q["reads"] == 58 and q["runnable_reads"] == 4 and q["runnable_probes"] == 1
    assert [a["id"] for a in q["arms"] if a["runnable"]] == ["gemini_shipped"]


def test_arms_as_spec():
    arms = {a["id"]: a for a in R.ARMS}
    assert (arms["muse_low"]["reasoning"], arms["muse_medium"]["reasoning"]) == ("low", "medium")
    assert arms["deepseek_off"]["reasoning"] == "off"
    assert arms["deepseek_off"]["provider"] == arms["deepseek_low"]["provider"] == "coreweave/fp8"
    assert arms["gemini_shipped"]["model"] == "google/gemini-3.8-flash"
    assert arms["gemini_shipped"]["reasoning"] == arms["gemini_shipped"]["provider"] == ""
    assert arms["muse_pdf_low"]["inputs"] == R.PDF_INPUTS
    # muse refuses reasoning off: no muse arm sends it.
    assert all(a["reasoning"] != "off" for a in R.ARMS if a["model"] == R.MUSE)


def test_arm_env_is_built_not_inherited():
    arm = {a["id"]: a for a in R.ARMS}["deepseek_off"]
    base = {"PATH": "p", "SYSTEMROOT": "s", "QWEN_API_KEY": "sk-PROD",
            "DASHSCOPE_API_KEY": "sk-DS", "LLM_FALLBACK_MODELS": "x", "TMP": "t"}
    env = R.arm_env(arm, "sk-or-BENCH", base)
    assert env["LLM_PROVIDER"] == "qwen"
    assert env["QWEN_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["QWEN_API_KEY"] == "sk-or-BENCH"
    assert env["TEMPLATE_DIGITISE_MODEL"] == "deepseek/deepseek-v4.1-flash"
    assert env["TEMPLATE_DIGITISE_REASONING"] == "off"
    assert env["TEMPLATE_DIGITISE_PROVIDER"] == "coreweave/fp8"
    assert env["TEMPLATE_DIGITISE_FALLBACK_MODELS"] == ""
    assert "DASHSCOPE_API_KEY" not in env and "LLM_FALLBACK_MODELS" not in env
    assert (env["PATH"], env["SYSTEMROOT"], env["TMP"]) == ("p", "s", "t")


def test_capabilities_read_from_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "llm_utils.py").write_text(
        "def call_llm_parts(parts, *, model, max_tokens, deadline=None):\n    pass\n",
        encoding="utf-8")
    (src / "template_vision.py").write_text("X = 1\n", encoding="utf-8")
    assert R.capabilities(str(src)) == NO_PR2
    (src / "llm_utils.py").write_text(
        "def call_llm_parts(parts, *, model, max_tokens, reasoning=None, provider=None):\n"
        "    pass\n", encoding="utf-8")
    assert R.capabilities(str(src)) == NO_PR2          # template_vision does not read them
    (src / "template_vision.py").write_text(
        "A = 'TEMPLATE_DIGITISE_REASONING'\nB = 'TEMPLATE_DIGITISE_PROVIDER'\n",
        encoding="utf-8")
    assert R.capabilities(str(src)) == PR2
    assert R.capabilities(str(tmp_path / "missing")) == NO_PR2
    # On the real src the source read agrees with the live signature.
    import inspect
    import llm_utils
    params = inspect.signature(llm_utils.call_llm_parts).parameters
    tv = open(os.path.join(REPO, "src", "template_vision.py"), encoding="utf-8").read()
    assert R.capabilities() == {
        "reasoning": "reasoning" in params and "TEMPLATE_DIGITISE_REASONING" in tv,
        "provider": "provider" in params and "TEMPLATE_DIGITISE_PROVIDER" in tv}


def _inputs(tmp_path):
    d = tmp_path / "inputs"
    d.mkdir()
    for doc, n in (("bicl", 3), ("penina", 10)):
        (d / f"{doc}.pdf").write_bytes(b"%PDF")
        for i in range(1, n + 1):
            (d / f"{doc}_p{i}_clean.png").write_bytes(b"png")
            (d / f"{doc}_p{i}_photo.jpg").write_bytes(b"jpg")
    return d


def test_pages_in_page_order(tmp_path):
    d = _inputs(tmp_path)
    pages = R.pages_of(str(d), "penina_photo")
    assert [os.path.basename(p) for p, _ in pages][:3] == [
        "penina_p1_photo.jpg", "penina_p2_photo.jpg", "penina_p3_photo.jpg"]
    assert pages[-1][0].endswith("penina_p10_photo.jpg") and pages[0][1] == "image/jpeg"
    assert R.pages_of(str(d), "bicl_pdf") == [(str(d / "bicl.pdf"), "application/pdf")]
    assert R.page_count(str(d), "penina_pdf") == 10


def _args(tmp_path, *extra):
    d = _inputs(tmp_path) if not (tmp_path / "inputs").exists() else tmp_path / "inputs"
    return R.validate(["--inputs", str(d), "--out", str(tmp_path / "out"), *extra],
                      repo_root=REPO)


def test_dry_run_prints_58_and_makes_no_call(tmp_path):
    spawned = []
    out = io.StringIO()
    code = R.run(_args(tmp_path, "--dry-run"), caps=PR2, spawn=lambda *a: spawned.append(a),
                 stdout=out)
    text = out.getvalue()
    assert code == 0 and spawned == []
    assert "58 reads (+3 step-0 probes)" in text and "runnable now: 58 reads" in text
    out = io.StringIO()
    assert R.run(_args(tmp_path, "--dry-run"), caps=NO_PR2, spawn=None, stdout=out) == 0
    assert out.getvalue().count("needs PR-2") == 5
    assert "runnable now: 4 reads (+1 probes)" in out.getvalue()
    assert not (tmp_path / "out" / "arms").exists()


def test_validate_refusals(tmp_path):
    d = _inputs(tmp_path)
    with pytest.raises(SystemExit):                    # live without the bench key
        R.validate(["--inputs", str(d), "--out", str(tmp_path / "o"), "--confirm-calls", "4"],
                   repo_root=REPO)
    with pytest.raises(SystemExit):
        R.validate(["--inputs", str(d), "--out", os.path.join(REPO, "x"), "--dry-run"],
                   repo_root=REPO)
    with pytest.raises(SystemExit):
        R.validate(["--inputs", str(d), "--out", "C:/Users/camil/Dropbox/x", "--dry-run"],
                   repo_root=REPO)


def test_dry_run_refuses_over_the_openrouter_room(tmp_path):
    a = _args(tmp_path, "--dry-run")
    W.add_openrouter_spend(a.ledger, 9.9)
    out = io.StringIO()
    assert R.run(a, caps=PR2, spawn=None, stdout=out) == 2
    assert "refused: openrouter upper" in out.getvalue()


def _fake_spawn(tmp_path, probe_ok=True, calls=None):
    """Plays the worker: writes one result file per read of the job."""
    def spawn(cmd, env):
        assert cmd[2] == "--worker"
        calls.append((cmd, env)) if calls is not None else None
        with open(cmd[3], encoding="utf-8") as f:
            job = json.load(f)
        os.makedirs(job["out_dir"], exist_ok=True)
        for rd in job["reads"]:
            secs = (TRUTH_BICL["sections"] if rd["input"].startswith("bicl")
                    else TRUTH_PENINA["sections"])
            if job["probe"] and not probe_ok:
                secs = [{"title": "", "items": ["nothing here"]}]
            res = {"arm": job["arm"]["id"], "input": rd["input"],
                   "doc": rd["input"].split("_")[0], "rep": rd["rep"], "ok": True,
                   "error": None, "cut_off": False, "latency_s": 12.0, "calls": [],
                   "cost_usd": 0.001, "sections": secs, "probe": job["probe"]}
            with open(os.path.join(job["out_dir"], f"{rd['input']}_rep{rd['rep']}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(res, f)
        return 0
    return spawn


TRUTH_BICL, TRUTH_PENINA = S.load_truths()["bicl"], S.load_truths()["penina"]


def test_live_without_pr2_runs_only_the_baseline(tmp_path):
    calls = []
    kf = tmp_path / "or.key"
    kf.write_text("sk-or-BENCH-SENTINEL", encoding="utf-8")
    a = _args(tmp_path, "--confirm-calls", "4", "--openrouter-key-file", str(kf))
    out = io.StringIO()
    code = R.run(a, caps=NO_PR2, spawn=_fake_spawn(tmp_path, calls=calls), stdout=out)
    text = out.getvalue()
    assert code == 0
    assert [os.path.basename(c[0][3]) for c in calls] == ["probe_gemini_shipped.json",
                                                         "gemini_shipped.json"]
    assert all(c[1]["QWEN_API_KEY"] == "sk-or-BENCH-SENTINEL" for c in calls)
    assert text.count("skipped, needs PR-2") == 5
    s = json.loads((tmp_path / "out" / "summary.json").read_text("utf-8"))
    assert list(s["arms"]) == ["gemini_shipped"] and s["arms"]["gemini_shipped"]["reads"] == 4
    assert s["decision"]["winner"] is None
    blob = text + (tmp_path / "out" / "summary.json").read_text("utf-8")
    assert "SENTINEL" not in blob
    for sec in TRUTH_BICL["sections"]:                    # no item text on stdout / summary
        for item in sec["items"][:3]:
            assert item not in blob


def test_live_with_pr2_runs_58_and_skips_a_failed_probe(tmp_path):
    calls = []
    a = _args(tmp_path, "--confirm-calls", "58", "--openrouter-key-file",
              str(_keyfile(tmp_path)))
    out = io.StringIO()
    assert R.run(a, caps=PR2, spawn=_fake_spawn(tmp_path, calls=calls), stdout=out) == 0
    jobs = [os.path.basename(c[0][3]) for c in calls]
    assert sum(1 for j in jobs if j.startswith("probe_")) == 3 and len(jobs) == 3 + 6
    s = json.loads((tmp_path / "out" / "summary.json").read_text("utf-8"))
    assert sum(x["reads"] for x in s["arms"].values()) == 58
    assert s["arms"]["muse_low"]["passes"] and s["decision"]["winner"] == "muse_low"
    calls2 = []
    a = _args(tmp_path, "--confirm-calls", "58", "--openrouter-key-file",
              str(_keyfile(tmp_path)))
    a.out = str(tmp_path / "out2")
    out = io.StringIO()
    R.run(a, caps=PR2, spawn=_fake_spawn(tmp_path, probe_ok=False, calls=calls2), stdout=out)
    assert out.getvalue().count("skipped, step-0 probe failed") == 6
    assert len(calls2) == 3


def test_confirm_mismatch_sends_nothing(tmp_path):
    a = _args(tmp_path, "--confirm-calls", "58", "--openrouter-key-file",
              str(_keyfile(tmp_path)))
    out = io.StringIO()
    assert R.run(a, caps=NO_PR2, spawn=None, stdout=out) == 2       # runnable is 4
    assert "!= 4 runnable reads" in out.getvalue()


def _keyfile(tmp_path):
    kf = tmp_path / "or.key"
    kf.write_text("sk-or-BENCH", encoding="utf-8")
    return kf


def test_call_cost_never_zero_without_usage_cost():
    m = R.DEEPSEEK
    assert R.call_cost(m, [{"status": 200, "usage": {"cost": 0.002}}], 1, 1) == 0.002
    up = R.call_cost(m, [{"status": 200, "usage": {"prompt_tokens": 1000,
                                                    "completion_tokens": 100}}], 9, 9)
    assert up == pytest.approx((1000 * 0.13 + 100 * 0.65) / 1e6)
    assert R.call_cost(m, [{"status": None}], 1000, 100) == pytest.approx(up)
    assert R.call_cost(m, [{"status": 502, "usage": {}}], 1000, 100) == 0.0


def test_import_needs_no_image_libraries():
    code = ("import sys, importlib.util as u\n"
            "for n in ('run', 'score', 'raster'):\n"
            "    s = u.spec_from_file_location('x_' + n, r'%s/' + n + '.py')\n"
            "    m = u.module_from_spec(s); s.loader.exec_module(m)\n"
            "print(sorted(k for k in ('PIL', 'pymupdf', 'numpy', 'llm_utils') if k in sys.modules))"
            % BENCH)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert r.stdout.strip() == "[]", r.stderr[-500:]
    assert RASTER.page_names("bicl", 2) == [("bicl_p1_clean.png", "bicl_p1_photo.jpg"),
                                            ("bicl_p2_clean.png", "bicl_p2_photo.jpg")]


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell only")
def test_convert_refuses_dropbox_and_the_source_folder(tmp_path):
    ps = os.path.join(BENCH, "convert.ps1")
    for out in ("C:\\Users\\camil\\Dropbox\\bench-out", str(tmp_path)):
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            ps, "-Src", str(tmp_path / "x.docx"), "-Out", out],
                           capture_output=True, text=True)
        assert r.returncode == 2 and "refused" in r.stderr
    assert not os.path.exists("C:\\Users\\camil\\Dropbox\\bench-out")


class _Resp:
    def __init__(self, status, data):
        self.status, self.data = status, json.dumps(data).encode("utf-8")


def test_worker_reads_through_template_vision_and_charges_the_pool(tmp_path, monkeypatch,
                                                                    capsys):
    sys.path.insert(0, os.path.join(REPO, "src"))
    import llm_utils
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm_utils, "QWEN_API_KEY", "sk-or-test")
    answer = json.dumps({"sections": TRUTH_BICL["sections"]})
    sent = []

    def fake_post(url, body, headers, **kw):
        sent.append(json.loads(body))
        return _Resp(200, {"model": "m", "choices": [{"finish_reason": "stop",
                                                      "message": {"content": answer}}],
                           "usage": {"prompt_tokens": 9000, "completion_tokens": 3000}}), None
    monkeypatch.setattr(llm_utils, "_post_with_retry", fake_post)
    d = _inputs(tmp_path)
    arm = {a["id"]: a for a in R.ARMS}["gemini_shipped"]
    a = _args(tmp_path, "--dry-run")
    job = R._job(a, arm, str(tmp_path / "out" / "arms" / arm["id"]))
    try:
        assert R.worker(job) == 0
    finally:
        monkeypatch.setattr(llm_utils, "_post_with_retry", fake_post)
    assert len(sent) == 4 and "usage" not in sent[0]        # prod payload, unchanged
    assert sent[0]["model"] == "google/gemini-3.8-flash"
    res = json.loads((tmp_path / "out" / "arms" / arm["id"] / "bicl_photo_rep1.json")
                     .read_text("utf-8"))
    assert res["ok"] and res["calls"] == [{"status": 200, "usage": {
        "prompt_tokens": 9000, "completion_tokens": 3000}}]
    per = (9000 * 0.825 + 3000 * 4.125) / 1e6
    assert res["cost_usd"] == pytest.approx(per)
    assert W.openrouter_spent(a.ledger) == pytest.approx(4 * per)
    out = capsys.readouterr().out
    assert "items=47" in out and TRUTH_BICL["sections"][0]["items"][0] not in out
