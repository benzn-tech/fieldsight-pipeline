"""web_fetch: the action-pack page fetcher's SSRF guard, caps and robots (spec 2026-11-01
background action agent §6, §14 S5; Task 9).

No network anywhere: DNS is a fake resolver, the HTTPS connection is a fake factory, the
clocks are fakes. The one test of the real pinned connection monkeypatches
socket.create_connection and hands it a fake TLS context.
"""
import socket
import ssl

import pytest

import web_fetch as wf

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:2800:220:1:248:1893:25c8:1946"


# ------------------------------------------------------------------------ fakes

class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunk_log=None):
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self._pos = 0
        self.reads = chunk_log if chunk_log is not None else []

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, n=None):
        n = len(self._body) - self._pos if n is None else n
        self.reads.append(n)
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class FakeConn:
    def __init__(self, server, host, ip, port, timeout):
        self.server, self.host, self.ip, self.port, self.timeout = server, host, ip, port, timeout
        self.closed = False

    def request(self, method, path, headers=None):
        self.server.requests.append({"host": self.host, "ip": self.ip, "port": self.port,
                                     "method": method, "path": path,
                                     "headers": dict(headers or {})})
        self._path = path

    def getresponse(self):
        route = self.server.routes.get((self.host, self._path))
        if route is None:
            if self._path == "/robots.txt":
                return FakeResponse(404, {"Content-Type": "text/plain"}, b"")
            raise AssertionError(f"unexpected request {self.host}{self._path}")
        if isinstance(route, Exception):
            raise route
        return route

    def close(self):
        self.closed = True


class FakeServer:
    """Routes (host, path) -> FakeResponse; records every connection and request."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []
        self.connects = []

    def connect(self, host, ip, port, timeout):
        self.connects.append((host, ip, port))
        return FakeConn(self, host, ip, port, timeout)


def resolver_of(table):
    calls = []

    def resolve(host, port):
        calls.append((host, port))
        if host not in table:
            raise socket.gaierror("no such host")
        return list(table[host])
    resolve.calls = calls
    return resolve


def html(body, ctype="text/html; charset=utf-8", status=200, extra=None):
    headers = {"Content-Type": ctype}
    headers.update(extra or {})
    return FakeResponse(status, headers, body.encode("utf-8") if isinstance(body, str) else body)


def make(routes=None, dns=None, **kw):
    server = FakeServer(routes)
    resolve = resolver_of(dns if dns is not None else {"example.org": [PUBLIC]})
    f = wf.Fetcher(resolver=resolve, connect=server.connect,
                   now=lambda: __import__("datetime").datetime(2026, 11, 1, 1, 2, 3,
                                                               tzinfo=__import__("datetime").timezone.utc),
                   **kw)
    return f, server, resolve


# ----------------------------------------------------------------- SSRF table

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254",
    "169.254.1.1", "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1", "239.1.2.3",
    "240.0.0.1", "255.255.255.255", "192.0.2.1", "198.18.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "fd12:3456::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::127.0.0.1",
    "2002:7f00:0001::1",        # 6to4 embedding 127.0.0.1
    "2002:a9fe:a9fe::1",        # 6to4 embedding 169.254.169.254
    "64:ff9b::a9fe:a9fe",       # NAT64 embedding 169.254.169.254
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",   # Teredo
    "fec0::1",                  # site-local
])
def test_blocked_addresses(addr):
    assert wf.address_allowed(addr) is False


# 6to4 (2002::/16) embedding a public address is not listed: newer stdlib patch releases
# class all of 2002::/16 as private, and refusing it costs nothing.
@pytest.mark.parametrize("addr", [PUBLIC, "8.8.8.8", "1.1.1.1", PUBLIC6])
def test_public_addresses_allowed(addr):
    assert wf.address_allowed(addr) is True


@pytest.mark.parametrize("url,reason", [
    ("http://example.org/", "scheme"),
    ("ftp://example.org/", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://example.org/", "scheme"),
    ("javascript:alert(1)", "scheme"),
    ("https://user:pw@example.org/", "userinfo"),
    ("https://user@example.org/", "userinfo"),
    ("https://example.org:8443/", "port"),
    ("https://example.org:80/", "port"),
    ("https://example.org:99999/", "port"),
    ("https:///nohost", "bad_host"),
    ("https://127.0.0.1/", "blocked_address"),
    ("https://169.254.169.254/latest/meta-data/", "blocked_address"),
    ("https://10.1.2.3/", "blocked_address"),
    ("https://[::1]/", "blocked_address"),
    ("https://[fd00:ec2::254]/", "blocked_address"),
    ("https://[::ffff:127.0.0.1]/", "blocked_address"),
    ("https://[fe80::1%25eth0]/", "bad_host"),
    ("https://2130706433/", "bad_host"),          # decimal 127.0.0.1
    ("https://0x7f000001/", "bad_host"),          # hex
    ("https://0177.0.0.1/", "bad_host"),          # octal
    ("https://127.1/", "bad_host"),               # short form
    ("https://localhost/", "bad_host"),
    ("https://a.localhost/", "bad_host"),
    ("https://exa mple.org/", "bad_url"),
    ("https://example.org/\npath", "bad_url"),
    ("", "bad_url"),
    (None, "bad_url"),
    ({"url": "https://example.org/"}, "bad_url"),
])
def test_vet_url_refusals(url, reason):
    resolve = resolver_of({"example.org": [PUBLIC], "2130706433": ["127.0.0.1"],
                           "0x7f000001": ["127.0.0.1"], "0177.0.0.1": ["127.0.0.1"],
                           "127.1": ["127.0.0.1"], "localhost": ["127.0.0.1"],
                           "a.localhost": ["127.0.0.1"]})
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url(url, resolve)
    assert exc.value.code == reason


def test_numeric_host_forms_are_refused_after_resolution_too():
    """If a numeric form slipped past the name check, the resolved address is what is
    judged: a resolver answering 127.0.0.1 for an ordinary-looking name is refused."""
    resolve = resolver_of({"evil.example": ["127.0.0.1"]})
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url("https://evil.example/", resolve)
    assert exc.value.code == "blocked_address"


def test_one_public_and_one_private_answer_is_refused():
    resolve = resolver_of({"mixed.example": [PUBLIC, "10.0.0.5"]})
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url("https://mixed.example/", resolve)
    assert exc.value.code == "blocked_address"
    resolve = resolver_of({"mixed6.example": [PUBLIC6, "fd00:ec2::254"]})
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url("https://mixed6.example/", resolve)
    assert exc.value.code == "blocked_address"


def test_dns_failure_and_empty_answer_are_refused():
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url("https://nowhere.example/", resolver_of({}))
    assert exc.value.code == "dns"
    with pytest.raises(wf.Refused) as exc:
        wf.vet_url("https://empty.example/", resolver_of({"empty.example": []}))
    assert exc.value.code == "dns"


def test_vet_url_pins_the_first_vetted_address_and_keeps_the_query_in_the_path():
    t = wf.vet_url("https://Example.ORG./a/b?q=1#frag", resolver_of({"example.org": [PUBLIC]}))
    assert (t.host, t.ip, t.path) == ("example.org", PUBLIC, "/a/b?q=1")
    t = wf.vet_url("https://example.org:443", resolver_of({"example.org": [PUBLIC]}))
    assert t.path == "/"


def test_ip_literal_host_is_judged_without_dns():
    resolve = resolver_of({})
    t = wf.vet_url(f"https://{PUBLIC}/x", resolve)
    assert t.ip == PUBLIC and resolve.calls == []


@pytest.mark.parametrize("url", ["http://example.org/", "https://169.254.169.254/",
                                 "https://user@example.org/", "https://example.org:8443/"])
def test_fetch_never_connects_for_a_refused_url(url):
    f, server, _ = make()
    r = f.fetch(url)
    assert r["ok"] is False and r["text"] is None and server.connects == []


# --------------------------------------------------------- pinned connection

def test_fetch_connects_to_the_vetted_ip_with_the_hostname_for_host_and_sni():
    f, server, resolve = make({("example.org", "/page"): html("<p>Hi</p>")})
    r = f.fetch("https://example.org/page")
    assert r["ok"] is True
    page = [c for c in server.connects if c[0] == "example.org"]
    assert page and all(c == ("example.org", PUBLIC, 443) for c in page)
    # One resolution per hop at most: robots + page share the vetted address of this run.
    assert all(h == "example.org" for h, _ in resolve.calls)


def test_dns_rebinding_cannot_swap_the_address_between_check_and_connect():
    """The resolver answers public first, private afterwards. The connection still targets
    the vetted public address because the fetcher never resolves twice for one hop."""
    answers = [[PUBLIC], ["127.0.0.1"], ["127.0.0.1"], ["127.0.0.1"]]

    def rebinding(host, port):
        return answers.pop(0)
    server = FakeServer({("rebind.example", "/"): html("<p>ok</p>")})
    f = wf.Fetcher(resolver=rebinding, connect=server.connect, check_robots=False)
    r = f.fetch("https://rebind.example/")
    assert r["ok"] is True
    assert server.connects == [("rebind.example", PUBLIC, 443)]


def test_real_pinned_connection_dials_the_ip_and_verifies_the_hostname(monkeypatch):
    dialled = {}

    def fake_create_connection(addr, timeout=None, *a, **k):
        dialled["addr"], dialled["timeout"] = addr, timeout
        return "raw-socket"

    class FakeContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, sock, server_hostname=None):
            dialled["wrapped"], dialled["sni"] = sock, server_hostname
            return "tls-socket"

    monkeypatch.setattr(wf.socket, "create_connection", fake_create_connection)
    conn = wf.PinnedHTTPSConnection("example.org", PUBLIC, timeout=7, context=FakeContext())
    conn.connect()
    assert dialled == {"addr": (PUBLIC, 443), "timeout": 7, "wrapped": "raw-socket",
                       "sni": "example.org"}
    assert conn.sock == "tls-socket" and conn.host == "example.org"


def test_default_tls_context_verifies_certificates():
    conn = wf.PinnedHTTPSConnection("example.org", PUBLIC, timeout=5)
    ctx = conn._context
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED


def test_default_resolver_uses_getaddrinfo(monkeypatch):
    seen = []

    def fake_gai(host, port, *a, **k):
        seen.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC6, port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]
    monkeypatch.setattr(wf.socket, "getaddrinfo", fake_gai)
    assert wf.default_resolver("example.org", 443) == [PUBLIC, PUBLIC6]
    assert seen == [("example.org", 443)]


# --------------------------------------------------------------- redirects

def test_redirect_to_a_private_host_is_refused():
    f, server, _ = make({("example.org", "/a"): FakeResponse(
        302, {"Location": "https://inside.example/admin"})},
        dns={"example.org": [PUBLIC], "inside.example": ["10.0.0.7"]})
    r = f.fetch("https://example.org/a")
    assert r["ok"] is False and r["reason"] == "blocked_address"
    assert all(c[0] != "inside.example" for c in server.connects)


def test_redirect_to_metadata_literal_is_refused():
    f, server, _ = make({("example.org", "/a"): FakeResponse(
        301, {"Location": "https://169.254.169.254/latest/meta-data/"})})
    r = f.fetch("https://example.org/a")
    assert r["reason"] == "blocked_address"
    assert all(c[1] != "169.254.169.254" for c in server.connects)


def test_redirect_to_http_is_refused():
    f, _, _ = make({("example.org", "/a"): FakeResponse(
        302, {"Location": "http://example.org/b"})})
    assert f.fetch("https://example.org/a")["reason"] == "scheme"


def test_relative_redirect_is_followed_and_rechecked():
    f, server, _ = make({("example.org", "/a"): FakeResponse(307, {"Location": "/b?x=1"}),
                         ("example.org", "/b?x=1"): html("<p>landed</p>")})
    r = f.fetch("https://example.org/a")
    assert r["ok"] is True and r["final_url"] == "https://example.org/b?x=1"
    assert r["url"] == "https://example.org/a" and r["text"] == "landed"


def test_three_redirects_are_followed_a_fourth_is_refused():
    routes = {("example.org", f"/{i}"): FakeResponse(302, {"Location": f"/{i + 1}"})
              for i in range(4)}
    routes[("example.org", "/3")] = html("<p>three</p>")
    f, _, _ = make(routes)
    assert f.fetch("https://example.org/0")["text"] == "three"

    routes[("example.org", "/3")] = FakeResponse(302, {"Location": "/4"})
    routes[("example.org", "/4")] = html("<p>four</p>")
    f, server, _ = make(routes)
    r = f.fetch("https://example.org/0")
    assert r["ok"] is False and r["reason"] == "too_many_redirects"
    assert all(q["path"] != "/4" for q in server.requests)


def test_redirect_without_location_fails():
    f, _, _ = make({("example.org", "/a"): FakeResponse(302, {})})
    assert f.fetch("https://example.org/a")["reason"] == "redirect_without_location"


# ------------------------------------------------------------- size / type

def test_download_is_capped_at_2_mb_and_marked_truncated():
    body = b"a" * (wf.MAX_DOWNLOAD_BYTES + 500_000)
    resp = FakeResponse(200, {"Content-Type": "text/plain"}, body)
    f, _, _ = make({("example.org", "/big"): resp})
    r = f.fetch("https://example.org/big")
    assert r["ok"] is True and r["truncated"] is True
    assert sum(resp.reads) <= wf.MAX_DOWNLOAD_BYTES + 1
    assert len(r["text"].encode("utf-8")) <= wf.MAX_TEXT_BYTES


def test_text_is_capped_at_200_kb_on_a_character_boundary():
    body = ("中" * 100_000).encode("utf-8")       # 300 kB of 3-byte characters
    f, _, _ = make({("example.org", "/zh"): FakeResponse(
        200, {"Content-Type": "text/plain; charset=utf-8"}, body)})
    r = f.fetch("https://example.org/zh")
    assert r["truncated"] is True
    raw = r["text"].encode("utf-8")
    assert len(raw) <= wf.MAX_TEXT_BYTES and set(r["text"]) == {"中"}


def test_small_page_is_not_truncated():
    f, _, _ = make({("example.org", "/s"): html("<p>small</p>")})
    r = f.fetch("https://example.org/s")
    assert r["truncated"] is False and r["status"] == 200
    assert r["fetched_at"] == "2026-11-01T01:02:03+00:00" and r["reason"] is None


@pytest.mark.parametrize("ctype", ["application/pdf", "application/json", "image/png",
                                   "application/xhtml+xml", "text/csv", None, ""])
def test_other_content_types_are_refused_before_the_body_is_read(ctype):
    headers = {} if ctype is None else {"Content-Type": ctype}
    resp = FakeResponse(200, headers, b"%PDF-1.7")
    f, _, _ = make({("example.org", "/f"): resp})
    r = f.fetch("https://example.org/f")
    assert r["ok"] is False and r["reason"] == "content_type" and r["text"] is None
    assert resp.reads == []


def test_compressed_body_is_refused():
    f, server, _ = make({("example.org", "/g"): html("x", extra={"Content-Encoding": "gzip"})})
    assert f.fetch("https://example.org/g")["reason"] == "content_encoding"
    assert server.requests[-1]["headers"]["Accept-Encoding"] == "identity"


def test_non_200_status_fails_with_its_status():
    f, _, _ = make({("example.org", "/x"): html("nope", status=404)})
    r = f.fetch("https://example.org/x")
    assert (r["ok"], r["reason"], r["status"]) == (False, "http_status", 404)


def test_request_carries_a_descriptive_user_agent():
    f, server, _ = make({("example.org", "/s"): html("<p>s</p>")})
    f.fetch("https://example.org/s")
    page = [q for q in server.requests if q["path"] == "/s"][0]
    assert page["headers"]["User-Agent"] == wf.USER_AGENT
    assert wf.ROBOTS_TOKEN in wf.USER_AGENT and page["method"] == "GET"


def test_network_error_is_a_failure_not_a_raise():
    f, _, _ = make({("example.org", "/n"): ConnectionResetError("reset")})
    r = f.fetch("https://example.org/n")
    assert r["ok"] is False and r["reason"] == "network"


def test_ssl_error_is_a_failure_not_a_raise():
    f, _, _ = make({("example.org", "/n"): ssl.SSLCertVerificationError("bad cert")})
    assert f.fetch("https://example.org/n")["reason"] == "tls"


def test_slow_drip_body_hits_the_page_deadline():
    t = [0.0]

    class Drip(FakeResponse):
        def read(self, n=None):
            t[0] += 4.0                     # every chunk costs 4 s of wall time
            return b"x" * 10

    server = FakeServer({("example.org", "/slow"): Drip(200, {"Content-Type": "text/plain"})})
    f = wf.Fetcher(resolver=resolver_of({"example.org": [PUBLIC]}), connect=server.connect,
                   clock=lambda: t[0], page_timeout=10, check_robots=False)
    r = f.fetch("https://example.org/slow")
    assert r["ok"] is False and r["reason"] == "timeout"


def test_socket_timeout_never_exceeds_the_page_budget():
    seen = []
    server = FakeServer({("example.org", "/s"): html("<p>s</p>")})

    def connect(host, ip, port, timeout):
        seen.append(timeout)
        return server.connect(host, ip, port, timeout)
    f = wf.Fetcher(resolver=resolver_of({"example.org": [PUBLIC]}), connect=connect,
                   page_timeout=6, check_robots=False)
    f.fetch("https://example.org/s")
    assert seen and all(0 < s <= 6 for s in seen)


# -------------------------------------------------------------- HTML -> text

def test_html_to_text_drops_scripts_styles_and_head():
    text = wf.html_to_text(
        "<html><head><title>T</title><style>p{}</style><script>var a=1;</script></head>"
        "<body><h1>Header</h1><p>First &amp; <b>bold</b></p><div>Second</div>"
        "<noscript>ns</noscript><ul><li>one</li><li>two</li></ul>"
        "<script>evil()</script>tail</body></html>")
    assert text == "Header\nFirst & bold\nSecond\none\ntwo\ntail"


def test_charset_from_header_is_used():
    body = "<p>café</p>".encode("latin-1")
    f, _, _ = make({("example.org", "/l"): FakeResponse(
        200, {"Content-Type": "text/html; charset=ISO-8859-1"}, body)})
    assert f.fetch("https://example.org/l")["text"] == "café"


def test_unknown_charset_falls_back_to_utf8():
    f, _, _ = make({("example.org", "/u"): FakeResponse(
        200, {"Content-Type": "text/plain; charset=nonsense-9"}, "héllo".encode())})
    assert f.fetch("https://example.org/u")["text"] == "héllo"


# ------------------------------------------------------------------- robots

ROBOTS_DENY = b"User-agent: *\nDisallow: /private/\n"


def test_robots_disallow_skips_the_page():
    f, server, _ = make({("example.org", "/robots.txt"): FakeResponse(
        200, {"Content-Type": "text/plain"}, ROBOTS_DENY)})
    r = f.fetch("https://example.org/private/page")
    assert r["ok"] is False and r["reason"] == "robots"
    assert [q["path"] for q in server.requests] == ["/robots.txt"]


def test_robots_is_fetched_once_per_host_per_run():
    f, server, _ = make({("example.org", "/robots.txt"): FakeResponse(
        200, {"Content-Type": "text/plain"}, ROBOTS_DENY),
        ("example.org", "/a"): html("<p>a</p>"), ("example.org", "/b"): html("<p>b</p>")})
    assert f.fetch("https://example.org/a")["ok"] and f.fetch("https://example.org/b")["ok"]
    assert [q["path"] for q in server.requests].count("/robots.txt") == 1


def test_robots_for_our_token_is_honoured():
    f, _, _ = make({("example.org", "/robots.txt"): FakeResponse(
        200, {"Content-Type": "text/plain"},
        b"User-agent: FieldSightBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")})
    assert f.fetch("https://example.org/x")["reason"] == "robots"


@pytest.mark.parametrize("robots", [
    FakeResponse(404, {"Content-Type": "text/html"}, b"missing"),
    FakeResponse(403, {"Content-Type": "text/html"}, b"forbidden"),
    FakeResponse(401, {}, b""),
    ConnectionResetError("reset"),
])
def test_robots_missing_forbidden_or_unreachable_allows(robots):
    f, _, _ = make({("example.org", "/robots.txt"): robots,
                    ("example.org", "/a"): html("<p>a</p>")})
    assert f.fetch("https://example.org/a")["ok"] is True


def test_robots_is_fetched_through_the_same_guard():
    """robots.txt redirecting to a private host is refused (and so treated as allow-all);
    the private host is never contacted."""
    f, server, _ = make({("example.org", "/robots.txt"): FakeResponse(
        302, {"Location": "https://inside.example/robots.txt"}),
        ("example.org", "/a"): html("<p>a</p>")},
        dns={"example.org": [PUBLIC], "inside.example": ["192.168.0.2"]})
    assert f.fetch("https://example.org/a")["ok"] is True
    assert all(c[0] != "inside.example" for c in server.connects)


def test_redirect_target_robots_is_checked_too():
    f, _, _ = make({("example.org", "/a"): FakeResponse(302, {"Location": "https://other.example/p"}),
                    ("other.example", "/robots.txt"): FakeResponse(
                        200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n")},
                   dns={"example.org": [PUBLIC], "other.example": ["8.8.4.4"]})
    assert f.fetch("https://example.org/a")["reason"] == "robots"


# ------------------------------------------------------------------- logging

def test_logs_never_carry_the_url_or_page_text(caplog):
    caplog.set_level("DEBUG")
    f, _, _ = make({("example.org", "/p?token=SECRET"): html("<p>PAGE-TEXT</p>")})
    f.fetch("https://example.org/p?token=SECRET")
    f.fetch("http://example.org/p?token=SECRET")
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET" not in joined and "PAGE-TEXT" not in joined and "/p" not in joined
