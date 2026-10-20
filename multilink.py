"""
MultiLink - use your hostel Wi-Fi AND your phones' USB tethering at the same time.

No server, no cloud, nothing to install (Python standard library only).

* "Download a file": splits one download into many pieces and fetches the pieces over
  all your connections at once (a fast link simply ends up fetching more pieces).
* "Proxy mode": a small local proxy that sends every NEW connection of a browser/app
  down a different network, so many-connection downloads use all links together.

Run:   pythonw multilink.py            (window)
       python multilink.py --list      (show the connections it can see)
       python multilink.py --cli URL   (download without the window)
       python multilink.py --reset-proxy   (undo the Windows proxy if it got stuck)
"""

import argparse
import http.client
import json
import os
import queue
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit

APP = "MultiLink"
IS_WIN = sys.platform == "win32"
IP_UNICAST_IF = 31          # Windows: force the outgoing network interface per socket
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MultiLink/1.0"


# --------------------------------------------------------------------------- networks
class Iface:
    def __init__(self, name, index, ip, gw="", desc=""):
        self.name, self.index, self.ip, self.gw, self.desc = name, index, ip, gw, desc
        self.enabled = True
        self.total = 0           # bytes moved so far
        self.speed = 0.0         # bytes/s, smoothed
        self.cap = 0.0           # best speed seen (used to balance the proxy)
        self.active = 0          # live proxy connections
        self.fail = 0
        self.down_until = 0.0
        self._lock = threading.Lock()

    def add(self, n):
        with self._lock:
            self.total += n

    def usable(self):
        return self.enabled and time.time() >= self.down_until

    def mark_fail(self):
        self.fail += 1
        if self.fail >= 3:
            self.fail = 0
            self.down_until = time.time() + 20

    def mark_ok(self):
        self.fail = 0

    def kind(self):
        d = (self.desc + " " + self.name).lower()
        if ("rndis" in d or "remote ndis" in d or "android" in d or "tether" in d
                or "apple mobile device" in d or "iphone" in d):
            return "Phone (USB)"
        if "wi-fi" in d or "wifi" in d or "wireless" in d or "wlan" in d:
            return "Wi-Fi"
        if "ethernet" in d:
            return "Wired / USB network"
        return "Network"

    def label(self):
        return "%s  -  %s  (%s)" % (self.kind(), self.name, self.ip)


def discover():
    """Find networks that have an IPv4 address and a gateway (i.e. can reach the internet)."""
    fake = os.environ.get("MULTILINK_FAKE_IFACES")      # used for testing only
    if fake:
        return [Iface("test%d" % i, 0, ip.strip()) for i, ip in enumerate(fake.split(","))]
    if not IS_WIN:
        return []
    ps = (
        "Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and ($_.IPv4DefaultGateway -or "
        "$_.InterfaceDescription -match 'RNDIS|Remote NDIS|NCM|Android|Tether|Apple Mobile') } | "
        "ForEach-Object { [pscustomobject]@{ name=[string]$_.InterfaceAlias; "
        "index=[int]$_.InterfaceIndex; ip=[string]$_.IPv4Address[0].IPAddress; "
        "gw=[string]($_.IPv4DefaultGateway | Select-Object -First 1).NextHop; "
        "desc=[string]$_.InterfaceDescription } } "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=40, creationflags=0x08000000,
        ).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [Iface(d["name"], d["index"], d["ip"], d.get("gw", ""), d.get("desc", "")) for d in data]


def resolve(host, port):
    return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


def connect(iface, ip, port, timeout=15):
    """TCP connection that is forced to leave through `iface`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if IS_WIN and iface.index:
            s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, socket.htonl(iface.index))
        s.bind((iface.ip, 0))
        s.settimeout(timeout)
        s.connect((ip, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s
    except Exception:
        s.close()
        raise


class Monitor(threading.Thread):
    """Updates the speed of every network once a second."""

    def __init__(self, get_ifaces):
        super().__init__(daemon=True)
        self.get = get_ifaces
        self.last = {}

    def run(self):
        while True:
            time.sleep(1.0)
            for i in list(self.get()):
                prev = self.last.get(id(i), i.total)
                rate = max(0, i.total - prev)
                self.last[id(i)] = i.total
                i.speed = i.speed * 0.5 + rate * 0.5
                i.cap = max(i.cap * 0.99, i.speed)


# --------------------------------------------------------------------------- http helpers
def make_ctx(cafile=None):
    return ssl.create_default_context(cafile=cafile)


def make_conn(iface, scheme, host, port, ctx, timeout=20):
    raw = connect(iface, resolve(host, port), port, timeout)
    try:
        if scheme == "https":
            raw = ctx.wrap_socket(raw, server_hostname=host)
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
    except Exception:
        raw.close()
        raise
    conn.sock = raw
    return conn


def split_url(url):
    u = urlsplit(url)
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// links are supported")
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return scheme, u.hostname, port, path


def filename_from(resp, url):
    cd = resp.getheader("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        name = m.group(1) if m else unquote(os.path.basename(urlsplit(url).path))
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "download.bin"
    return name


def probe(url, iface, ctx):
    """Follow redirects; return (final_url, size, supports_ranges, filename)."""
    for _ in range(10):
        scheme, host, port, path = split_url(url)
        conn = make_conn(iface, scheme, host, port, ctx)
        try:
            conn.request("GET", path, headers={"Range": "bytes=0-0", "User-Agent": UA,
                                               "Accept-Encoding": "identity"})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                url = urljoin(url, r.getheader("Location"))
                continue
            if r.status == 206:
                size = int((r.getheader("Content-Range") or "bytes 0-0/-1").split("/")[-1])
                return url, size, size > 0, filename_from(r, url)
            if r.status == 200:
                return url, int(r.getheader("Content-Length") or -1), False, filename_from(r, url)
            raise IOError("The server answered HTTP %d" % r.status)
        finally:
            conn.close()
    raise IOError("Too many redirects")


# --------------------------------------------------------------------------- downloader
class Download:
    def __init__(self, url, folder, ifaces, per_iface=4, cafile=None, log=print):
        self.url, self.folder, self.ifaces = url, folder, ifaces
        self.per_iface, self.log = per_iface, log
        self.ctx = make_ctx(cafile)
        self.cancel = threading.Event()
        self.progress = 0
        self.size = 0
        self.path = None
        self.error = None
        self.finished = False
        self._lock = threading.Lock()
        self._fl = threading.Lock()

    # -- public
    def run(self):
        try:
            self._run()
        except Exception as e:
            self.error = str(e)
            self.log("Stopped: %s" % e)

    # -- internals
    def _run(self):
        active = [i for i in self.ifaces if i.enabled]
        if not active:
            raise RuntimeError("Tick at least one connection first")
        info = None
        for i in active:
            try:
                info = probe(self.url, i, self.ctx)
                break
            except Exception as e:
                self.log("Could not reach the link through %s: %s" % (i.name, e))
        if info is None:
            raise RuntimeError("Could not open the link on any connection")
        self.final_url, self.size, ranges, name = info
        os.makedirs(self.folder, exist_ok=True)
        final = os.path.join(self.folder, name)
        part, state = final + ".part", final + ".mlstate"

        resume = set()
        cs = self._chunk_size(self.size)
        if os.path.exists(part) and os.path.exists(state):
            try:
                st = json.load(open(state))
                if st["size"] == self.size and st["chunk"] == cs:
                    resume = set(st["done"])
                    self.log("Resuming: %d pieces already done" % len(resume))
            except Exception:
                resume = set()
        elif os.path.exists(final):
            base, ext = os.path.splitext(name)
            n = 1
            while os.path.exists(os.path.join(self.folder, "%s (%d)%s" % (base, n, ext))):
                n += 1
            name = "%s (%d)%s" % (base, n, ext)
            final = os.path.join(self.folder, name)
            part, state = final + ".part", final + ".mlstate"
        self.path = final

        if not ranges:
            self.log("This server doesn't allow split downloads - using one connection.")
            self._single(active[0], part)
            os.replace(part, final)
            self.finished = True
            self.log("Done: %s" % final)
            return

        n_chunks = (self.size + cs - 1) // cs
        self.cs, self.n_chunks = cs, n_chunks
        self.done = set(resume)
        self.retries = {}
        self.q = queue.Queue()
        for ci in range(n_chunks):
            if ci not in self.done:
                self.q.put(ci)
        self.progress = sum(min(cs, self.size - ci * cs) for ci in self.done)
        mode = "r+b" if os.path.exists(part) and resume else "w+b"
        self.f = open(part, mode)
        if mode == "w+b":
            self.f.truncate(self.size)
        self.log("%s  (%.1f MB) over %d connection(s)" % (name, self.size / 1e6, len(active)))

        threads = []
        for i in active:
            for _ in range(self.per_iface):
                t = threading.Thread(target=self._worker, args=(i,), daemon=True)
                t.start()
                threads.append(t)
        last_save = time.time()
        while True:
            with self._lock:
                left = n_chunks - len(self.done)
            if left == 0 or self.cancel.is_set() or self.error:
                break
            if not any(t.is_alive() for t in threads):
                self.error = self.error or "All connections stopped working"
                break
            if time.time() - last_save > 2:
                self._save(state, cs)
                last_save = time.time()
            time.sleep(0.2)
        self.cancel.set() if self.error else None
        for t in threads:
            t.join(timeout=3)
        self.f.flush()
        self.f.close()
        if self.error:
            self._save(state, cs)
            raise RuntimeError(self.error)
        if len(self.done) == n_chunks:
            os.replace(part, final)
            if os.path.exists(state):
                os.remove(state)
            self.finished = True
            self.log("Done: %s" % final)
        else:
            self._save(state, cs)
            self.log("Paused - press Start again with the same link to continue.")

    @staticmethod
    def _chunk_size(size):
        return max(512 * 1024, min(8 * 1024 * 1024, size // 200 if size > 0 else 1 << 20))

    def _save(self, state, cs):
        with self._lock:
            data = {"size": self.size, "chunk": cs, "done": sorted(self.done)}
        tmp = state + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, state)

    def _write(self, pos, data):
        with self._fl:
            self.f.seek(pos)
            self.f.write(data)

    def _worker(self, iface):
        scheme, host, port, path = split_url(self.final_url)
        conn = None
        while not self.cancel.is_set():
            if not iface.enabled:
                return
            if not iface.usable():
                time.sleep(0.5)
                continue
            try:
                ci = self.q.get(timeout=0.3)
            except queue.Empty:
                with self._lock:
                    if len(self.done) == self.n_chunks:
                        return
                continue
            a = ci * self.cs
            b = min(a + self.cs, self.size) - 1
            pos = a
            try:
                if conn is None:
                    conn = make_conn(iface, scheme, host, port, self.ctx)
                conn.request("GET", path, headers={"Range": "bytes=%d-%d" % (a, b), "User-Agent": UA,
                                                   "Accept-Encoding": "identity"})
                r = conn.getresponse()
                if r.status != 206:
                    raise IOError("HTTP %d (the link may only work from one network)" % r.status)
                while pos <= b and not self.cancel.is_set():
                    data = r.read(min(65536, b - pos + 1))
                    if not data:
                        raise IOError("connection closed early")
                    self._write(pos, data)
                    pos += len(data)
                    iface.add(len(data))
                    with self._lock:
                        self.progress += len(data)
                if pos <= b:                      # cancelled mid-piece
                    raise InterruptedError()
                r.read()
                iface.mark_ok()
                with self._lock:
                    self.done.add(ci)
            except InterruptedError:
                with self._lock:
                    self.progress -= pos - a
                return
            except Exception as e:
                with self._lock:
                    self.progress -= pos - a
                    n = self.retries[ci] = self.retries.get(ci, 0) + 1
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                self.log("%s: %s" % (iface.name, e))
                iface.mark_fail()
                if n > 25:
                    self.error = "A piece failed too many times: %s" % e
                    return
                self.q.put(ci)
                time.sleep(1)
        if conn is not None:
            conn.close()

    def _single(self, iface, part):
        scheme, host, port, path = split_url(self.final_url)
        conn = make_conn(iface, scheme, host, port, self.ctx)
        try:
            conn.request("GET", path, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            r = conn.getresponse()
            if r.status != 200:
                raise IOError("HTTP %d" % r.status)
            with open(part, "wb") as fh:
                while not self.cancel.is_set():
                    data = r.read(65536)
                    if not data:
                        break
                    fh.write(data)
                    iface.add(len(data))
                    self.progress += len(data)
        finally:
            conn.close()


# --------------------------------------------------------------------------- proxy
def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        d = s.recv(n - len(buf))
        if not d:
            raise IOError("closed")
        buf += d
    return buf


class Proxy:
    """Local HTTP + SOCKS5 proxy (one port). Every new connection goes down the
    network that currently has the most spare capacity."""

    def __init__(self, get_ifaces, port=8080, log=print):
        self.get, self.port, self.log = get_ifaces, port, log
        self.running = False
        self.srv = None
        self.clients = set()
        self._lock = threading.Lock()

    def start(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(256)
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()
        self.log("Proxy listening on 127.0.0.1:%d" % self.port)

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except Exception:
            pass
        with self._lock:
            for c in list(self.clients):
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()
        self.log("Proxy stopped")

    def pick(self, exclude):
        c = [i for i in self.get() if i.usable() and i not in exclude]
        if not c:
            return None
        return min(c, key=lambda i: (i.active + 1) / max(i.cap, 200_000.0))

    def _accept(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        with self._lock:
            self.clients.add(c)
        try:
            c.settimeout(20)
            first = c.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first == b"\x05":
                self._socks(c)
            else:
                self._http(c)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass
            with self._lock:
                self.clients.discard(c)

    def _open(self, host, port):
        ip = resolve(host, port)
        tried = []
        for _ in range(4):
            i = self.pick(tried)
            if i is None:
                break
            tried.append(i)
            try:
                s = connect(i, ip, port, 12)
                i.mark_ok()
                return s, i
            except Exception:
                i.mark_fail()
        return None, None

    def _socks(self, c):
        ver, n = recv_exact(c, 2)
        recv_exact(c, n)
        c.sendall(b"\x05\x00")
        ver, cmd, _, atyp = recv_exact(c, 4)
        if atyp == 1:
            host = socket.inet_ntoa(recv_exact(c, 4))
        elif atyp == 3:
            host = recv_exact(c, recv_exact(c, 1)[0]).decode("idna")
        else:
            c.sendall(b"\x05\x08\x00\x01\0\0\0\0\0\0")
            return
        port = struct.unpack("!H", recv_exact(c, 2))[0]
        if cmd != 1:
            c.sendall(b"\x05\x07\x00\x01\0\0\0\0\0\0")
            return
        try:
            s, i = self._open(host, port)
        except Exception:
            s = None
        if s is None:
            c.sendall(b"\x05\x04\x00\x01\0\0\0\0\0\0")
            return
        c.sendall(b"\x05\x00\x00\x01\0\0\0\0\0\0")
        self._relay(c, s, i)

    def _http(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(8192)
            if not d:
                return
            buf += d
            if len(buf) > 65536:
                return
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            s, i = self._open(host.strip("[]"), int(port))
            if s is None:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                s.sendall(rest)
            self._relay(c, s, i)
            return
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        hdrs = [l for l in lines[1:]
                if not l.lower().startswith(("proxy-connection:", "proxy-authorization:", "connection:"))]
        hdrs.append("Connection: close")
        req = ("%s %s %s\r\n%s\r\n\r\n" % (method, path, version, "\r\n".join(hdrs))).encode("latin-1") + rest
        s, i = self._open(u.hostname, u.port or 80)
        if s is None:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        s.sendall(req)
        self._relay(c, s, i)

    def _relay(self, a, b, iface):
        a.settimeout(None)
        b.settimeout(None)
        with iface._lock:
            iface.active += 1
        try:
            while self.running:
                r, _, _ = select.select([a, b], [], [], 120)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
                    iface.add(len(data))
        except Exception:
            pass
        finally:
            with iface._lock:
                iface.active -= 1
            try:
                b.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- speed test
SPEED_BASE = os.environ.get("MULTILINK_SPEED_URL", "https://speed.cloudflare.com")


class SpeedTest:
    """Ping, jitter and download speed for each connection alone, then all of them together.
    Uses Cloudflare's speed-test server (nearest city is picked automatically)."""

    PHASE_S = 8.0       # seconds of downloading per test
    WARMUP_S = 1.5      # ignored at the start while connections speed up
    PER_IFACE = 4       # parallel connections per network
    CHUNK = 25_000_000  # bytes asked for per request

    def __init__(self, ifaces, cafile=None, log=print):
        self.ifaces = [i for i in ifaces if i.enabled]
        self.ctx = make_ctx(cafile)
        self.log = log
        self.cancel = threading.Event()
        self.results = []
        self.phase = ""
        self.live = 0.0
        self.done = False
        self.error = None
        self.scheme, self.host, self.port, _ = split_url(SPEED_BASE)

    def run(self):
        try:
            if not self.ifaces:
                raise RuntimeError("Tick at least one connection first")
            for i in self.ifaces:
                if self.cancel.is_set():
                    break
                self.phase = "Testing %s..." % getattr(i, "title", i.kind())
                ping, jitter = self._ping(i)
                down = self._download([i]).get(i, 0.0)
                self.results.append({"name": i.label(), "iface": i, "ping": ping, "jitter": jitter, "down": down})
            if len(self.ifaces) > 1 and not self.cancel.is_set():
                self.phase = "Testing all connections together..."
                per = self._download(self.ifaces)
                self.results.append({"name": "ALL COMBINED", "ping": None, "jitter": None,
                                     "down": sum(per.values()), "split": per})
        except Exception as e:
            self.error = str(e)
        finally:
            self.phase = ""
            self.live = 0.0
            self.done = True

    def _ping(self, iface):
        try:
            conn = make_conn(iface, self.scheme, self.host, self.port, self.ctx, timeout=10)
        except Exception as e:
            self.log("%s: could not reach the test server (%s)" % (iface.name, e))
            return None, None
        times = []
        try:
            for _ in range(9):
                if self.cancel.is_set():
                    break
                t = time.perf_counter()
                conn.request("GET", "/__down?bytes=0", headers={"User-Agent": UA})
                conn.getresponse().read()
                times.append((time.perf_counter() - t) * 1000.0)
        except Exception as e:
            self.log("%s: ping failed (%s)" % (iface.name, e))
        finally:
            conn.close()
        times = times[1:]                      # the first request is a warm-up
        if not times:
            return None, None
        ping = sorted(times)[len(times) // 2]
        jitter = sum(abs(a - b) for a, b in zip(times, times[1:])) / max(1, len(times) - 1)
        return ping, jitter

    def _download(self, ifaces):
        counts = {i: 0 for i in ifaces}
        lock = threading.Lock()
        stop = threading.Event()
        reported = set()

        def worker(i):
            conn = None
            while not stop.is_set() and not self.cancel.is_set():
                try:
                    if conn is None:
                        conn = make_conn(i, self.scheme, self.host, self.port, self.ctx, timeout=10)
                    conn.request("GET", "/__down?bytes=%d" % self.CHUNK, headers={"User-Agent": UA})
                    r = conn.getresponse()
                    if r.status != 200:
                        raise IOError("test server answered HTTP %d" % r.status)
                    while not stop.is_set() and not self.cancel.is_set():
                        d = r.read1(65536)
                        if not d:
                            break
                        with lock:
                            counts[i] += len(d)
                        i.add(len(d))
                    if stop.is_set() or self.cancel.is_set():
                        break
                except Exception as e:
                    if i not in reported:
                        reported.add(i)
                        self.log("%s: %s" % (i.name, e))
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    conn = None
                    time.sleep(0.5)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in ifaces for _ in range(self.PER_IFACE)]
        for t in threads:
            t.start()
        start = time.time()
        while time.time() - start < self.WARMUP_S and not self.cancel.is_set():
            time.sleep(0.1)
        with lock:
            c0 = dict(counts)
        t0 = time.time()
        while time.time() - start < self.PHASE_S and not self.cancel.is_set():
            time.sleep(0.25)
            with lock:
                got = sum(counts.values()) - sum(c0.values())
            self.live = got / max(time.time() - t0, 0.01)
        with lock:
            c1 = dict(counts)
        dt = max(time.time() - t0, 0.01)
        stop.set()
        for t in threads:
            t.join(timeout=2)
        self.live = 0.0
        return {i: (c1[i] - c0[i]) / dt for i in ifaces}


# --------------------------------------------------------------------------- Windows proxy
def _backup_file():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "proxy_backup.json")


def _refresh_wininet():
    import ctypes
    w = ctypes.windll.wininet
    w.InternetSetOptionW(0, 39, 0, 0)
    w.InternetSetOptionW(0, 37, 0, 0)


def set_windows_proxy(hostport):
    """Point Windows' own proxy setting at us. The old setting is saved so it can be restored."""
    if not IS_WIN:
        return
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS)
    def get(name, default):
        try:
            return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return default
    if not os.path.exists(_backup_file()):
        json.dump({"enable": get("ProxyEnable", 0), "server": get("ProxyServer", ""),
                   "override": get("ProxyOverride", "")}, open(_backup_file(), "w"))
    winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, hostport)
    winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, "<local>;localhost;127.*;10.*;192.168.*")
    winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
    winreg.CloseKey(k)
    _refresh_wininet()


def restore_windows_proxy():
    if not IS_WIN or not os.path.exists(_backup_file()):
        return
    import winreg
    old = json.load(open(_backup_file()))
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS)
    winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, old.get("server", ""))
    winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(old.get("enable", 0)))
    winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, old.get("override", ""))
    winreg.CloseKey(k)
    os.remove(_backup_file())
    _refresh_wininet()


# --------------------------------------------------------------------------- window
def fmt_speed(bps):
    return "%.1f Mbps" % (bps * 8 / 1e6)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    try:
        restore_windows_proxy()        # in case a previous run was closed abnormally
    except Exception:
        pass

    # ---------------------------------------------------------------- look
    BG = "#141833"        # ink blue window
    SURFACE = "#1E2347"   # panels
    RAISED = "#2A305C"    # inputs, tracks
    LINE = "#3A4278"
    TEXT = "#EEF0FF"
    MUTED = "#9AA2D6"
    ACCENT = "#8B8DFF"    # periwinkle: main buttons, combined total
    GOOD = "#34D399"
    BAD = "#F87171"
    # every connection keeps its own colour everywhere in the app
    LANES = ["#38BDF8", "#F472B6", "#FBBF24", "#34D399", "#A78BFA", "#FB923C"]

    DISPLAY = ("Bahnschrift SemiBold", 30)
    TITLE = ("Bahnschrift SemiBold", 19)
    NUM = ("Bahnschrift SemiBold", 15)
    HEAD = ("Segoe UI Semibold", 11)
    BODY = ("Segoe UI", 10)
    BODY_B = ("Segoe UI Semibold", 10)
    SMALL = ("Segoe UI", 9)

    def mbps(bps):
        return "%.1f" % (bps * 8 / 1e6)

    hub = {"ifaces": [], "loading": True, "found": None}
    Monitor(lambda: hub["ifaces"]).start()

    def find_in_background():
        hub["loading"] = True
        def work():
            try:
                hub["found"] = discover()
            except Exception:
                hub["found"] = []
        threading.Thread(target=work, daemon=True).start()

    def arrange(ifaces):
        """Wi-Fi first, then phones; give each a colour and a short name."""
        order = {"Wi-Fi": 0, "Phone (USB)": 1}
        ifaces = sorted(ifaces, key=lambda i: order.get(i.kind(), 2))
        phones = [i for i in ifaces if i.kind() == "Phone (USB)"]
        wifis = [i for i in ifaces if i.kind() == "Wi-Fi"]
        for n, i in enumerate(ifaces):
            i.color = LANES[n % len(LANES)]
            if i.kind() == "Phone (USB)":
                i.title = "Phone" if len(phones) == 1 else "Phone %d" % (phones.index(i) + 1)
            elif i.kind() == "Wi-Fi":
                i.title = "Wi-Fi" if len(wifis) == 1 else "Wi-Fi %d" % (wifis.index(i) + 1)
            else:
                i.title = i.kind()
        titles = [i.title for i in ifaces]     # make repeated names unique
        seen = {}
        for i in ifaces:
            if titles.count(i.title) > 1:
                seen[i.title] = seen.get(i.title, 0) + 1
                i.title = "%s %d" % (i.title, seen[i.title])
        return ifaces

    root = tk.Tk()
    root.title("MultiLink")
    root.geometry("780x760")
    root.minsize(700, 660)
    root.configure(bg=BG)

    # ---------------------------------------------------------------- small widgets
    KINDS = {"primary": (ACCENT, BG, "#A9ABFF"), "ghost": (RAISED, TEXT, LINE), "stop": (BAD, BG, "#FCA5A5")}

    def button(parent, text, command, kind="primary", small=False):
        b = tk.Button(parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
                      padx=10 if small else 16, pady=3 if small else 7, font=SMALL if small else BODY_B,
                      highlightthickness=0, disabledforeground=MUTED)
        restyle(b, kind)
        b.bind("<Enter>", lambda e: b.config(bg=b.hover) if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.config(bg=b.base))
        return b

    def restyle(b, kind, text=None):
        bg, fg, hover = KINDS[kind]
        b.base, b.hover = bg, hover
        b.config(bg=bg, fg=fg, activebackground=hover, activeforeground=fg)
        if text:
            b.config(text=text)

    def enable(b, on):
        b.config(state="normal" if on else "disabled", bg=b.base if on else RAISED)

    def eta(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return "%d s" % max(seconds, 1)
        if seconds < 3600:
            return "%d min" % round(seconds / 60.0)
        return "%d h %d min" % (seconds // 3600, (seconds % 3600) // 60)

    def entry(parent, var, width=None):
        e = tk.Entry(parent, textvariable=var, bg=RAISED, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=BODY, highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        if width:
            e.config(width=width)
        return e

    def label(parent, text="", font=BODY, fg=TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"], **kw)

    class Switch(tk.Canvas):
        def __init__(self, parent, color, on=True, command=None):
            super().__init__(parent, width=40, height=22, bg=parent["bg"], cursor="hand2", takefocus=1,
                             highlightthickness=1, highlightbackground=parent["bg"], highlightcolor=ACCENT)
            self.color, self.on, self.command = color, on, command
            self.bind("<Button-1>", self.flip)
            self.bind("<space>", self.flip)
            self.draw()

        def draw(self):
            self.delete("all")
            c = self.color if self.on else LINE
            self.create_oval(2, 2, 20, 20, fill=c, outline=c)
            self.create_oval(20, 2, 38, 20, fill=c, outline=c)
            self.create_rectangle(11, 2, 29, 20, fill=c, outline=c)
            x = 29 if self.on else 11
            self.create_oval(x - 7, 4, x + 7, 18, fill=BG if self.on else MUTED, outline="")

        def flip(self, _=None):
            self.on = not self.on
            self.draw()
            if self.command:
                self.command(self.on)

    def draw_split_bar(canvas, parts, fill_fraction=1.0, empty_text=""):
        """parts = [(colour, amount), ...]; drawn side by side across fill_fraction of the width."""
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 10), max(canvas.winfo_height(), 6)
        canvas.create_rectangle(0, 0, w, h, fill=RAISED, outline="")
        total = sum(a for _, a in parts)
        if total <= 0 or fill_fraction <= 0:
            if empty_text:
                canvas.create_text(10, h / 2, text=empty_text, anchor="w", fill=MUTED, font=SMALL)
            return
        x, span = 0.0, w * min(fill_fraction, 1.0)
        for colour, a in parts:
            seg = span * a / total
            if seg >= 1:
                canvas.create_rectangle(x, 0, x + seg, h, fill=colour, outline="")
                if x > 0:
                    canvas.create_line(x, 0, x, h, fill=BG, width=2)
            x += seg

    # ---------------------------------------------------------------- header
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=22, pady=(18, 6))
    left = tk.Frame(head, bg=BG)
    left.pack(side="left", anchor="n")
    label(left, "MultiLink", TITLE).pack(anchor="w")
    label(left, "Your Wi-Fi and phones, working as one connection", SMALL, MUTED).pack(anchor="w")
    right = tk.Frame(head, bg=BG)
    right.pack(side="right", anchor="n")
    big = tk.Frame(right, bg=BG)
    big.pack(anchor="e")
    total_lab = label(big, "0.0", DISPLAY)
    total_lab.pack(side="left")
    label(big, " Mbps", BODY, MUTED).pack(side="left", anchor="s", pady=(0, 8))
    label(right, "combined right now", SMALL, MUTED).pack(anchor="e")

    merge = tk.Canvas(root, height=18, bg=BG, highlightthickness=0)
    merge.pack(fill="x", padx=22, pady=(4, 14))

    # ---------------------------------------------------------------- connections
    conn_head = tk.Frame(root, bg=BG)
    conn_head.pack(fill="x", padx=22)
    label(conn_head, "Connections", HEAD).pack(side="left")
    state = {"dl": None, "proxy": None, "winproxy": False, "st": None, "st_shown": 0, "dl_base": {}}

    def refresh():
        if state["dl"] or state["proxy"] or state["st"]:
            messagebox.showinfo(APP, "Stop the download, proxy or speed test first, then refresh.")
            return
        find_in_background()
        build_rows()

    button(conn_head, "Refresh", refresh, "ghost", small=True).pack(side="right")
    lanes_box = tk.Frame(root, bg=BG)
    lanes_box.pack(fill="x", padx=22, pady=(8, 0))
    label(root, "Phones connect through USB tethering, not the Wi-Fi hotspot.", SMALL, MUTED).pack(
        anchor="w", padx=22, pady=(4, 12))
    rows = []

    def build_rows():
        for w in lanes_box.winfo_children():
            w.destroy()
        rows.clear()
        if hub["loading"]:
            box = tk.Frame(lanes_box, bg=SURFACE)
            box.pack(fill="x")
            label(box, "Looking for your connections...", BODY, MUTED).pack(anchor="w", padx=14, pady=14)
            return
        if not hub["ifaces"]:
            box = tk.Frame(lanes_box, bg=SURFACE)
            box.pack(fill="x")
            label(box, "No connections found. Join the Wi-Fi, plug in a phone, turn on USB tethering, "
                       "then press Refresh.", BODY, MUTED, wraplength=640, justify="left").pack(
                anchor="w", padx=14, pady=14)
            return
        for i in hub["ifaces"]:
            row = tk.Frame(lanes_box, bg=SURFACE)
            row.pack(fill="x", pady=3)
            stripe = tk.Frame(row, bg=i.color, width=5)
            stripe.pack(side="left", fill="y")

            def on_toggle(on, i=i, stripe=stripe):
                i.enabled = on
                stripe.config(bg=i.color if on else LINE)

            sw = Switch(row, i.color, on=i.enabled, command=on_toggle)
            sw.pack(side="left", padx=(12, 10), pady=12)
            names = tk.Frame(row, bg=SURFACE)
            names.pack(side="left", fill="x", expand=True)
            label(names, i.title, BODY_B).pack(anchor="w")
            label(names, "%s   %s" % (i.name, i.ip), SMALL, MUTED).pack(anchor="w")
            nums = tk.Frame(row, bg=SURFACE)
            nums.pack(side="right", padx=14)
            sp = tk.Frame(nums, bg=SURFACE)
            sp.pack(anchor="e")
            speed = label(sp, "0.0", NUM, i.color)
            speed.pack(side="left")
            unit = label(sp, " Mbps", SMALL, MUTED)
            unit.pack(side="left", anchor="s", pady=(0, 3))
            used = label(nums, "", SMALL, MUTED)
            used.pack(anchor="e")
            rows.append((i, speed, unit, used))

    find_in_background()
    build_rows()

    # ---------------------------------------------------------------- tabs
    tabbar = tk.Frame(root, bg=BG)
    tabbar.pack(fill="x", padx=22)
    body = tk.Frame(root, bg=SURFACE)
    body.pack(fill="both", expand=True, padx=22, pady=(0, 18))
    pages, tab_btns = {}, {}

    def show_tab(name):
        for n, (b, line) in tab_btns.items():
            on = n == name
            b.config(bg=SURFACE if on else BG, fg=TEXT if on else MUTED)
            line.config(bg=ACCENT if on else BG)
        for n, f in pages.items():
            f.pack_forget()
        pages[name].pack(fill="both", expand=True, padx=18, pady=16)

    for key, text in (("dl", "Download"), ("proxy", "Proxy mode"), ("speed", "Speed test")):
        holder = tk.Frame(tabbar, bg=BG)
        holder.pack(side="left")
        line = tk.Frame(holder, height=2, bg=BG)
        line.pack(fill="x", side="top")
        b = tk.Button(holder, text=text, font=BODY_B, relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
                      bg=BG, fg=MUTED, activebackground=SURFACE, activeforeground=TEXT, highlightthickness=0,
                      command=lambda k=key: show_tab(k))
        b.pack(side="top")
        tab_btns[key] = (b, line)
        pages[key] = tk.Frame(body, bg=SURFACE)

    logq = queue.Queue()

    def log(msg):
        logq.put(msg)

    # ---------------------------------------------------------------- download page
    t1 = pages["dl"]
    label(t1, "Download link", BODY_B).pack(anchor="w")
    url_var = tk.StringVar()
    entry(t1, url_var).pack(fill="x", ipady=6, pady=(4, 12))
    label(t1, "Save to", BODY_B).pack(anchor="w")
    fr = tk.Frame(t1, bg=SURFACE)
    fr.pack(fill="x", pady=(4, 14))
    folder_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
    entry(fr, folder_var).pack(side="left", fill="x", expand=True, ipady=6)
    button(fr, "Browse", lambda: folder_var.set(
        filedialog.askdirectory(initialdir=folder_var.get()) or folder_var.get()), "ghost").pack(side="left", padx=(8, 0))
    br = tk.Frame(t1, bg=SURFACE)
    br.pack(fill="x")
    start_btn = button(br, "Start download", None)
    start_btn.pack(side="left")
    stop_btn = button(br, "Pause", None, "ghost")
    enable(stop_btn, False)
    stop_btn.pack(side="left", padx=8)
    pbar = tk.Canvas(t1, height=12, bg=SURFACE, highlightthickness=0)
    pbar.pack(fill="x", pady=(16, 6))
    status = label(t1, "Paste a direct link to a big file, then press Start download.", BODY)
    status.pack(anchor="w")
    status2 = label(t1, "", SMALL, MUTED)
    status2.pack(anchor="w")
    logbox = tk.Text(t1, height=4, state="disabled", wrap="word", bg=BG, fg=MUTED, font=SMALL, relief="flat",
                     padx=10, pady=8, highlightthickness=0)
    logbox.pack(fill="both", expand=True, pady=(10, 0))

    def start_dl():
        url = url_var.get().strip()
        if not url:
            messagebox.showinfo(APP, "Paste a download link first.")
            return
        if state["st"]:
            messagebox.showinfo(APP, "Wait for the speed test to finish first.")
            return
        dl = Download(url, folder_var.get(), hub["ifaces"], log=log)
        state["dl"] = dl
        state["dl_base"] = {id(i): i.total for i in hub["ifaces"]}
        enable(start_btn, False)
        enable(stop_btn, True)
        status.config(text="Starting...")
        status2.config(text="")
        def work():
            dl.run()
            logq.put("__done__")
        threading.Thread(target=work, daemon=True).start()

    def stop_dl():
        if state["dl"]:
            state["dl"].cancel.set()

    start_btn.config(command=start_dl)
    stop_btn.config(command=stop_dl)

    # ---------------------------------------------------------------- proxy page
    t2 = pages["proxy"]
    label(t2, "Lets browsers and apps use every connection", HEAD).pack(anchor="w")
    label(t2, "Each new connection an app opens goes out through a different network, so downloads "
              "that open many connections (Chrome, Edge, most launchers, torrents) use them all. One single "
              "stream, like cloud gaming or one video, still uses just one network.",
          BODY, MUTED, wraplength=660, justify="left").pack(anchor="w", pady=(4, 14))
    pr = tk.Frame(t2, bg=SURFACE)
    pr.pack(fill="x")
    label(pr, "Port", BODY_B).pack(side="left")
    port_var = tk.StringVar(value="8080")
    entry(pr, port_var, width=7).pack(side="left", padx=(8, 12), ipady=5)
    proxy_btn = button(pr, "Start proxy", None)
    proxy_btn.pack(side="left")
    pstat = label(pr, "Not running", BODY, MUTED)
    pstat.pack(side="left", padx=14)
    wrow = tk.Frame(t2, bg=SURFACE)
    wrow.pack(fill="x", pady=(14, 0))
    win_switch = Switch(wrow, ACCENT, on=True)
    win_switch.pack(side="left")
    label(wrow, "Point Windows' proxy setting at MultiLink while it runs (put back when you stop)",
          BODY).pack(side="left", padx=8)
    label(t2, "For an app with its own proxy option, use HTTP proxy 127.0.0.1 and the port above "
              "(SOCKS5 works on the same port).\nIf the internet stops working after MultiLink closed "
              "unexpectedly, open MultiLink again and it puts the setting back.",
          SMALL, MUTED, wraplength=660, justify="left").pack(anchor="w", pady=(18, 0))

    def toggle_proxy():
        if state["proxy"]:
            state["proxy"].stop()
            state["proxy"] = None
            if state["winproxy"]:
                restore_windows_proxy()
                state["winproxy"] = False
            restyle(proxy_btn, "primary", "Start proxy")
            pstat.config(text="Not running", fg=MUTED)
            return
        try:
            p = Proxy(lambda: hub["ifaces"], int(port_var.get()), log=log)
            p.start()
        except Exception as e:
            messagebox.showerror(APP, "Couldn't start the proxy: %s" % e)
            return
        state["proxy"] = p
        if win_switch.on:
            try:
                set_windows_proxy("127.0.0.1:%d" % p.port)
                state["winproxy"] = True
            except Exception as e:
                messagebox.showwarning(APP, "The proxy is running, but Windows' setting wasn't changed: %s" % e)
        restyle(proxy_btn, "stop", "Stop proxy")
        pstat.config(text="Running on 127.0.0.1:%d" % p.port, fg=GOOD)

    proxy_btn.config(command=toggle_proxy)

    # ---------------------------------------------------------------- speed test page
    t3 = pages["speed"]
    label(t3, "Tests each connection on its own, then all of them together, against Cloudflare's nearest "
              "server. About 10 seconds per connection; uses a little mobile data.",
          BODY, MUTED, wraplength=660, justify="left").pack(anchor="w")
    sb = tk.Frame(t3, bg=SURFACE)
    sb.pack(fill="x", pady=(12, 8))
    st_btn = button(sb, "Run speed test", None)
    st_btn.pack(side="left")
    st_live = label(sb, "", ("Bahnschrift SemiBold", 20), ACCENT)
    st_live.pack(side="left", padx=(18, 6))
    st_phase = label(sb, "", SMALL, MUTED)
    st_phase.pack(side="left")
    table = tk.Frame(t3, bg=SURFACE)
    table.pack(fill="x")
    table.columnconfigure(1, weight=1)
    st_summary = label(t3, "", BODY, TEXT, wraplength=660, justify="left")
    st_summary.pack(anchor="w", pady=(12, 4))
    label(t3, "Lower ping and jitter mean smoother cloud gaming. Cloud gaming uses one connection, so use "
              "the one with the best ping. The combined speed is what big downloads in MultiLink can reach.",
          SMALL, MUTED, wraplength=660, justify="left").pack(anchor="w")

    def ms(v):
        return "-" if v is None else "%.0f ms" % v

    def table_header():
        for w in table.winfo_children():
            w.destroy()
        for col, text in ((1, "Connection"), (2, "Ping"), (3, "Jitter"), (4, "Download"), (5, "")):
            label(table, text, SMALL, MUTED).grid(row=0, column=col, sticky="w", padx=6, pady=(0, 4))
        table.columnconfigure(5, minsize=160)

    def table_row(n, r, best):
        combined = "split" in r
        colour = ACCENT if combined else getattr(r.get("iface"), "color", ACCENT)
        dot = tk.Canvas(table, width=12, height=12, bg=SURFACE, highlightthickness=0)
        dot.create_oval(1, 1, 11, 11, fill=colour, outline="")
        dot.grid(row=n, column=0, padx=(0, 4))
        name = "All combined" if combined else getattr(r.get("iface"), "title", r["name"])
        label(table, name, BODY_B if combined else BODY).grid(row=n, column=1, sticky="w", padx=6, pady=4)
        label(table, ms(r["ping"])).grid(row=n, column=2, sticky="w", padx=6)
        label(table, ms(r["jitter"])).grid(row=n, column=3, sticky="w", padx=6)
        label(table, "%s Mbps" % mbps(r["down"]), BODY_B, colour).grid(row=n, column=4, sticky="w", padx=6)
        bar = tk.Canvas(table, width=150, height=10, bg=SURFACE, highlightthickness=0)
        bar.grid(row=n, column=5, sticky="w", padx=6)
        frac = r["down"] / best if best > 0 else 0
        parts = ([(getattr(i, "color", ACCENT), v) for i, v in r["split"].items()] if combined
                 else [(colour, 1)])
        bar.update_idletasks()
        draw_split_bar(bar, parts, frac)

    def toggle_speedtest():
        if state["st"]:
            state["st"].cancel.set()
            enable(st_btn, False)
            return
        if state["dl"]:
            messagebox.showinfo(APP, "Pause the download first - it would slow the test down.")
            return
        test = SpeedTest(hub["ifaces"], log=log)
        if not test.ifaces:
            messagebox.showinfo(APP, "Switch on at least one connection first.")
            return
        table_header()
        st_summary.config(text="")
        state["st"], state["st_shown"] = test, 0
        state["st_best"] = 1.0
        restyle(st_btn, "stop", "Stop")
        threading.Thread(target=test.run, daemon=True).start()

    st_btn.config(command=toggle_speedtest)

    def update_speedtest():
        test = state["st"]
        if not test:
            return
        st_live.config(text="%s Mbps" % mbps(test.live) if test.live else "")
        st_phase.config(text=test.phase)
        if state["st_shown"] < len(test.results):
            state["st_shown"] = len(test.results)
            best = max(r["down"] for r in test.results) or 1.0
            table_header()
            for n, r in enumerate(test.results, start=1):
                table_row(n, r, best)
        if test.done:
            state["st"] = None
            restyle(st_btn, "primary", "Run speed test")
            enable(st_btn, True)
            st_live.config(text="")
            singles = [r for r in test.results if "split" not in r]
            combined = [r for r in test.results if "split" in r]
            msg = ""
            if test.error:
                msg = "The test stopped: %s" % test.error
            elif singles:
                best = max(singles, key=lambda r: r["down"])
                pinged = [r for r in singles if r["ping"] is not None]
                if combined and best["down"] > 0:
                    gain = (combined[0]["down"] / best["down"] - 1) * 100
                    msg = "Together: %s Mbps, %+.0f%% compared with %s alone. " % (
                        mbps(combined[0]["down"]), gain, getattr(best.get("iface"), "title", best["name"]))
                if pinged:
                    g = min(pinged, key=lambda r: (r["ping"] + 2 * (r["jitter"] or 0)))
                    msg += "Best for cloud gaming: %s." % getattr(g.get("iface"), "title", g["name"])
            st_summary.config(text=msg)

    # ---------------------------------------------------------------- live updates
    def tick():
        update_speedtest()
        if hub["found"] is not None:
            hub["ifaces"] = arrange(hub["found"])
            hub["found"], hub["loading"] = None, False
            build_rows()
        live = [i for i in hub["ifaces"] if i.enabled]
        total_lab.config(text=mbps(sum(i.speed for i in live)))
        draw_split_bar(merge, [(i.color, i.speed) for i in live], 1.0,
                       "Waiting for traffic - start a download, the proxy or a speed test")
        for i, speed, unit, used in rows:
            if i.enabled:
                speed.config(text=mbps(i.speed), fg=i.color)
                unit.config(text=" Mbps")
                used.config(text="%.0f MB used" % (i.total / 1e6))
            else:
                speed.config(text="Off", fg=MUTED)
                unit.config(text="")
                used.config(text="")
        while True:
            try:
                m = logq.get_nowait()
            except queue.Empty:
                break
            if m == "__done__":
                dl = state["dl"]
                state["dl"] = None
                enable(start_btn, True)
                enable(stop_btn, False)
                if dl and dl.finished:
                    status.config(text="Finished")
                    status2.config(text=dl.path)
                elif dl:
                    status.config(text="Paused" if not dl.error else "Stopped")
                    status2.config(text="Press Start download with the same link to continue." if not dl.error
                                   else dl.error)
                continue
            logbox.config(state="normal")
            logbox.insert("end", m + "\n")
            logbox.see("end")
            logbox.config(state="disabled")
        dl = state["dl"]
        if dl and dl.size > 0:
            base = state["dl_base"]
            parts = [(i.color, max(0, i.total - base.get(id(i), i.total))) for i in hub["ifaces"]]
            if sum(a for _, a in parts) <= 0:
                parts = [(ACCENT, 1)]
            draw_split_bar(pbar, parts, dl.progress / dl.size)
            total = sum(i.speed for i in live)
            left = (dl.size - dl.progress) / total if total > 1 else 0
            status.config(text="%.0f%%   %.1f of %.1f MB" % (100.0 * dl.progress / dl.size,
                                                          dl.progress / 1e6, dl.size / 1e6))
            status2.config(text="%s Mbps   about %s left" % (mbps(total), eta(left)) if left
                           else "%s Mbps" % mbps(total))
        elif dl is None and not str(status.cget("text")).startswith(("Finished", "Paused", "Stopped")):
            draw_split_bar(pbar, [])
        root.after(500, tick)

    def on_close():
        if state["dl"]:
            state["dl"].cancel.set()
        if state["st"]:
            state["st"].cancel.set()
        if state["proxy"]:
            state["proxy"].stop()
        if state["winproxy"]:
            restore_windows_proxy()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    show_tab("dl")
    tick()
    if os.environ.get("MULTILINK_SELFTEST_URL"):          # automated test only
        url_var.set(os.environ["MULTILINK_SELFTEST_URL"])
        folder_var.set(os.environ.get("MULTILINK_SELFTEST_DIR", folder_var.get()))
        root.after(1500, start_dl)
        root.after(int(os.environ.get("MULTILINK_SELFTEST_MS", "9000")), on_close)
    elif os.environ.get("MULTILINK_SELFTEST_SPEED"):      # automated test only
        show_tab("speed")
        root.after(1500, toggle_speedtest)
        root.after(int(os.environ.get("MULTILINK_SELFTEST_MS", "30000")), on_close)
    elif os.environ.get("MULTILINK_SELFTEST_TAB"):        # automated test only
        show_tab(os.environ["MULTILINK_SELFTEST_TAB"])
        root.after(2500, on_close)
    elif os.environ.get("MULTILINK_SELFTEST"):
        root.after(2500, on_close)
    root.mainloop()


# --------------------------------------------------------------------------- command line
def main():
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--list", action="store_true", help="show the connections that can be used")
    ap.add_argument("--cli", metavar="URL", help="download a link without opening the window")
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "Downloads"))
    ap.add_argument("--per", type=int, default=4, help="connections per network")
    ap.add_argument("--cafile", help=argparse.SUPPRESS)
    ap.add_argument("--proxy", type=int, metavar="PORT", help="run only the proxy")
    ap.add_argument("--speedtest", action="store_true", help="test each connection and all combined")
    ap.add_argument("--reset-proxy", action="store_true", help="undo the Windows proxy setting")
    a = ap.parse_args()

    if a.reset_proxy:
        restore_windows_proxy()
        print("Windows proxy setting restored.")
        return
    ifaces = discover()
    if a.list:
        if not ifaces:
            print("No usable connections found.")
        for i in ifaces:
            print("%-14s %-16s index=%s  %s" % (i.kind(), i.ip, i.index, i.name))
        return
    if a.speedtest:
        t = SpeedTest(ifaces, cafile=a.cafile, log=print)
        th = threading.Thread(target=t.run, daemon=True)
        th.start()
        shown = 0
        while th.is_alive() or shown < len(t.results):
            time.sleep(0.5)
            while shown < len(t.results):
                r = t.results[shown]
                shown += 1
                print("%-40s ping %-7s jitter %-7s download %s" % (
                    r["name"][:40], "-" if r["ping"] is None else "%.0fms" % r["ping"],
                    "-" if r["jitter"] is None else "%.0fms" % r["jitter"], fmt_speed(r["down"])))
        if t.error:
            print("Error:", t.error)
        return
    if a.cli or a.proxy:
        Monitor(lambda: ifaces).start()
    if a.cli:
        if not ifaces:
            sys.exit("No usable connections found.")
        d = Download(a.cli, a.out, ifaces, per_iface=a.per, cafile=a.cafile, log=print)
        t0 = time.time()
        th = threading.Thread(target=d.run, daemon=True)
        th.start()
        while th.is_alive():
            time.sleep(1)
            sys.stdout.write("\r%5.1f%%  %s" % (100.0 * d.progress / max(d.size, 1),
                                                "  ".join("%s %s" % (i.ip, fmt_speed(i.speed)) for i in ifaces)))
            sys.stdout.flush()
        print()
        print("%s in %.1fs" % ("OK" if d.finished else "NOT finished", time.time() - t0))
        for i in ifaces:
            print("  %-16s %8.1f MB" % (i.ip, i.total / 1e6))
        sys.exit(0 if d.finished else 1)
    if a.proxy:
        p = Proxy(lambda: ifaces, a.proxy)
        p.start()
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            p.stop()
        return
    try:
        run_gui()
    except Exception:
        import traceback
        err = traceback.format_exc()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multilink_error.txt")
        try:
            with open(path, "w") as fh:
                fh.write(err)
        except Exception:
            pass
        if IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "MultiLink could not start:\n\n%s\n\nSaved to %s" % (
                err.strip().splitlines()[-1], path), APP, 0x10)
        raise


if __name__ == "__main__":
    main()
