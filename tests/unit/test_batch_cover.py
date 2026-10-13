"""A batch wav is the same speech as the chunk wavs it was stitched from. The map beside
it is the only authority on which chunks those are -- the filename is not (a chunk VAD
rejected is bridged, so c1035_bn4 holds 1035, 1036, 1037 and 1039)."""
import json

import batch_cover

SID = "449c1bb3473c405985351aaa189049e6"
P = "audio_segments/Ben_Lin_test2/2026-09-17/"


def chunk(i, base="13-05-18", to="30.0"):
    return f"{P}ben_lin_2026-09-17_{base}_sid{SID}_c{i:04d}_off0.0_to{to}_srcwav.wav"


def batch(first, count, to):
    return f"{P}ben_lin_2026-09-17_13-05-18_sid{SID}_c{first:04d}_bn{count}_off0.0_to{to}_srcwav.wav"


def map_of(batch_key, member_keys):
    stem = batch_key[: -len(".wav")]
    return stem + "_batch_map.json", json.dumps(
        {"schema": 1, "session_id": SID,
         "members": [{"chunk_index": n, "chunk_key": k} for n, k in enumerate(member_keys)]}
    ).encode()


class Store:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.read = []

    def get(self, key):
        self.read.append(key)
        if key not in self.objects:
            raise KeyError(key)          # stands in for S3 NoSuchKey / AccessDenied
        return self.objects[key]


def test_returns_the_member_keys_the_map_names():
    b = batch(0, 2, "58.0")
    store = Store([map_of(b, [chunk(0), chunk(1)])])
    assert batch_cover.covered_chunk_keys(store.get, [b, chunk(0), chunk(1), chunk(2)]) == {
        chunk(0), chunk(1)}


def test_members_come_from_the_map_not_the_filename():
    # c0035_bn4 whose 4th member is c0039 because c0038 was VAD-rejected (prod 2026-09-10).
    b = batch(35, 4, "116.0")
    members = [chunk(35), chunk(36), chunk(37), chunk(39)]
    store = Store([map_of(b, members)])
    covered = batch_cover.covered_chunk_keys(store.get, [b] + members)
    assert covered == set(members)
    assert chunk(38) not in covered


def test_non_batch_keys_are_never_read():
    store = Store([])
    assert batch_cover.covered_chunk_keys(store.get, [chunk(0), chunk(1)]) == set()
    assert store.read == []


def test_a_missing_map_hides_nothing_and_does_not_stop_the_others():
    b_missing, b_ok = batch(0, 2, "58.0"), batch(2, 3, "82.0")
    store = Store([map_of(b_ok, [chunk(2), chunk(3), chunk(4)])])
    assert batch_cover.covered_chunk_keys(store.get, [b_missing, b_ok]) == {
        chunk(2), chunk(3), chunk(4)}


def test_a_malformed_map_hides_nothing():
    b1, b2, b3 = batch(0, 2, "58.0"), batch(2, 1, "30.0"), batch(3, 1, "30.0")
    store = Store([
        (b1[: -len(".wav")] + "_batch_map.json", b"not json"),
        (b2[: -len(".wav")] + "_batch_map.json", json.dumps({"members": "nope"}).encode()),
        (b3[: -len(".wav")] + "_batch_map.json", json.dumps({"members": [{"x": 1}, 7]}).encode()),
    ])
    assert batch_cover.covered_chunk_keys(store.get, [b1, b2, b3]) == set()


def test_a_batch_never_hides_a_batch():
    b1, b2 = batch(0, 2, "58.0"), batch(2, 2, "58.0")
    store = Store([map_of(b1, [chunk(0), b2])])
    assert batch_cover.covered_chunk_keys(store.get, [b1, b2]) == {chunk(0)}


def test_empty_input_reads_nothing():
    store = Store([])
    assert batch_cover.covered_chunk_keys(store.get, []) == set()
    assert store.read == []
