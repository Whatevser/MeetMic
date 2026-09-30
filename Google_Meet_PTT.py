"""Google Meet PTT — system-wide hard push-to-talk for Google Meet (Windows only).

The PTT key is read with GetAsyncKeyState (no keyboard hook). The "Google Meet PTT"
Tampermonkey script long-polls this app on 127.0.0.1:8875 and drives Meet's mic button.

Tray icon is the only indicator:
    mic.ico          armed, mic muted
    redmic.ico       mic is live in Meet
    fadedmic.ico     not working (reason in the tooltip and in Settings)
"""
import ctypes
import ctypes.wintypes as wt
import errno
import json
import os
import queue
import signal
import socket
import socketserver
import sys
import threading
import time
import tkinter as tk
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk

if os.name != "nt":
    raise SystemExit("Google Meet PTT працює лише на Windows.")

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

APP_NAME = "Google Meet PTT"
APP_ID = "roman.googlemeet.ptt"
HOST = "127.0.0.1"
PORT = 8875
PROTO = 2                         # must match PROTO in the userscript
AUTH_HEADER = "X-GMeet-PTT"       # web pages can't send custom headers cross-origin, so they can't drive us
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}  # blocks DNS-rebinding
LEGACY_PATHS = {"/mic-command", "/meet-status", "/browser-key"}  # old userscript endpoints

POLL_HOLD_S = 8.0                 # long-poll hold; the script re-polls instantly
CLIENT_STALE_S = 12.0             # a tab silent this long is gone (must be > POLL_HOLD_S)
LEGACY_WARN_S = 10.0

ICON_FILE = "mic.ico"
MIC_ON_ICON_FILE = "redmic.ico"
FADED_ICON_FILE = "fadedmic.ico"

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "GoogleMeetPTT")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

PTT_KEYS = {  # name -> (virtual-key code, label)
    "Insert": (0x2D, "Insert"),
    "~": (0xC0, "~"),
    "LShift": (0xA0, "Лівий Shift"),
    "RShift": (0xA1, "Правий Shift"),
    "CapsLock": (0x14, "Caps Lock"),
    "Tab": (0x09, "Tab"),
    "F8": (0x77, "F8"),
    "F9": (0x78, "F9"),
    "F10": (0x79, "F10"),
    "F11": (0x7A, "F11"),
    "F12": (0x7B, "F12"),
    "Pause": (0x13, "Pause"),
    "Scroll Lock": (0x91, "Scroll Lock"),
    "Home": (0x24, "Home"),
    "End": (0x23, "End"),
    "Page Up": (0x21, "Page Up"),
    "Page Down": (0x22, "Page Down"),
}

CLIENT_ERRORS = {
    "mic-unresponsive": "Meet не реагує на кнопку мікрофона",
    "mic-state-unknown": "не можу прочитати стан мікрофона",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]

stop_event = threading.Event()
ui_queue = queue.Queue()          # everything that touches Tk goes through here
tk_root = None
tray_icon = None
ICONS = {}


def message_box(text):
    user32.MessageBoxW(None, text, APP_NAME, 0x10 | 0x10000 | 0x40000)  # ICONERROR | SETFOREGROUND | TOPMOST


# ---------------------------------------------------------------- settings

def key_label(name):
    return PTT_KEYS[name][1] if name in PTT_KEYS else name


def normalize_settings(data):
    d = data if isinstance(data, dict) else {}
    raw_keys = d.get("ptt_keys") if isinstance(d.get("ptt_keys"), list) else []
    keys = list(dict.fromkeys(k for k in raw_keys if isinstance(k, str) and k in PTT_KEYS)) or ["Insert"]
    try:
        poll_ms = min(250, max(10, int(d.get("ptt_poll_ms", 20))))
    except (TypeError, ValueError):
        poll_ms = 20
    return {
        "ptt_enabled": bool(d.get("ptt_enabled", True)),
        "ptt_keys": keys,
        "ptt_multi_mode": "all" if d.get("ptt_multi_mode") == "all" else "any",
        "ptt_poll_ms": poll_ms,
    }


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return normalize_settings(json.load(f))
    except (OSError, ValueError):
        return normalize_settings({})


def save_settings(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_PATH)  # atomic: a crash mid-write can't corrupt the file
    except OSError as e:
        print(f"[SETTINGS] save failed: {e}", flush=True)


settings = load_settings()  # replaced wholesale, never mutated, so other threads read it without locks
settings_lock = threading.Lock()


# ---------------------------------------------------------------- shared state

class Hub:
    """State shared by the key poller, the HTTP server and the tray."""

    def __init__(self, enabled):
        self.cond = threading.Condition()
        self.version = 0          # bumped on any change a tab must react to
        self.enabled = enabled
        self.pressed = False
        self.clients = {}         # tab id -> status reported by the userscript
        self.target = None        # the one tab allowed to open its mic
        self.legacy_at = -1e9

    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    def set_pressed(self, pressed, down):
        pressed = pressed and not stop_event.is_set()  # nothing can re-open the mic once quitting
        with self.cond:
            if pressed == self.pressed:
                return
            self.pressed = pressed
            self._bump()
        print(f"[PTT] {'DOWN' if pressed else 'UP'} ({'+'.join(down) or '-'})", flush=True)

    def set_enabled(self, enabled):
        with self.cond:
            if enabled == self.enabled:
                return
            self.enabled = enabled
            self.pressed = self.pressed and enabled
            self._bump()

    def release_all(self):
        with self.cond:
            self.pressed = False
            self._bump()

    def mark_legacy(self):
        with self.cond:
            self.legacy_at = time.monotonic()

    def update_client(self, cid, d):
        now = time.monotonic()
        with self.cond:
            c = self.clients.get(cid)
            if c is None:
                if len(self.clients) >= 32:
                    return
                c = self.clients[cid] = {"joinedAt": now, "focusedAt": 0.0}
            c.update(
                seen=now,
                hasMic=bool(d.get("hasMic")),
                micOn=bool(d.get("micOn")),
                focused=bool(d.get("focused")),
                error=str(d.get("error") or "")[:40],
                url=str(d.get("url") or "")[:200],
            )
            if c["focused"]:
                c["focusedAt"] = now
            self._retarget()

    def drop_client(self, cid):
        with self.cond:
            if self.clients.pop(cid, None) is not None:
                self._retarget()

    def _retarget(self):
        # Drop silent tabs; the target is the most recently focused tab that has a mic button.
        now = time.monotonic()
        for cid in [cid for cid, c in self.clients.items() if now - c["seen"] > CLIENT_STALE_S]:
            del self.clients[cid]
        candidates = [(cid, c) for cid, c in self.clients.items() if c["hasMic"]]
        target = None
        if candidates:
            target = max(candidates, key=lambda kv: (kv[1]["focused"], kv[1]["focusedAt"], kv[1]["joinedAt"]))[0]
        if target != self.target:
            self.target = target
            self._bump()

    def wait(self, seen_version, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen_version or stop_event.is_set(), timeout)

    def view(self, cid):
        with self.cond:
            return {"v": self.version, "enabled": self.enabled,
                    "open": self.pressed and not stop_event.is_set(), "target": cid == self.target}

    def status(self):
        """-> (icon kind, text). kind: 'on' = mic live, 'off' = armed, 'bad' = not working."""
        with self.cond:
            self._retarget()
            clients = list(self.clients.values())
            target = self.clients.get(self.target)
            mic_on = any(c["micOn"] for c in clients)
            if not self.enabled:
                problem = "PTT вимкнено"
            elif time.monotonic() - self.legacy_at < LEGACY_WARN_S:
                problem = "стара версія скрипта в Tampermonkey — видали її"
            elif not clients:
                problem = "Google Meet не підключено"
            elif target is None:
                problem = "не в дзвінку / кнопку мікрофона не знайдено"
            elif target["error"]:
                problem = CLIENT_ERRORS.get(target["error"], target["error"])
            else:
                problem = ""
        kind = "on" if mic_on else ("bad" if problem else "off")
        text = ("MIC ON" if mic_on else "MIC OFF") + (f" — {problem}" if problem else "")
        return kind, text

    def describe(self):
        _, text = self.status()
        cfg = settings
        with self.cond:
            target = self.clients.get(self.target)
            keys = ", ".join(key_label(k) for k in cfg["ptt_keys"])
            return "\n".join([
                f"Стан: {text}",
                f"Клавіші: {keys} — {'натиснуто' if self.pressed else 'відпущено'}",
                f"Вкладок Meet: {len(self.clients)}" + (f", ціль: {target['url']}" if target else ""),
            ])


hub = Hub(settings["ptt_enabled"])


def apply_settings(new):
    global settings
    with settings_lock:
        cfg = normalize_settings(new)
        settings = cfg
        save_settings(cfg)
    hub.set_enabled(cfg["ptt_enabled"])
    if tray_icon is not None:
        try:
            tray_icon.update_menu()
        except Exception:
            pass


def request_quit(reason):
    if stop_event.is_set():
        return
    print(f"[APP] exit: {reason}", flush=True)
    stop_event.set()
    hub.release_all()  # wakes every pending long-poll with open=false


# ---------------------------------------------------------------- PTT key

def key_poll_loop():
    print("[PTT] reading keys with GetAsyncKeyState (no keyboard hook)", flush=True)
    while not stop_event.is_set():
        cfg = settings
        down, pressed = [], False
        try:
            if cfg["ptt_enabled"]:
                down = [k for k in cfg["ptt_keys"] if user32.GetAsyncKeyState(PTT_KEYS[k][0]) & 0x8000]
                if cfg["ptt_multi_mode"] == "all" and len(cfg["ptt_keys"]) > 1:
                    pressed = len(down) == len(cfg["ptt_keys"])
                else:
                    pressed = bool(down)
        except Exception as e:
            print(f"[PTT] key read failed: {e}", flush=True)
            down, pressed = [], False
        hub.set_pressed(pressed, down)
        stop_event.wait(cfg["ptt_poll_ms"] / 1000.0)


# ---------------------------------------------------------------- HTTP

class Server(ThreadingHTTPServer):
    # http.server turns SO_REUSEADDR on by default. On Windows that lets a second copy bind
    # the same port and silently steal requests. A plain bind fails while another listener
    # exists and still rebinds past TIME_WAIT after a restart.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        # Skip HTTPServer's socket.getfqdn(): it can hang for seconds when DNS is broken.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    server_version = "GoogleMeetPTT/5"
    timeout = 20  # drop half-open connections instead of leaking threads

    def log_message(self, *_args):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the tab went away mid-request

    def _route(self):
        path = self.path.split("?", 1)[0]
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self._reply(403, {"error": "bad host"})
            return None
        if path in LEGACY_PATHS:
            hub.mark_legacy()
            self._reply(410, {"error": "outdated userscript"})
            return None
        if path == "/health":
            return path
        proto = self.headers.get(AUTH_HEADER)
        if proto is None:
            self._reply(403, {"error": "forbidden"})
            return None
        if proto != str(PROTO):
            hub.mark_legacy()
            self._reply(409, {"error": f"protocol {proto} != {PROTO}"})
            return None
        return path

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= 65536:
            raise ValueError("body size")
        raw = self.rfile.read(n) if n else b""
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data

    def do_GET(self):
        path = self._route()
        if path == "/health":
            self._reply(200, {"ok": True, "app": APP_NAME, "proto": PROTO, "pid": os.getpid()})
        elif path:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self._route()
        if not path:
            return
        try:
            data = self._body()
        except Exception:
            self._reply(400, {"error": "bad json"})
            return
        cid = str(data.get("id") or "")[:64]

        if path == "/poll":
            if not cid:
                self._reply(400, {"error": "no id"})
                return
            hub.update_client(cid, data)
            hub.wait(data.get("v"), POLL_HOLD_S)
            self._reply(200, hub.view(cid))
        elif path == "/status":
            if cid:
                if data.get("closing"):
                    hub.drop_client(cid)
                else:
                    hub.update_client(cid, data)
            self._reply(200, {"ok": True})
        elif path == "/quit":
            self._reply(200, {"ok": True})
            request_quit("replaced by a new instance")
        else:
            self._reply(404, {"error": "not found"})


def serve_loop(httpd):
    print(f"[HTTP] listening on http://{HOST}:{PORT}", flush=True)
    while not stop_event.is_set():
        try:
            httpd.serve_forever(poll_interval=0.25)
        except Exception as e:
            print(f"[HTTP] server loop error: {e}", flush=True)
            stop_event.wait(0.5)


# ---------------------------------------------------------------- single instance (newest wins)

def local_http(method, path, timeout):
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}",
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={AUTH_HEADER: str(PROTO), "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send localhost via a system proxy
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    return data if isinstance(data, dict) else {}


def listening_pids(port):
    class Row(ctypes.Structure):  # MIB_TCPROW_OWNER_PID
        _fields_ = [("state", wt.DWORD), ("local_addr", wt.DWORD), ("local_port", wt.DWORD),
                    ("remote_addr", wt.DWORD), ("remote_port", wt.DWORD), ("pid", wt.DWORD)]

    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    size = wt.DWORD(16384)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value)
        rc = get_table(buf, ctypes.byref(size), False, socket.AF_INET, 3, 0)  # 3 = TCP_TABLE_OWNER_PID_LISTENER
        if rc == 0:
            count = wt.DWORD.from_buffer(buf).value
            rows = (Row * count).from_buffer(buf, ctypes.sizeof(wt.DWORD))
            return sorted({r.pid for r in rows if socket.ntohs(r.local_port & 0xFFFF) == port})
        if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
            break
    return []


def process_image(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        kernel32.CloseHandle(h)


def try_bind():
    try:
        return Server((HOST, PORT), Handler)
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or e.errno == errno.EADDRINUSE:
            return None
        raise


def bind_retry(seconds):
    end = time.monotonic() + seconds
    while True:
        srv = try_bind()
        if srv or time.monotonic() >= end:
            return srv
        time.sleep(0.15)


def acquire_server():
    """Take the port. If a copy is already running, ask it to quit; kill it if it's hung or outdated."""
    srv = try_bind()
    if srv:
        return srv

    try:
        info = local_http("GET", "/health", 1.0)
    except Exception:
        info = {}
    ours = info.get("app") == APP_NAME

    if ours and "proto" in info:
        print("[INIT] another instance is running — asking it to quit", flush=True)
        try:
            local_http("POST", "/quit", 1.0)
        except Exception:
            pass
        srv = bind_retry(3.0)
        if srv:
            return srv

    me = os.path.basename(sys.executable).lower()
    owners = [p for p in listening_pids(PORT) if p != os.getpid()]
    for pid in owners:
        image = process_image(pid)
        if ours or os.path.basename(image).lower() == me:
            print(f"[INIT] terminating stale instance pid={pid} {image}", flush=True)
            try:
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
            except OSError as e:
                print(f"[INIT] kill failed: {e}", flush=True)

    srv = bind_retry(3.0)
    if srv:
        return srv
    who = ", ".join(f"{p} ({os.path.basename(process_image(p)) or '?'})" for p in owners) or "невідомо"
    message_box(f"Порт {PORT} зайнятий: PID {who}.\nЗакрий цю програму і запусти {APP_NAME} знову.")
    return None


# ---------------------------------------------------------------- tray

def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_icon(name, rgb):
    path = resource_path(name)
    if os.path.exists(path):
        with Image.open(path) as img:
            return img.convert("RGBA")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=rgb + (255,))
    return img


def build_icons():
    off = load_icon(ICON_FILE, (40, 160, 70))
    on = load_icon(MIC_ON_ICON_FILE, (200, 40, 30))
    bad = load_icon(FADED_ICON_FILE, (128, 128, 128))
    return {"off": off, "on": on, "bad": bad}


def tray_updater(icon):
    last = None
    while not stop_event.is_set():
        try:
            kind, text = hub.status()
            if (kind, text) != last:
                icon.icon = ICONS[kind]
                icon.title = f"{APP_NAME}: {text}"[:127]
                last = (kind, text)
        except Exception as e:
            print(f"[TRAY] update failed: {e}", flush=True)
        stop_event.wait(0.15)


def tray_setup(icon):
    icon.visible = True
    threading.Thread(target=tray_updater, args=(icon,), daemon=True, name="tray-updater").start()


def build_tray():
    def on_settings(_icon=None, _item=None):
        ui_queue.put(open_settings)

    def on_toggle(_icon=None, _item=None):
        apply_settings({**settings, "ptt_enabled": not settings["ptt_enabled"]})

    def on_quit(_icon=None, _item=None):
        request_quit("tray")

    menu = pystray.Menu(
        pystray.MenuItem("Налаштування...", on_settings, default=True),
        pystray.MenuItem("PTT увімкнено", on_toggle, checked=lambda _item: settings["ptt_enabled"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Вийти", on_quit),
    )
    return pystray.Icon(APP_ID, ICONS["bad"], APP_NAME, menu)


# ---------------------------------------------------------------- settings window

BG, FIELD, FG = "#111111", "#181818", "#eeeeee"
settings_win = None


def setup_dark_ttk_style():
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", fieldbackground=FIELD, background="#222222", foreground=FG,
                    arrowcolor=FG, bordercolor="#444444", lightcolor="#444444", darkcolor=BG)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
              background=[("readonly", "#222222")])


def dark_label(parent, text, font=None):
    return tk.Label(parent, text=text, fg=FG, bg=BG, font=font)


def dark_button(parent, text, command):
    return tk.Button(parent, text=text, command=command, fg=FG, bg="#222222", activeforeground="#ffffff",
                     activebackground="#333333", relief="flat", bd=1, highlightthickness=1,
                     highlightbackground="#444444", padx=10, pady=4)


def dark_check(parent, text, variable):
    return tk.Checkbutton(parent, text=text, variable=variable, fg=FG, bg=BG, selectcolor="#222222",
                          activebackground=BG, activeforeground="#ffffff", highlightthickness=0)


def open_settings():
    global settings_win
    if settings_win is not None and settings_win.winfo_exists():
        settings_win.deiconify()
        settings_win.lift()
        settings_win.focus_force()
        return

    win = settings_win = tk.Toplevel(tk_root)
    win.title(f"Налаштування {APP_NAME}")
    win.configure(bg=BG)
    win.minsize(480, 380)
    win.attributes("-topmost", True)

    frm = tk.Frame(win, bg=BG)
    frm.pack(fill="both", expand=True, padx=14, pady=12)
    frm.columnconfigure(0, weight=1)
    frm.rowconfigure(1, weight=1)

    dark_label(frm, "PTT-кнопки", ("Segoe UI", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

    pending = list(settings["ptt_keys"])
    keys_list = tk.Listbox(frm, selectmode="extended", fg=FG, bg=FIELD, selectforeground="#ffffff",
                           selectbackground="#333333", highlightbackground="#444444", relief="flat",
                           height=7, exportselection=False)
    keys_list.grid(row=1, column=0, sticky="nsew")

    def refresh_list():
        keys_list.delete(0, "end")
        for name in pending:
            keys_list.insert("end", key_label(name))

    side = tk.Frame(frm, bg=BG)
    side.grid(row=1, column=1, sticky="n", padx=(12, 0))
    label_to_name = {key_label(n): n for n in PTT_KEYS}
    add_var = tk.StringVar(value=next(iter(label_to_name)))
    ttk.Combobox(side, textvariable=add_var, values=list(label_to_name), state="readonly", width=16).pack(fill="x")

    def add_key():
        name = label_to_name.get(add_var.get())
        if name and name not in pending:
            pending.append(name)
            refresh_list()

    def remove_keys():
        for idx in reversed(keys_list.curselection()):
            pending.pop(idx)
        if not pending:
            pending.append("Insert")
        refresh_list()

    dark_button(side, "Додати", add_key).pack(fill="x", pady=(6, 0))
    dark_button(side, "Видалити вибрані", remove_keys).pack(fill="x", pady=(6, 0))
    refresh_list()

    enabled_var = tk.BooleanVar(value=settings["ptt_enabled"])
    all_var = tk.BooleanVar(value=settings["ptt_multi_mode"] == "all")
    poll_var = tk.StringVar(value=str(settings["ptt_poll_ms"]))

    opts = tk.Frame(frm, bg=BG)
    opts.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
    dark_check(opts, "PTT увімкнено", enabled_var).pack(anchor="w")
    dark_check(opts, "Комбінація: тримати всі вибрані кнопки одночасно", all_var).pack(anchor="w")
    poll_row = tk.Frame(opts, bg=BG)
    poll_row.pack(anchor="w", pady=(6, 0))
    dark_label(poll_row, "Опитування клавіш, мс:").pack(side="left")
    tk.Entry(poll_row, textvariable=poll_var, width=6, fg=FG, bg=FIELD, insertbackground="#ffffff",
             relief="flat").pack(side="left", padx=(8, 0))

    status_var = tk.StringVar()
    tk.Label(frm, textvariable=status_var, fg="#bdbdbd", bg=BG, justify="left", anchor="w",
             wraplength=440).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def apply(close):
        try:
            poll_ms = int(poll_var.get().strip())
        except ValueError:
            poll_ms = settings["ptt_poll_ms"]
        apply_settings({
            "ptt_enabled": enabled_var.get(),
            "ptt_keys": pending,
            "ptt_multi_mode": "all" if all_var.get() else "any",
            "ptt_poll_ms": poll_ms,
        })
        poll_var.set(str(settings["ptt_poll_ms"]))
        if close:
            win.destroy()

    buttons = tk.Frame(frm, bg=BG)
    buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    dark_button(buttons, "Застосувати", lambda: apply(False)).pack(side="left")
    dark_button(buttons, "OK", lambda: apply(True)).pack(side="left", padx=6)
    dark_button(buttons, "Закрити", win.destroy).pack(side="right")

    def refresh_status():
        if win.winfo_exists():
            status_var.set(hub.describe())
            win.after(300, refresh_status)

    refresh_status()


def ui_pump():
    while True:
        try:
            fn = ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception as e:
            print(f"[UI] {e}", flush=True)
    if stop_event.is_set():
        tk_root.destroy()
        return
    tk_root.after(100, ui_pump)


# ---------------------------------------------------------------- main

def main():
    global tk_root, tray_icon, ICONS
    if pystray is None:
        message_box("Бракує пакетів. Встанови: pip install pystray pillow")
        return 1
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    try:
        httpd = acquire_server()
    except OSError as e:
        hint = ""
        if getattr(e, "winerror", None) == 10013:
            hint = ("\n\nWindows могла зарезервувати цей порт. Перевір:\n"
                    "netsh int ipv4 show excludedportrange protocol=tcp")
        message_box(f"Не вдалося відкрити порт {PORT}: {e}{hint}")
        return 1
    if httpd is None:
        return 1

    threading.Thread(target=serve_loop, args=(httpd,), daemon=True, name="http").start()
    threading.Thread(target=key_poll_loop, daemon=True, name="keys").start()

    ICONS = build_icons()
    tk_root = tk.Tk()
    tk_root.withdraw()
    setup_dark_ttk_style()

    tray_icon = build_tray()
    threading.Thread(target=tray_icon.run, kwargs={"setup": tray_setup}, daemon=True, name="tray").start()

    tk_root.after(100, ui_pump)
    try:
        tk_root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        request_quit("shutdown")
        time.sleep(0.2)  # let pending long-polls deliver open=false
        stopper = threading.Thread(target=httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(2.0)
        httpd.server_close()
        try:
            tray_icon.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
