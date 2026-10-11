"""The ASR keyterms are a table to watch and update, with the file as seed and fallback.

Owner, 2026-10-08: collect the words in a table in AWS so they can be followed
and updated. "Hiab" -- in the file since August -- is the first word that may
need retiring without a deploy.
"""
import importlib.util
import os

import pytest

av = pytest.importorskip("asr_vocabulary")
eu = pytest.importorskip("elevenlabs_utils")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VOCAB = os.path.join(ROOT, "src", "config", "custom_vocabulary_construction_nz.txt")


@pytest.fixture(autouse=True)
def _fresh_cache():
    av._cache.update(at=None, rows=None)
    yield
    av._cache.update(at=None, rows=None)


def _vocab(tmp_path, *terms):
    f = tmp_path / "v.txt"
    f.write_text("Phrase\tSoundsLike\tIPA\tDisplayAs\n" + "\n".join(terms) + "\n", encoding="utf-8")
    return str(f)


def test_THE_a_retired_row_takes_a_word_out_of_the_file_too(tmp_path):
    path = _vocab(tmp_path, "Hiab", "formwork")
    rows = [{"term": "Hiab", "status": "retired"}, {"term": "Hirepool", "status": "active"},
            {"term": "washdown", "status": "candidate"}]
    assert av.keyterms(path, "t", read=lambda name: rows) == ["formwork", "Hirepool"]


def test_the_same_word_is_sent_once_in_its_first_spelling():
    assert av.merge(["Formwork", "reo"], [{"term": "formwork", "status": "active"},
                                          {"term": "REO ", "status": "active"}]) == ["Formwork", "reo"]


def test_the_list_stays_inside_scribe_limits():
    rows = [{"term": "t%04d" % i, "status": "active"} for i in range(1200)]
    rows.append({"term": "x" * 80, "status": "active"})
    out = av.merge([], rows)
    assert len(out) == eu.MAX_KEYTERMS and all(len(t) <= eu.MAX_KEYTERM_LEN for t in out)


def test_no_table_is_the_file_exactly_as_before(tmp_path):
    path = _vocab(tmp_path, "GIB", "dwang")
    assert av.keyterms(path, "") == eu.load_keyterms(path) == ["GIB", "dwang"]


def test_the_table_is_read_once_per_five_minutes(tmp_path):
    path, calls = _vocab(tmp_path, "GIB"), []

    def read(name):
        calls.append(name)
        return [{"term": "Hirepool", "status": "active"}]
    av.keyterms(path, "t", now=1000, read=read)
    av.keyterms(path, "t", now=1000 + av.CACHE_SECONDS - 1, read=read)
    assert len(calls) == 1
    av.keyterms(path, "t", now=1000 + av.CACHE_SECONDS, read=read)
    assert len(calls) == 2


def test_an_unreadable_table_never_stops_a_transcription(tmp_path):
    path = _vocab(tmp_path, "GIB")

    def boom(name):
        raise RuntimeError("AccessDenied")
    assert av.keyterms(path, "t", now=1, read=boom) == ["GIB"], "no good read yet: the file alone"
    av.keyterms(path, "t", now=1000, read=lambda n: [{"term": "Hirepool", "status": "active"}])
    assert av.keyterms(path, "t", now=2000, read=boom) == ["GIB", "Hirepool"], "the last good read"


def test_the_transcriber_asks_the_table():
    src = open(os.path.join(ROOT, "src", "lambda_transcribe.py"), encoding="utf-8").read()
    assert "asr_vocabulary.keyterms(KEYTERMS_PATH, ASR_VOCAB_TABLE)" in src
    assert "elevenlabs_utils.load_keyterms(KEYTERMS_PATH)" not in src


def test_the_template_keeps_the_table_and_lets_the_transcriber_read_it():
    tpl = open(os.path.join(ROOT, "src", "template.yaml"), encoding="utf-8").read()
    table = tpl[tpl.index("  AsrVocabularyTable:"):][:900]
    assert "DeletionPolicy: Retain" in table and "UpdateReplacePolicy: Retain" in table
    assert '"${P}-asr-vocabulary"' in table
    assert "PointInTimeRecovery" not in table, "the deploy role cannot set it (simulated 2026-10-09)"
    fn = tpl[tpl.index("  TranscribeFunction:"):tpl.index("  AsrVocabularyTable:")]
    assert "ASR_VOCAB_TABLE: !Ref AsrVocabularyTable" in fn
    assert "DynamoDBReadPolicy:\n            TableName: !Ref AsrVocabularyTable" in fn.replace("\r\n", "\n")


# ---- the script that edits it -------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location("asr_vocab", os.path.join(ROOT, "scripts", "asr_vocab.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTable:
    def __init__(self, rows=()):
        self.rows = {r["term"]: dict(r) for r in rows}

    def scan(self, **kw):
        return {"Items": [dict(r) for r in self.rows.values()]}

    def get_item(self, Key):
        r = self.rows.get(Key["term"])
        return {"Item": dict(r)} if r else {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression and Item["term"] in self.rows:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.rows[Item["term"]] = dict(Item)


def test_the_script_reads_the_file_as_the_transcriber_does():
    assert _script().file_terms(VOCAB) == eu.load_keyterms(VOCAB)


def test_a_reseed_never_undoes_a_retire():
    s = _script()
    t = FakeTable([{"term": "Hiab", "status": "retired"}])
    assert s.seed(t, ["Hiab", "GIB"], by="t", now="n") == 1
    assert t.rows["Hiab"]["status"] == "retired" and t.rows["GIB"]["status"] == "active"


def test_add_records_what_it_was_heard_as_and_where():
    s = _script()
    t = FakeTable()
    s.add(t, "Hirepool", heard=["Hiab"], evidence={"date": "2026-10-08", "session": "sid5cc1"},
          by="t", now="n")
    s.add(t, "Hirepool", heard=["hair pool", "Hiab"], by="t", now="n2")
    row = t.rows["Hirepool"]
    assert row["status"] == "active" and row["misheard_as"] == ["Hiab", "hair pool"]
    assert row["evidence"] == [{"date": "2026-10-08", "session": "sid5cc1"}]
    s.add(t, "Hiab", status="retired", by="t", now="n3")
    assert t.rows["Hiab"]["status"] == "retired"
    with pytest.raises(ValueError):
        s.add(t, "x", status="maybe")


# ---- promote: TEST's decisions to prod -------------------------------------------


def _promote(src, dst, answer="y"):
    said = []
    n = _script().promote(src, dst, "test", ask=lambda q: answer, show=said.append)
    return n, said


def test_THE_promote_carries_adds_and_retires_and_leaves_candidates_and_prod_only_rows():
    src = FakeTable([{"term": "Hirepool", "status": "active", "misheard_as": ["Hiab"],
                      "evidence": [{"date": "2026-10-08"}]},
                     {"term": "Hiab", "status": "retired"},
                     {"term": "washdown", "status": "candidate"}])
    dst = FakeTable([{"term": "Hiab", "status": "active"},
                     {"term": "Provista", "status": "active"}])
    n, said = _promote(src, dst)
    assert n == 2
    assert dst.rows["Hirepool"]["status"] == "active" and dst.rows["Hirepool"]["misheard_as"] == ["Hiab"]
    assert dst.rows["Hiab"]["status"] == "retired" and dst.rows["Hiab"]["promoted_from"] == "test"
    assert "washdown" not in dst.rows, "a candidate is a note, not a decision"
    assert dst.rows["Provista"]["status"] == "active", "a prod-only row is never touched"
    assert any("1 add, 1 status" in line for line in said)


def test_a_second_promote_has_nothing_to_do():
    src = FakeTable([{"term": "Hirepool", "status": "active", "misheard_as": ["Hiab"]}])
    dst = FakeTable()
    _promote(src, dst)
    n, said = _promote(src, dst)
    assert n == 0 and said == ["nothing to promote: the target already agrees"]


def test_new_heard_as_travels_and_merges_with_what_prod_already_knew():
    src = FakeTable([{"term": "Hirepool", "status": "active", "misheard_as": ["hair pool"]}])
    dst = FakeTable([{"term": "Hirepool", "status": "active", "misheard_as": ["Hiab"]}])
    n, _ = _promote(src, dst)
    assert n == 1 and dst.rows["Hirepool"]["misheard_as"] == ["Hiab", "hair pool"]


def test_nothing_is_written_without_a_yes():
    src = FakeTable([{"term": "Hirepool", "status": "active"}])
    dst = FakeTable()
    n, said = _promote(src, dst, answer="")
    assert n == 0 and dst.rows == {} and said[-1] == "not applied"
