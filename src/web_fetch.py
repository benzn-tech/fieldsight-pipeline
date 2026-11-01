"""The action-pack agent's page fetcher (spec 2026-11-01 background action agent §6, §14 S5;
Task 9). New code: nothing in the repo fetched arbitrary pages before.

The agent runs outside the VPC with internet egress, so every fetch goes through an SSRF
guard, and the guard decides the address the connection is made to:

* `vet_url` -- https only, no userinfo, port 443 only, a host that is a canonical IP literal
  or a DNS name (numeric shortcuts such as 2130706433 / 0x7f000001 / 0177.0.0.1 / 127.1 and
  localhost are refused by name), resolved ONCE; refused when ANY answer is not a public
  address (`address_allowed`). It returns the vetted addresses, IPv4 first.
* `PinnedHTTPSConnection` -- dials a vetted address and wraps it in TLS with SNI and
  certificate verification for the HOSTNAME. There is no second resolution, so DNS
  rebinding cannot swap the address between the check and the connect. A connect failure
  tries the next VETTED address (never a new lookup).
* Redirects are followed by hand, at most MAX_REDIRECTS, every hop through `vet_url` again.
* Citation redirectors (`CITATION_REDIRECTORS`): the web search vendor hands back every
  source as a Google grounding redirect (vertexaisearch.cloud.google.com/
  grounding-api-redirect/...), whose robots.txt disallows that path -- honoured, no cited
  page could ever be archived. When the URL we were HANDED (hop 0 only) is such a redirect,
  that one hop skips the redirector's robots check and must answer a redirect (its Location
  is all we read; a page from it fails 'redirector_no_redirect'). The real page it points
  to is vetted and robots-checked like any other.
* A per-page deadline that holds in every phase. A socket timeout bounds one recv only (a
  slow-drip server defeats it), so each hop arms a watchdog timer that shuts the socket
  down at the deadline; the socket timeout is also reset to the time left before every
  read, and the body is read with read1 (never "block until n bytes"). At most
  MAX_DOWNLOAD_BYTES read (then `truncated`), at most MAX_TEXT_BYTES of text kept,
  text/html and text/plain only, identity encoding only.
* robots.txt via urllib.robotparser.parse on text fetched through this same guarded path
  (never RobotFileParser.read, which opens its own connection), once per host per Fetcher.
  RFC 9309: a 4xx robots.txt = allow all; a 5xx, timeout, network/TLS error or a refused
  robots fetch = unreachable = disallow all (reason 'robots_unavailable'); an explicit
  disallow skips the page (reason 'robots').

Every seam is injectable: the resolver, the connection factory, both clocks. Tests make no
network calls.

Logs: reason codes and counts only -- never a URL, a host, or page text.
"""
from __future__ import annotations

import codecs
import datetime as dt
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

logger = logging.getLogger(__name__)

ROBOTS_TOKEN = "FieldSightBot"
USER_AGENT = (f"{ROBOTS_TOKEN}/1.0 (FieldSight action packs; fetches each cited page once "
              "for an archived snapshot; honours robots.txt)")
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 200_000            # = action_pack_view.SNAPSHOT_MAX_BYTES (the viewer's cap)
MAX_ROBOTS_BYTES = 512 * 1024
MAX_REDIRECTS = 3
MAX_ROBOTS_REDIRECTS = 5        # RFC 9309 2.3.1.2: follow at least five for robots.txt
PAGE_TIMEOUT_S = 10.0
MAX_URL_CHARS = 2048
ALLOWED_TYPES = ("text/html", "text/plain")
_REDIRECTS = (301, 302, 303, 307, 308)
# (host, path prefix) of the vendor redirect hops a cited source URL arrives as. Matched on
# the vetted host (exact) and the vetted path, never on the raw string.
CITATION_REDIRECTORS = (("vertexaisearch.cloud.google.com", "/grounding-api-redirect/"),)
_CHUNK = 64 * 1024

# Belt and braces next to `is_global`: a stdlib change must never widen what we dial.
_DENY = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128", "::1/128", "::/96", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64",
    "2001::/23", "2001:db8::/32", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8"))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
# Characters kept as they are when a path/query is percent-encoded (an existing %XX stays).
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"


class Refused(Exception):
    """The guard refused a URL. `code` is the reason the fetch result carries."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    url: str
    host: str          # what Host and SNI carry (IDNA ascii, lower case, no trailing dot)
    ips: tuple         # every vetted address, IPv4 first; dialled in order, never re-resolved
    path: str          # path + query, never the fragment

    @property
    def ip(self) -> str:
        """The address dialled first."""
        return self.ips[0]


def address_allowed(ip) -> bool:
    """True only for a public unicast address. IPv6 forms that embed an IPv4 address
    (mapped, IPv4-compatible, 6to4, NAT64, Teredo) are judged by the embedded address too."""
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    if any(a in net for net in _DENY if net.version == a.version):
        return False
    if a.version == 6:
        inner = [a.ipv4_mapped, a.sixtofour, *(a.teredo or ())]
        if a in _NAT64:
            inner.append(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF))
        if any(i is not None and not address_allowed(i) for i in inner):
            return False
        if a.is_site_local:
            return False
    if (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
            or a.is_unspecified):
        return False
    return a.is_global


def default_resolver(host, port):
    """Every address getaddrinfo returns for host (dedupe, order kept)."""
    out = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


def _host_of(parts):
    """The host to vet: (ascii host, is_ip_literal). Raises Refused."""
    try:
        host = parts.hostname
    except ValueError:
        raise Refused("bad_host")
    if not host:
        raise Refused("bad_host")
    host = host.rstrip(".").lower()
    if not host or "%" in host:           # an IPv6 zone id never leaves this machine
        raise Refused("bad_host")
    try:
        ipaddress.ip_address(host)
        return host, True
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Refused("bad_host")
    labels = host.split(".")
    if (len(host) > 253 or not all(_LABEL.match(lab) for lab in labels)
            or _NUMERIC_LABEL.match(labels[-1])):
        # A numeric last label is an IP shortcut (2130706433, 0x7f000001, 0177.0.0.1,
        # 127.1) that getaddrinfo would turn into an address: never a public DNS name.
        raise Refused("bad_host")
    if host == "localhost" or host.endswith(".localhost"):
        raise Refused("bad_host")
    return host, False


def vet_url(url, resolver) -> Target:
    """The guard. Returns the Target to dial, or raises Refused(code)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS \
            or _BAD_URL_CHARS.search(url):
        raise Refused("bad_url")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise Refused("bad_url")
    if parts.scheme.lower() != "https":
        raise Refused("scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("userinfo")
    try:
        port = parts.port
    except ValueError:
        raise Refused("port")
    if port not in (None, 443):
        raise Refused("port")
    host, literal = _host_of(parts)
    if literal:
        ips = [host]
    else:
        try:
            ips = list(resolver(host, 443) or [])
        except (OSError, UnicodeError, ValueError):
            raise Refused("dns")
        if not ips:
            raise Refused("dns")
    if not all(address_allowed(ip) for ip in ips):
        raise Refused("blocked_address")
    # A non-ASCII path or query (a cited page's own URL) is percent-encoded as UTF-8;
    # http.client would otherwise refuse it.
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    if parts.query:
        path += "?" + quote(parts.query, safe=_QUERY_SAFE)
    ordered = []
    for ip in ips:
        ip = str(ip)
        if ip not in ordered:
            ordered.append(ip)
    # IPv4 first (a Lambda outside a VPC usually has no IPv6 route); order kept otherwise.
    ordered.sort(key=lambda a: ipaddress.ip_address(a).version != 4)
    return Target(url=url, host=host, ips=tuple(ordered), path=path)


def _is_redirector(target) -> bool:
    return any(target.host == host and target.path.startswith(prefix)
               for host, prefix in CITATION_REDIRECTORS)


def is_citation_redirect(url) -> bool:
    """True when `url` is a vendor citation redirect hop (CITATION_REDIRECTORS). Pure: no DNS."""
    try:
        target = vet_url(url, lambda host, port: ["8.8.8.8"])
    except Refused:
        return False
    return _is_redirector(target)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a host whose address was vetted already: dials `ip`, verifies the
    certificate for `host` (SNI + check_hostname), Host header = `host`."""

    def __init__(self, host, ip, *, timeout, context=None):
        super().__init__(host, 443, timeout=timeout,
                         context=context or ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # Handshake after self.sock is set, so the deadline watchdog can shut the socket
        # down during a slow handshake too.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host,
                                              do_handshake_on_connect=False)
        self.sock.do_handshake()


def default_connect(host, ip, port, timeout):
    return PinnedHTTPSConnection(host, ip, timeout=timeout)


# ------------------------------------------------------------------ HTML -> text

_SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object",
         "canvas", "math"}
_BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
          "section", "article", "header", "footer", "table", "ul", "ol", "dl", "dt", "dd",
          "blockquote", "pre", "hr", "main", "nav", "aside", "figure", "figcaption", "form",
          "title", "body", "caption"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed",
         "source", "track", "wbr", "param"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP and tag not in _VOID:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _tidy(text):
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def html_to_text(html) -> str:
    p = _TextParser()
    try:
        p.feed(html)
        p.close()
    except Exception:                 # noqa: BLE001 -- a broken page keeps what was parsed
        pass
    return _tidy("".join(p.parts))


def _cap_text(text):
    raw = text.encode("utf-8")
    if len(raw) <= MAX_TEXT_BYTES:
        return text, False
    return raw[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), True


def _content_type(value):
    """(media type, charset or None) from a Content-Type header."""
    if not value:
        return "", None
    bits = [b.strip() for b in value.split(";")]
    charset = None
    for b in bits[1:]:
        k, _, v = b.partition("=")
        if k.strip().lower() == "charset":
            charset = v.strip().strip("\"'") or None
    return bits[0].lower(), charset


def _decode(raw, charset):
    """Decode with the declared charset when it is a TEXT encoding (not base64, zlib, rot13,
    ...); anything else or unknown -> utf-8 with replacement."""
    try:
        if not codecs.lookup(charset or "utf-8")._is_text_encoding:
            charset = "utf-8"
    except LookupError:
        charset = "utf-8"
    return raw.decode(charset or "utf-8", errors="replace")


def _close(conn):
    if conn is None:
        return
    try:
        conn.close()
    except Exception:                 # noqa: BLE001
        pass


def _sock_of(conn):
    return getattr(conn, "sock", None)


def _set_timeout(conn, seconds):
    sock = _sock_of(conn)
    if sock is not None:
        try:
            sock.settimeout(max(0.001, seconds))
        except OSError:
            pass


def _abort(conn):
    """Unblock whatever this connection is blocked in (connect/handshake/headers/body):
    shut the socket down. Runs on the watchdog thread, so it never closes the connection
    object (the fetching thread does that)."""
    sock = _sock_of(conn)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    hook = getattr(conn, "abort", None)          # a test double may expose one
    if callable(hook):
        hook()


class _Watchdog:
    """Fires once at the deadline and aborts the connection."""

    def __init__(self, seconds, conn):
        self.fired = False
        self._conn = conn
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        self.fired = True
        _abort(self._conn)

    def cancel(self):
        self._timer.cancel()


class _Fail(Exception):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


def _as_fail(exc, watchdog):
    """The _Fail an exception from the connection means, or None to re-raise it."""
    if isinstance(exc, _Fail):
        return exc
    if watchdog is not None and watchdog.fired:
        return _Fail("timeout")
    if isinstance(exc, ssl.SSLError):
        return _Fail("tls")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return _Fail("timeout")
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return _Fail("network")
    return None


_ROBOTS_UNAVAILABLE = object()      # cache value: robots.txt unreachable -> disallow all


class Fetcher:
    """One per run: the robots cache lives here (per host, per run).

    resolver(host, port) -> [ip, ...]; connect(host, ip, port, timeout) -> an object with
    request(method, path, headers=), getresponse() -> (.status, .getheader, .read(n)),
    close(); clock() -> monotonic seconds; now() -> aware datetime for `fetched_at`."""

    def __init__(self, *, resolver=None, connect=None, clock=None, now=None,
                 page_timeout=PAGE_TIMEOUT_S, check_robots=True):
        self._resolve = resolver or default_resolver
        self._connect = connect or default_connect
        self._clock = clock or time.monotonic
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._timeout = float(page_timeout)
        self._check_robots = check_robots
        self._robots = {}             # host -> RobotFileParser | None (allow all) | _ROBOTS_UNAVAILABLE

    # ---------------------------------------------------------------- public

    def fetch(self, url) -> dict:
        """{ok, url, final_url, status, text, truncated, reason, fetched_at}. Never raises."""
        out = {"ok": False, "url": url if isinstance(url, str) else None, "final_url": None,
               "status": None, "text": None, "truncated": False, "reason": None,
               "fetched_at": None}
        deadline = self._clock() + self._timeout
        try:
            final, status, ctype, raw, truncated = self._get(
                url, deadline, max_bytes=MAX_DOWNLOAD_BYTES, types=ALLOWED_TYPES,
                robots=self._check_robots)
            media, charset = ctype
            text = _decode(raw, charset)
            if media == "text/html":
                text = html_to_text(text)
            else:
                text = _tidy(text)
            text, cut = _cap_text(text)
            out.update(ok=True, final_url=final, status=status, text=text,
                       truncated=truncated or cut,
                       fetched_at=self._now().isoformat())
            logger.info("web fetch ok status=%d bytes=%d text_bytes=%d truncated=%s",
                        status, len(raw), len(text.encode("utf-8")), out["truncated"])
        except Refused as exc:
            out["reason"] = exc.code
            logger.info("web fetch refused reason=%s", exc.code)
        except _Fail as exc:
            out["reason"], out["status"] = exc.code, exc.status
            logger.info("web fetch failed reason=%s status=%s", exc.code, exc.status)
        except Exception as exc:      # noqa: BLE001 -- the contract is "never raises"
            out["reason"] = "internal"
            logger.warning("web fetch error type=%s", type(exc).__name__)
        return out

    # --------------------------------------------------------------- internals

    def _left(self, deadline):
        left = deadline - self._clock()
        if left <= 0:
            raise _Fail("timeout")
        return left

    def _get(self, url, deadline, *, max_bytes, types, robots,
             max_redirects=MAX_REDIRECTS):
        """Follow up to MAX_REDIRECTS redirects, each hop vetted. Returns
        (final_url, status, (media, charset), raw bytes, truncated)."""
        current = url
        for hop in range(max_redirects + 1):
            target = vet_url(current, self._resolve)
            # Only the URL we were handed may be a citation redirect hop (module doc).
            redirector = robots and hop == 0 and _is_redirector(target)
            if robots and not redirector:
                verdict = self._robots_verdict(target)
                if verdict is not None:
                    raise _Fail(verdict)
            out = self._exchange(target, deadline, max_bytes=0 if redirector else max_bytes,
                                 types=types)
            if redirector and out[0] != "redirect":
                raise _Fail("redirector_no_redirect", out[1])
            if out[0] == "redirect":
                _, location, status = out
                if hop == max_redirects:
                    raise _Fail("too_many_redirects", status)
                current = urljoin(target.url, location.strip())
                continue
            _, status, ctype, raw, truncated = out
            return current, status, ctype, raw, truncated
        raise _Fail("too_many_redirects")      # unreachable; the loop returns or raises

    def _exchange(self, target, deadline, *, max_bytes, types):
        """One request to an already-vetted target. ('redirect', location, status) or
        ('ok', status, (media, charset), raw, truncated); raises _Fail. The watchdog armed
        in _open holds the deadline through connect, handshake, headers and body."""
        conn = watchdog = None
        try:
            conn, watchdog = self._open(target, deadline)
            try:
                _set_timeout(conn, self._left(deadline))
                # No explicit Host: http.client writes the hostname (bracketing IPv6).
                conn.request("GET", target.path, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, text/plain;q=0.9",
                    "Accept-Encoding": "identity", "Connection": "close"})
                resp = conn.getresponse()
                self._left(deadline)
                status = resp.status
                if status in _REDIRECTS:
                    location = resp.getheader("Location")
                    if not location:
                        raise _Fail("redirect_without_location", status)
                    if watchdog.fired:
                        raise _Fail("timeout")
                    return "redirect", location, status
                if status != 200:
                    raise _Fail("http_status", status)
                ctype = _content_type(resp.getheader("Content-Type"))
                if types is not None and ctype[0] not in types:
                    raise _Fail("content_type", status)
                enc = (resp.getheader("Content-Encoding") or "identity").strip().lower()
                if enc not in ("", "identity"):
                    raise _Fail("content_encoding", status)
                raw, truncated = self._read(conn, resp, deadline, max_bytes)
                # POSIX: a shutdown makes a blocked recv return b"" (EOF), not an error. A
                # body cut by the watchdog is never a complete page.
                if watchdog.fired:
                    raise _Fail("timeout")
                return "ok", status, ctype, raw, truncated
            except BaseException as exc:
                fail = _as_fail(exc, watchdog)
                if fail is None or fail is exc:
                    raise
                raise fail
        finally:
            if watchdog is not None:
                watchdog.cancel()
            _close(conn)

    def _open(self, target, deadline):
        """A connected (conn, watchdog) to the first vetted address that accepts; the next
        vetted address on a connect failure, never a new lookup. A TLS failure is final."""
        last = _Fail("network")
        for ip in target.ips:
            left = self._left(deadline)
            conn = self._connect(target.host, ip, 443, left)
            watchdog = _Watchdog(left, conn)
            try:
                connect = getattr(conn, "connect", None)
                if callable(connect):
                    connect()
                return conn, watchdog
            except BaseException as exc:
                watchdog.cancel()
                _close(conn)
                fail = _as_fail(exc, watchdog)
                if fail is None:
                    raise
                if fail.code == "tls":
                    raise fail
                last = fail
                logger.info("web fetch connect failed reason=%s; next vetted address",
                            fail.code)
        raise last

    def _read(self, conn, resp, deadline, max_bytes):
        """Up to max_bytes (+1 to detect truncation). read1 returns what one recv gives, so
        a read never waits for a full chunk; the socket timeout is reset to the time left
        before each read and the watchdog covers the rest."""
        buf = bytearray()
        read = getattr(resp, "read1", None) or resp.read
        while len(buf) <= max_bytes:
            _set_timeout(conn, self._left(deadline))
            chunk = read(min(_CHUNK, max_bytes + 1 - len(buf)))
            if not chunk:
                self._left(deadline)          # an EOF at the deadline is the watchdog's
                remaining = getattr(resp, "length", None)
                if (isinstance(remaining, int) and not isinstance(remaining, bool)
                        and remaining > 0):
                    # The server closed before its own Content-Length: an incomplete page
                    # is never stored as a complete snapshot.
                    raise _Fail("truncated_body")
                break
            buf += chunk
            self._left(deadline)
        if len(buf) > max_bytes:
            return bytes(buf[:max_bytes]), True
        return bytes(buf), False

    def _robots_verdict(self, target):
        """None (fetch allowed), 'robots' (disallowed) or 'robots_unavailable'."""
        if target.host not in self._robots:
            self._robots[target.host] = self._load_robots(target.host)
        parser = self._robots[target.host]
        if parser is None:
            return None
        if parser is _ROBOTS_UNAVAILABLE:
            return "robots_unavailable"
        return None if parser.can_fetch(ROBOTS_TOKEN, target.url) else "robots"

    def _load_robots(self, host):
        """RFC 9309 §2.3.1: a parser; None = allow all (any 4xx, a 2xx other than 200,
        more redirects than we follow); _ROBOTS_UNAVAILABLE = disallow all (5xx, timeout,
        network/TLS error, a refused hop, anything else)."""
        deadline = self._clock() + self._timeout
        try:
            authority = f"[{host}]" if ":" in host else host      # an IPv6 literal
            _, _, ctype, raw, _ = self._get(f"https://{authority}/robots.txt", deadline,
                                            max_bytes=MAX_ROBOTS_BYTES, types=None,
                                            robots=False,
                                            max_redirects=MAX_ROBOTS_REDIRECTS)
        except _Fail as exc:
            st = exc.status
            if (exc.code == "http_status" and st is not None
                    and (400 <= st < 500 or 200 <= st < 300)) \
                    or exc.code == "too_many_redirects":
                logger.info("web fetch robots absent reason=%s status=%s -> allow",
                            exc.code, st)
                return None
            logger.info("web fetch robots unreachable reason=%s status=%s -> disallow",
                        exc.code, st)
            return _ROBOTS_UNAVAILABLE
        except Refused as exc:
            logger.info("web fetch robots refused reason=%s -> disallow", exc.code)
            return _ROBOTS_UNAVAILABLE
        except Exception as exc:      # noqa: BLE001
            logger.warning("web fetch robots error type=%s -> disallow", type(exc).__name__)
            return _ROBOTS_UNAVAILABLE
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(_decode(raw, ctype[1]).splitlines())
        parser.modified()              # can_fetch answers False until "checked"
        return parser
