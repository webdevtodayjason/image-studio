"""How this app finds a Tiiny and talks to it.

Copied in shape from tiiny-bench/bench.py: same discovery order, same
port-then-vhost walk, same rule that a refused connection (and only a refused
connection) moves a service onto port 80. load() and unload() are the same
helpers the bench uses, so a start is not reported ready until the model is
actually up.
"""
import errno
import getpass
import glob
import json
import os
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.3.1"

HERE = pathlib.Path(__file__).resolve().parent

CONFIG = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config")
) / "tiiny-image-studio.json"

FARM_DEVICE = pathlib.Path.home() / ".tiinyapps" / "device.json"

DISCO_PORT = 39218
DISCO_PATH = "/device.json"
USB_NET = "172.17."
UDP_PORT = 39217
UDP_TOKEN = b"GADGET_DISCOVER_V1"
PROXY_HOSTS = ("tiiny", "openai.api.tiiny", "tiiny.local")

SERVICES = {
    "gateway": (8800, "p8800.api.tiiny"),
    "openai":  (8800, "openai.api.tiiny"),
    "mgmt":    (80,   None),
}

TRANSPORT = {}
HOST = ""
PORT_OVERRIDE = None
SOURCE = ""
PLANE = ""
DEVICE = {}
KEY_SOURCE = ""

MAX_CAPTURE = 2000
NOT_LOADED_TYPE = "service_unavailable"
# bench.py waits this long between polls, then this long after the id shows
# in /running, because the runtime is listed before it will answer. Tests
# drop these so a load does not take six seconds.
LOAD_POLL_S = 4.0
LOAD_SETTLE_S = 2.0
UNLOAD_SETTLE_S = 1.5
LOAD_WAIT_S = 420


class Call:
    __slots__ = ("service", "path")

    def __init__(self, service, path):
        self.service = service
        self.path = path

    def __str__(self):
        return "%s%s" % (self.service, self.path)


def gw(path):
    return Call("gateway", path)


def mgmt(path):
    return Call("mgmt", path)


def _own_port(service):
    port = SERVICES[service][0]
    if service != "mgmt" and PORT_OVERRIDE:
        return PORT_OVERRIDE
    return port


def _attempts(target):
    if isinstance(target, str):
        return [(target, {}, None)]
    vhost = SERVICES[target.service][1]
    known = TRANSPORT.get(target.service)
    modes = [known] if known else (["direct", "vhost"] if vhost else ["direct"])
    out = []
    for m in modes:
        if m == "vhost" and vhost:
            out.append(("http://%s:80%s" % (HOST, target.path), {"Host": vhost}, m))
        elif m == "direct":
            out.append(("http://%s:%d%s" % (HOST, _own_port(target.service),
                                            target.path), {}, m))
    return out


def _mark(target, mode):
    if mode and not isinstance(target, str):
        TRANSPORT[target.service] = mode


def _refused(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return False
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return True
    return getattr(reason, "errno", None) == errno.ECONNREFUSED


def _config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_config(**kw):
    cfg = _config()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass
    return cfg


def _farm_device():
    try:
        d = json.loads(FARM_DEVICE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _split_base(base):
    base = (base or "").strip()
    if not base:
        return None, None
    if "//" not in base:
        base = "http://" + base
    try:
        u = urllib.parse.urlsplit(base)
        return (u.hostname or None), u.port
    except ValueError:
        return None, None


def device_json(addr, timeout=0.6):
    try:
        with urllib.request.urlopen(
                "http://%s:%d%s" % (addr, DISCO_PORT, DISCO_PATH),
                timeout=timeout) as r:
            d = json.load(r)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict) or not d.get("serial_number"):
        return None
    return d


DISCO_SEEN = {}


def discovered(addr, timeout=1.5):
    if not addr:
        return {}
    if addr not in DISCO_SEEN:
        DISCO_SEEN[addr] = device_json(addr, timeout=timeout) or {}
    return DISCO_SEEN[addr]


def _holds(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _usb_addresses():
    found = []
    for third in range(256):
        for block in range(0, 256, 4):
            for last in (block + 2, block + 1):
                addr = "%s%d.%d" % (USB_NET, third, last)
                if _holds(addr):
                    found.append(addr)
                    break
    return found


def _lan_addresses():
    found = []

    def add(a):
        if (a and not a.startswith("127.") and not a.startswith(USB_NET)
                and a.count(".") == 3 and a not in found):
            found.append(a)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 9))
        add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_DGRAM):
            add(info[4][0])
    except OSError:
        pass
    return found


_IFACES = None


def interfaces(refresh=False):
    global _IFACES
    if _IFACES is None or refresh:
        _IFACES = ([(a, 30) for a in _usb_addresses()]
                   + [(a, 24) for a in _lan_addresses()])
    return _IFACES


def usb_peers():
    peers = []
    for addr, bits in interfaces():
        if bits != 30 or not addr.startswith(USB_NET):
            continue
        try:
            o = [int(x) for x in addr.split(".")]
        except ValueError:
            continue
        n = (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]
        base = n & ~3
        for cand in (base + 1, base + 2):
            if cand != n:
                peers.append("%d.%d.%d.%d" % (
                    (cand >> 24) & 255, (cand >> 16) & 255,
                    (cand >> 8) & 255, cand & 255))
    return peers


def lan_candidates():
    out, seen = [], set()
    for addr, bits in interfaces():
        if addr.startswith("127.") or addr.startswith(USB_NET) or bits >= 31:
            continue
        head = addr.rsplit(".", 1)[0]
        for i in range(1, 255):
            cand = "%s.%d" % (head, i)
            if cand != addr and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def udp_record(data, where):
    try:
        d = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(d, dict) or not d.get("serial_number"):
        return None
    return {"addr": where, "serial": d.get("serial_number"),
            "name": d.get("device_name"), "transport": d.get("transport")}


def udp_targets():
    return ["255.255.255.255"] + usb_peers()


def udp_scan(timeout=1.2, grace=0.4):
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        asked = False
        for t in udp_targets():
            try:
                sock.sendto(UDP_TOKEN, (t, UDP_PORT))
                asked = True
            except OSError:
                continue
        if not asked:
            return found
        seen = set()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                sock.settimeout(max(0.05, left))
                data, where = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            rec = udp_record(data, where[0])
            if not rec or (where[0], rec["serial"]) in seen:
                continue
            seen.add((where[0], rec["serial"]))
            found.append(rec)
            deadline = min(deadline, time.monotonic() + grace)
    finally:
        sock.close()
    return found


def scan(timeout=0.6, workers=64):
    interfaces(refresh=True)
    found = {}
    lock = threading.Lock()

    def keep(rec):
        cur = found.get(rec["serial"])
        if cur is None or (cur["plane"] == "lan" and rec["plane"] == "usb"):
            found[rec["serial"]] = rec

    def probe(addr, plane):
        d = device_json(addr, timeout)
        if not d:
            return
        rec = {"addr": addr, "plane": plane,
               "serial": d.get("serial_number"),
               "name": d.get("device_name"),
               "transport": d.get("transport")}
        with lock:
            keep(rec)

    peers = usb_peers()
    for plane, addrs in (("usb", peers), ("lan", lan_candidates())):
        if plane == "lan":
            for rec in udp_scan():
                rec["plane"] = "usb" if rec["addr"] in peers else "lan"
                with lock:
                    keep(rec)
        for i in range(0, len(addrs), workers):
            ths = [threading.Thread(target=probe, args=(a, plane))
                   for a in addrs[i:i + workers]]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
    return sorted(found.values(), key=lambda r: (r["plane"] != "usb", r["addr"]))


def reachable(host, timeout=2.5):
    vhost = SERVICES["gateway"][1]
    for url, extra in (
            ("http://%s:%d/v1/models" % (host, _own_port("gateway")), {}),
            ("http://%s:80/v1/models" % host, {"Host": vhost})):
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer probe", **extra})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 404:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def connect(host=None, serial=None, rescan=False, quiet=False):
    global HOST, PORT_OVERRIDE, SOURCE, PLANE, DEVICE
    TRANSPORT.clear()
    DISCO_SEEN.clear()
    DEVICE = {}
    env_port = os.environ.get("TIINY_PORT")
    forced_port = int(env_port) if env_port and env_port.isdigit() else None
    PORT_OVERRIDE = forced_port

    def settle(addr, port, source, plane, dev=None):
        global HOST, PORT_OVERRIDE, SOURCE, PLANE, DEVICE
        HOST, SOURCE, PLANE = addr, source, plane
        PORT_OVERRIDE = forced_port or (port if port and port != 80 else None)
        DEVICE = dev or {}
        if not DEVICE.get("serial"):
            d = discovered(addr)
            if d.get("serial_number"):
                DEVICE = {"addr": addr, "plane": plane,
                          "serial": d.get("serial_number"),
                          "name": d.get("device_name"),
                          "transport": d.get("transport")}
        probe_gateway()
        return where()

    base, port = _split_base(os.environ.get("TIINY_BASE")
                             or os.environ.get("TIINY_HOST"))
    if base:
        var = "TIINY_BASE" if os.environ.get("TIINY_BASE") else "TIINY_HOST"
        if host and host != base and not quiet:
            print("  note: %s=%s is being used, not --host %s. "
                  "Unset %s to use the flag." % (var, base, host, var))
        return settle(base, port, var, "given")

    base, port = _split_base(_farm_device().get("base"))
    if base:
        if host and host != base and not quiet:
            print("  note: %s is being used, not --host %s." % (FARM_DEVICE, host))
        return settle(base, port, str(FARM_DEVICE), "given")

    if host:
        addr, port = _split_base(host)
        if not addr:
            sys.exit("--host %r is not an address." % host)
        return settle(addr, port, "--host", "given")

    if not rescan and not serial:
        saved, port = _split_base(_config().get("host"))
        if saved and reachable(saved):
            return settle(saved, port, str(CONFIG), _config().get("plane") or "saved")

    boxes = scan()
    if serial:
        boxes = [b for b in boxes if b["serial"] == serial
                 or b["serial"].endswith(serial)]
        if not boxes:
            sys.exit("no Tiiny with serial %r answered. "
                     "Run without --serial to see what is here." % serial)
    if len(boxes) > 1:
        lines = ["", "  More than one Tiiny answered. Pick one:", ""]
        for b in boxes:
            lines.append("    %-16s %-5s %-24s %s" % (
                b["addr"], b["plane"], b["serial"], b["name"] or ""))
        lines += ["", "    python3 studio.py --serial %s ..." % boxes[0]["serial"],
                  "    python3 studio.py --host %s ..." % boxes[0]["addr"], ""]
        sys.exit("\n".join(lines))
    if boxes:
        b = boxes[0]
        save_config(host=b["addr"], plane=b["plane"])
        return settle(b["addr"], None, "scan", b["plane"], b)

    for name in PROXY_HOSTS:
        if reachable(name, timeout=1.5):
            save_config(host=name, plane="proxy")
            return settle(name, None, "proxy name", "proxy")

    sys.exit(
        "No Tiiny found.\n"
        "  Looked for a USB /30 peer on :%d, asked the responder on :%d, swept "
        "this host's own /24 on :%d, and tried %s.\n"
        "  A box on another network hears none of that: give it an address with "
        "--host, or set TIINY_BASE." % (
            DISCO_PORT, UDP_PORT, DISCO_PORT, ", ".join(PROXY_HOSTS)))


def connect_soft(**kw):
    try:
        return connect(**kw), ""
    except SystemExit as exc:
        return None, str(exc)


def probe_gateway(timeout=3.0):
    api(gw("/v1/models"), "probe", timeout=timeout)
    return TRANSPORT.get("gateway")


def where():
    mode = TRANSPORT.get("gateway") or "unknown"
    return {
        "host": HOST,
        "source": SOURCE,
        "plane": PLANE,
        "serial": DEVICE.get("serial"),
        "gateway_transport": mode,
        "gateway_port": 80 if mode == "vhost" else _own_port("gateway"),
        "gateway_vhost": SERVICES["gateway"][1] if mode == "vhost" else None,
        "services": dict(TRANSPORT),
    }


def identify():
    out = {}
    d = api(mgmt("/api/v1/sys/device_info"), "probe", timeout=5)
    if "_error" not in d:
        out = {"name": d.get("device_name"), "model": d.get("device_model_name"),
               "os": d.get("tiiny_os"), "service": d.get("version"),
               "ram": d.get("ram"), "storage": d.get("storage"),
               "serial": d.get("sn")}
    disco = discovered(HOST)
    if disco:
        if not out.get("name"):
            out["name"] = disco.get("device_name")
        out["serial"] = disco.get("serial_number") or out.get("serial")
        out["transport"] = disco.get("transport")
    return {k: v for k, v in out.items() if v}


UUID_RE = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _tiinyos_keys():
    home = pathlib.Path.home()
    files = sorted(glob.glob(str(
        home / "Library/Application Support/TiinyOS/Local Storage/leveldb/*.ldb")))
    files += sorted(glob.glob(str(
        home / "Library/Application Support/TiinyOS/Local Storage/leveldb/*.log")))
    if not files:
        return []
    counts = {}
    for f in files:
        try:
            blob = pathlib.Path(f).read_bytes()
        except OSError:
            continue
        for h in UUID_RE.findall(blob):
            h = h.decode("ascii")
            counts[h] = counts.get(h, 0) + 1
    return sorted(counts, key=lambda c: -counts[c])


def account_auth_key(addr, serial, password):
    """The device's own static API key, straight from the box, no TiinyOS.

    POST /api/v1/account/auth (password + serial), Host: auth.api.tiiny,
    unlocks /data and hands back `auth_key` -- the same 36-char UUID this
    module calls just "key". Confirmed 2026-09-24 against a live device.
    Full writeup: ~/code/tiiny/tools/README-unlock.md. Returns "" rather
    than raising on any failure, matching the other candidates in key().
    """
    body = json.dumps({"password": password, "device_id": serial}).encode()
    req = urllib.request.Request(
        "http://%s/api/v1/account/auth" % addr, data=body, method="POST",
        headers={"Content-Type": "application/json", "Host": "auth.api.tiiny",
                 "x-device-id": serial, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError):
        return ""
    return (out.get("auth_key") or "").strip()


def key():
    global KEY_SOURCE
    env = os.environ.get("TIINY_KEY", "").strip()
    if env:
        KEY_SOURCE = "TIINY_KEY"
        return env
    farm = (_farm_device().get("key") or "").strip()
    if farm:
        KEY_SOURCE = str(FARM_DEVICE)
        return farm
    saved = (_config().get("key") or "").strip()
    if saved:
        KEY_SOURCE = str(CONFIG)
        return saved
    for c in _tiinyos_keys():
        if "_error" not in api(gw("/api/v1/models/running"), c, timeout=20):
            KEY_SOURCE = "TiinyOS local storage"
            return c
    if HOST and DEVICE.get("serial") and sys.stdin.isatty():
        print("  No API key found. This box's own account can hand one "
              "over -- no TiinyOS needed.")
        try:
            password = getpass.getpass("  Tiiny main password (not echoed): ")
        except (EOFError, KeyboardInterrupt):
            password = ""
        got = account_auth_key(HOST, DEVICE["serial"], password) if password else None
        del password
        if got:
            save_config(key=got)
            KEY_SOURCE = "account API (saved to %s)" % CONFIG
            return got
        print("  That didn't work. Falling through to the usual error.")
    sys.exit("No API key. Set TIINY_KEY, paste one into the web UI, "
             "or run this on the Mac running TiinyOS.")


class _StayOnBox(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.parse.urlsplit(newurl)
        old = urllib.parse.urlsplit(req.full_url)
        if new.netloc and new.netloc != old.netloc:
            newurl = urllib.parse.urlunsplit(
                (old.scheme, old.netloc, new.path, new.query, ""))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_StayOnBox())


def _request(target, tok, body, timeout, method, read, raw=None, ctype=None):
    err = None
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    sent = ctype or ("application/json" if body else None)
    for url, extra, mode in _attempts(target):
        req = urllib.request.Request(
            url, method=method or ("POST" if (body is not None or raw is not None)
                                   else "GET"),
            data=data,
            headers={"Authorization": "Bearer %s" % tok, **extra,
                     **({"Content-Type": sent} if sent else {})})
        try:
            with OPENER.open(req, timeout=timeout) as r:
                _mark(target, mode)
                return r.read() if read else json.load(r)
        except urllib.error.HTTPError as e:
            _mark(target, mode)
            try:
                blob = e.read()[:MAX_CAPTURE]
                text = blob.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                text = ""
            return {"_error": str(e)[:140], "_status": e.code, "_body": text,
                    "_ctype": e.headers.get("Content-Type") if e.headers else None}
        except Exception as e:  # noqa: BLE001
            err = e
            if mode == "direct" and _refused(e) and not isinstance(target, str):
                _mark(target, "vhost")
                continue
            break
    return {"_error": str(err)[:140]}


def api(target, tok, body=None, timeout=900, method=None):
    return _request(target, tok, body, timeout, method, read=False)


def api_raw(target, tok, body=None, timeout=220):
    return _request(target, tok, body, timeout, None, read=True)


def catalog(tok):
    """Everything installed, from the box. There is no hand-kept model list."""
    d = api(gw("/api/v1/models/"), tok, timeout=60)
    if not isinstance(d, dict):
        return []
    if "_error" in d:
        return d
    out = []
    for m in d.get("data") or []:
        if not isinstance(m, dict):
            continue
        row = {k: m.get(k) for k in
               ("id", "display_name", "params", "type", "npu_usage",
                "total_size", "thinking", "version", "status")}
        for k, v in row.items():
            if isinstance(v, str):
                row[k] = " ".join(v.split())
        out.append(row)
    return sorted(out, key=lambda m: -(m.get("total_size") or 0))


def running(tok):
    d = api(gw("/api/v1/models/running"), tok, timeout=30)
    if not isinstance(d, dict) or "_error" in d:
        return []
    return d.get("running") or []


def npu_status(tok):
    s = api(gw("/api/v1/models/npu/status"), tok, timeout=30)
    return {} if not isinstance(s, dict) or "_error" in s else s


def npu_free(tok):
    s = npu_status(tok)
    return s.get("npu_available"), s.get("npu_total")


def running_detail(tok):
    """What is loaded right now, with each instance's units and status."""
    d = api(gw("/api/v1/models/running"), tok, timeout=30)
    if not isinstance(d, dict) or "_error" in d:
        return {}
    return d


def _npu_rows(units):
    return [m for m in (units.get("models") or []) if isinstance(m, dict)]


def _model_status(units, model):
    for m in _npu_rows(units):
        if (m.get("model_id") or m.get("id")) == model:
            return m.get("status") or "running"
    return None


def load(tok, model, poll_s=None):
    """Start a model and wait for it to actually answer.

    Copied from tiiny-bench/bench.py. The runtime is listed in /running before
    it is settled, and a call made in that window comes back 502. Two seconds
    of patience here is cheaper than a generate that looks like a dead model.

    npu/status is polled as well because a start that does not fit is accepted,
    sits there as "loading", and then vanishes. That is the only place the
    rollback can be seen.
    """
    if poll_s is None:
        poll_s = LOAD_WAIT_S
    units = npu_status(tok)
    if model in running(tok) and _model_status(units, model) != "loading":
        return True
    enc = urllib.parse.quote(model, safe="")
    t0 = time.time()
    started = api(gw("/api/v1/models/%s/start" % enc), tok, body={},
                  timeout=min(poll_s, 120))
    if isinstance(started, dict) and started.get("_error"):
        return False
    seen_loading = False
    while time.time() - t0 < poll_s:
        units = npu_status(tok)
        status = _model_status(units, model)
        live = running(tok)
        if status == "loading":
            seen_loading = True
            time.sleep(LOAD_POLL_S)
            continue
        if model in live:
            time.sleep(LOAD_SETTLE_S)
            return True
        names = [(m.get("model_id") or m.get("id")) for m in _npu_rows(units)]
        if seen_loading and model not in live and model not in names:
            return False
        time.sleep(LOAD_POLL_S)
    return False


def unload(tok, model):
    enc = urllib.parse.quote(model, safe="")
    api(gw("/api/v1/models/%s/stop" % enc), tok, body={}, timeout=180)
    time.sleep(UNLOAD_SETTLE_S)


def _error_node(body):
    """The `error` value out of a refusal body, whatever shape it is.

    The device is not consistent about what sits under "error". Every route
    this app drives puts an object there with a message, and /v1/ocr puts a
    bare string: {"error": "Endpoint not found"}, which is what a loaded
    model that does not implement the route answers. A string has no .get,
    the try below only ever caught ValueError, and refusal_text could raise
    AttributeError out of the one function whose whole job is turning a
    refusal into a sentence. Measured 2026-09-19 while building Foolscap.

    A bare string still carries the sentence, so it is returned and read
    rather than discarded.
    """
    try:
        obj = json.loads(body or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    err = obj.get("error")
    return err if isinstance(err, (dict, str)) else {}


def not_loaded(v):
    if not isinstance(v, dict) or "_error" not in v:
        return False
    if v.get("_status") not in (404, 503):
        return False
    err = _error_node(v.get("_body"))
    msg = str((err.get("message") if isinstance(err, dict) else err)
              or v.get("_body") or "").lower()
    return ((err.get("type") if isinstance(err, dict) else None) == NOT_LOADED_TYPE
            or "no suitable model" in msg
            or "is not loaded" in msg
            or "not loaded" in msg)


def refusal_text(v):
    """The device's own sentence. Status codes are not the answer."""
    if not isinstance(v, dict):
        return str(v)[:500]
    body = v.get("_body") or ""
    err = _error_node(body)
    if isinstance(err, str) and err.strip():
        return err.strip()[:500]
    if isinstance(err, dict):
        msg = err.get("message")
        if msg:
            return str(msg)
    if body.strip():
        return body.strip()[:500]
    return v.get("_error") or "the device refused"
