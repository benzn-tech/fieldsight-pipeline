"""Report bench, Task 3: cold snapshot render worker (stub snapshots, no git)."""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


snap = _load("rb_snapshot")

X_IN = {"user_folder": "u", "date": "2026-08-27", "session_base": "s",
        "turns": [{"speaker": "spk_0", "text": "hi"}], "n_segments": 1,
        "speaker_names": [{"source_filename": "f1", "speaker_label": "spk_0",
                           "display_name": "Ann"}]}


def _stub(tmp_path, body):
    src = tmp_path / "snap" / "src"
    src.mkdir(parents=True)
    (src / "lambda_extract_session.py").write_text(body, encoding="utf-8")
    return str(src)


def test_worker_filters_kwargs_by_signature(tmp_path):
    src = _stub(tmp_path, (
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):\n"
        "    return ('P:' + user_folder + ':' + str(n_segments), {})\n"))
    assert snap.render(src, "X", X_IN) == "P:u:1"


def test_worker_accepts_str_return_and_names(tmp_path):
    src = _stub(tmp_path, (
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments,\n"
        "                            speaker_names=None, continuity_block=''):\n"
        "    return 'N:' + ','.join(sorted('%s/%s=%s' % (k[0], k[1], v)\n"
        "                                  for k, v in (speaker_names or {}).items()))\n"))
    assert snap.render(src, "X", X_IN) == "N:f1/spk_0=Ann"


def test_worker_is_cold(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    src = _stub(tmp_path, (
        "import os\n"
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):\n"
        "    return os.environ.get('LLM_TEMPERATURE', 'absent') + "
        "os.environ['LLM_REASONING_EFFORT']\n"))
    assert snap.render(src, "X", X_IN) == "absenthigh"


def test_worker_cjk_roundtrip(tmp_path):
    src = _stub(tmp_path, (
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):\n"
        "    return turns[0]['text']\n"))
    inputs = dict(X_IN, turns=[{"speaker": "spk_0", "text": "明天浇筑混凝土"}])
    assert snap.render(src, "X", inputs) == "明天浇筑混凝土"


def test_worker_error_is_render_error(tmp_path):
    src = _stub(tmp_path, (
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):\n"
        "    raise ValueError('boom')\n"))
    with pytest.raises(snap.RenderError) as e:
        snap.render(src, "X", X_IN)
    assert "ValueError" in str(e.value)


def test_worker_t_without_report_facts(tmp_path):
    src = tmp_path / "snap" / "src"
    src.mkdir(parents=True)
    (src / "report_template.py").write_text(
        "SOURCE_BUILTIN = 'builtin'\n"
        "def render_prompt(template, scope, action_items, transcript, source='builtin'):\n"
        "    return template['t'] + '|' + str(len(action_items)) + '|' + transcript\n",
        encoding="utf-8")
    out = snap.render(str(src), "T", {"template": {"t": "TT"}, "scope": {"date": "d"},
                                     "action_items": [{"action": "a"}], "transcript": "x",
                                     "topics": [{"ref": "t0"}]})
    assert out == "TT|1|x"


def test_build_refuses_cache_inside_repo():
    with pytest.raises(ValueError):
        snap.build("HEAD", os.path.join(REPO, "tmp-cache"))


def test_worker_error_never_echoes_exception_text(tmp_path):
    src = _stub(tmp_path, (
        "def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):\n"
        "    raise ValueError('SECRETTEXT from the transcript')\n"))
    with pytest.raises(snap.RenderError) as e:
        snap.render(src, "X", X_IN)
    assert "ValueError" in str(e.value) and "SECRETTEXT" not in str(e.value)
