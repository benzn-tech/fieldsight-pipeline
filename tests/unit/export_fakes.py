"""A FakeS3 with real multipart semantics, for the company-export tests."""
import io

from botocore.exceptions import ClientError


class Body:
    def __init__(self, data):
        self._b = io.BytesIO(data)

    def iter_chunks(self, chunk_size=1024):
        while True:
            c = self._b.read(chunk_size)
            if not c:
                return
            yield c

    def read(self):
        return self._b.read()

    def close(self):
        pass


class FakeS3:
    """Objects per (bucket, key). Multipart: parts must be uploaded, every part but the last
    must be >= min_part, complete assembles in part order, abort discards. Mutations other
    than put_object/multipart (delete, copy) raise: an export never deletes or copies."""

    def __init__(self, objects=None, min_part=1):
        self.objects = dict(objects or {})      # (bucket, key) -> bytes
        self.min_part = min_part
        self.uploads = {}                       # upload_id -> {"bucket","key","parts":{n: bytes}}
        self.completed = []
        self.aborted = []
        self.written = []                       # every (bucket, key) created by put/complete
        self.max_part_len = 0
        self.part_lens = []
        self.get_calls = []
        self.fail_get = {}                      # key -> exception
        self.fail_upload_part = None            # exception raised on the Nth upload_part (1-based) -> (n, exc)
        self._n = 0
        self.etags = {}                         # (bucket, key) -> current etag
        self._etag_n = 0
        self.race_on_put = None                 # callable(bucket, key) run just before a put

    # reads
    def get_object(self, Bucket, Key):
        self.get_calls.append((Bucket, Key))
        if Key in self.fail_get:
            raise self.fail_get[Key]
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": Body(self.objects[(Bucket, Key)]),
                "ETag": self.etags.get((Bucket, Key), '"e0"')}

    def _stamp(self, Bucket, Key):
        self._etag_n += 1
        self.etags[(Bucket, Key)] = f'"v{self._etag_n}"'
        return self.etags[(Bucket, Key)]

    def put_object(self, Bucket, Key, Body, **kw):
        if self.race_on_put:
            self.race_on_put(Bucket, Key)
        exists = (Bucket, Key) in self.objects
        if kw.get("IfNoneMatch") == "*" and exists:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}}, "PutObject")
        if "IfMatch" in kw and (not exists or self.etags.get((Bucket, Key), '"e0"') != kw["IfMatch"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}}, "PutObject")
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()
        self.written.append((Bucket, Key))
        return {"ETag": self._stamp(Bucket, Key)}

    # multipart
    def create_multipart_upload(self, Bucket, Key, **kw):
        self._n += 1
        uid = f"up{self._n}"
        self.uploads[uid] = {"bucket": Bucket, "key": Key, "parts": {}}
        return {"UploadId": uid}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, **kw):
        if self.fail_upload_part and self.fail_upload_part[0] == len(self.part_lens) + 1:
            raise self.fail_upload_part[1]
        self.uploads[UploadId]["parts"][PartNumber] = bytes(Body)
        self.part_lens.append(len(Body))
        self.max_part_len = max(self.max_part_len, len(Body))
        return {"ETag": f'"e{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        up = self.uploads.pop(UploadId)
        nums = [p["PartNumber"] for p in MultipartUpload["Parts"]]
        assert nums == sorted(nums) == list(range(1, len(nums) + 1)), nums
        data = b""
        for i, n in enumerate(nums):
            part = up["parts"][n]
            assert i == len(nums) - 1 or len(part) >= self.min_part, "part below the S3 minimum"
            data += part
        self.objects[(Bucket, Key)] = data
        self.written.append((Bucket, Key))
        self.completed.append(Key)
        return {}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(UploadId, None)
        self.aborted.append(Key)
        return {}

    def __getattr__(self, name):
        if name.startswith(("delete_", "copy_", "list_")):
            def call(*a, **k):
                raise AssertionError(f"export must not call {name}")
            return call
        raise AttributeError(name)
