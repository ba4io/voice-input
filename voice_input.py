# -*- coding: utf-8 -*-
"""本地语音输入。

  - 键盘右 Ctrl：按住说话，松开出字（对讲式）
  - 鼠标侧键  ：按一下开始，再按一下结束（开关式）；该事件被拦截，不会触发后退/前进

识别结果自动粘贴到光标处。
"""
import os
import re
import time
import ctypes
import threading
import queue
from ctypes import wintypes
from datetime import datetime

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import sys
import json

import numpy as np
import sounddevice as sd
import keyboard
import winsound

# ---------------- 配置 ----------------
# 下面的值是默认值。想改就改同目录的 config.json，首次运行会自动生成。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
    "hotkey": "right ctrl",         # 键盘热键
    "model": "auto",                # auto = 有显卡用 large-v3，没显卡退回 small
    "language": "zh",               # 语言代码；填 "" 则自动判断
    "device_hint": "",              # 麦克风名字包含此串就绑定；留空 = 系统默认输入设备
    "mute_background": True,        # 录音时静音其它音频，防回声
    "clean_fillers": True,          # 去掉"嗯、那个、就是说"
    "suppress_side_buttons": True,  # 拦截鼠标侧键，防止触发前进/后退
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    except Exception as e:
        print("config.json 读取失败，已用默认值: %s" % e)
    return cfg


CFG = load_config()

HOTKEY = CFG["hotkey"]
SUPPRESS_SIDE_BUTTONS = CFG["suppress_side_buttons"]
MODEL_SIZE = CFG["model"]
LANGUAGE = CFG["language"] or None
DEVICE_HINT = CFG["device_hint"]
MUTE_BACKGROUND = CFG["mute_background"]
CLEAN_FILLERS = CFG["clean_fillers"]

SAMPLE_RATE = 16000
MIN_SECONDS = 0.4
MAX_SECONDS = 120
DEBOUNCE = 0.30
KB_TAP = 0.50              # 键盘按住短于此视为"轻点"，进入连续录音
SILENT_RATIO = 0.10
SILENT_DB = -45.0
LOG = os.path.join(BASE_DIR, "voice_input.log")
HOTWORDS = os.path.join(BASE_DIR, "hotwords.txt")


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


ui_state = {"active": False, "level": 0.0, "status": ""}
_ui_hist = []


def ui_thread():
    """半透明浮窗：录音时显示电平条，转写时显示状态。失败则静默跳过。"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.88)
        except Exception:
            pass
        W, H = 260, 64
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (W, H, (sw - W) // 2, sh - H - 140))
        cv = tk.Canvas(root, width=W, height=H, bg="#141414", highlightthickness=0)
        cv.pack()
        root.withdraw()

        N = 28
        barw = (W - 24) / float(N)
        rects = []
        for i in range(N):
            x = 12 + i * barw
            rects.append(cv.create_rectangle(x, H / 2, x + barw * 0.6, H / 2,
                                             fill="#3ddc84", outline=""))
        txt = cv.create_text(12, 10, anchor="w", fill="#cccccc",
                             text="", font=("Microsoft YaHei UI", 9))

        def tick():
            try:
                if ui_state["active"]:
                    if not root.winfo_ismapped():
                        root.deiconify()
                    lvl = ui_state["level"]
                    _ui_hist.append(lvl)
                    del _ui_hist[:-N]
                    while len(_ui_hist) < N:
                        _ui_hist.insert(0, 0.0)
                    for i, v in enumerate(_ui_hist):
                        h = max(2.0, min(H - 26, (v ** 0.5) * 260))
                        x = 12 + i * barw
                        cv.coords(rects[i], x, (H - 6) - h, x + barw * 0.6, H - 6)
                        cv.itemconfig(rects[i], fill="#3ddc84" if v < 0.35 else "#facc15")
                    cv.itemconfig(txt, text=ui_state["status"] or "● 录音中")
                else:
                    if root.winfo_ismapped():
                        root.withdraw()
                    _ui_hist.clear()
            except Exception:
                pass
            root.after(50, tick)

        root.after(50, tick)
        root.mainloop()
    except Exception as e:
        log("浮窗不可用: %s" % str(e)[:100])


def _make_tone(freq, ms, volume=0.5):
    import io, wave, math, struct
    sr = 22050
    n = int(sr * ms / 1000)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    frames = b"".join(
        struct.pack("<h", int(volume * 32767 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(n))
    w.writeframes(frames); w.close()
    return buf.getvalue()


_TONE_HIGH = _make_tone(880, 90)
_TONE_LOW = _make_tone(520, 130)


def beep(freq, ms=80):
    """走普通音频通道播放提示音（主板蜂鸣器在很多机器上不响）。"""
    try:
        data = _TONE_HIGH if freq >= 700 else _TONE_LOW
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        try:
            winsound.Beep(freq, ms)
        except Exception:
            pass


def _model_candidates():
    if MODEL_SIZE and MODEL_SIZE != "auto":
        return [(MODEL_SIZE, "cuda", "float16"), (MODEL_SIZE, "cpu", "int8")]
    # auto：有显卡跑 large-v3，没有就退回 small，免得在 CPU 上等到怀疑人生
    return [("large-v3", "cuda", "float16"), ("small", "cpu", "int8")]


def load_model():
    from faster_whisper import WhisperModel
    last = None
    for size, device, ctype in _model_candidates():
        try:
            log("加载模型 %s on %s (%s) ..." % (size, device, ctype))
            print("加载模型 %s（%s/%s），首次运行需要下载，请稍等…" % (size, device, ctype))
            m = WhisperModel(size, device=device, compute_type=ctype)
            log("模型就绪: %s / %s / %s" % (size, device, ctype))
            return m
        except Exception as e:
            last = e
            log("  %s/%s 失败: %s" % (size, device, str(e)[:200]))
    raise RuntimeError("模型加载失败: %s" % last)


# ---------------- 鼠标侧键（底层钩子，可拦截） ----------------
WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
user32 = ctypes.WinDLL("user32", use_last_error=True)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)
# 必须声明签名，否则 64 位下参数会被截断，CallNextHookEx 返回 0
# 而返回 0 的含义是"吞掉事件" —— 这会把整个鼠标锁死
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
_hook_ref = {}   # 持有回调引用，防止被回收


def install_mouse_hook(on_down, on_up, suppress=True):
    """在独立线程里安装 WH_MOUSE_LL，只处理侧键（XBUTTON1/2）。"""
    HOOKPROC_T = HOOKPROC

    def proc(n_code, w_param, l_param):
        try:
            if n_code == 0:
                if w_param == WM_XBUTTONDOWN and suppress:
                    threading.Thread(target=on_down, daemon=True).start()
                    return 1          # 吞掉侧键按下
                if w_param == WM_XBUTTONUP and suppress:
                    threading.Thread(target=on_up, daemon=True).start()
                    return 1          # 吞掉侧键松开
                if w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    if w_param == WM_XBUTTONDOWN:
                        threading.Thread(target=on_down, daemon=True).start()
                    else:
                        threading.Thread(target=on_up, daemon=True).start()
        except Exception:
            pass
        # 其它一切事件都必须原样放行
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    def run():
        cb = HOOKPROC_T(proc)
        _hook_ref["cb"] = cb
        h = user32.SetWindowsHookExW(WH_MOUSE_LL, cb, None, 0)
        if not h:
            log("鼠标钩子安装失败，侧键不可用")
            return
        log("鼠标侧键已接管（拦截模式: %s）" % suppress)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=run, daemon=True).start()


# ---------------- 录音 ----------------
_mute_state = {"vol": None}


def list_devices():
    """列出所有输入设备，用来填 config.json 里的 device_hint。"""
    print("输入设备：")
    found = False
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            found = True
            print("  [%d] %s  (%d 声道, %.0f Hz)" % (
                i, d.get("name", "?"), d["max_input_channels"],
                d.get("default_samplerate", 0)))
    if not found:
        print("  （没找到任何输入设备，检查麦克风是否插好、系统是否授权）")


def mute_background():
    if not MUTE_BACKGROUND:
        return
    try:
        import comtypes
        comtypes.CoInitialize()      # 每个线程都要初始化 COM
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities
        vol = AudioUtilities.GetSpeakers().EndpointVolume
        if not vol.GetMute():
            vol.SetMute(1, None)
            _mute_state["vol"] = vol
    except Exception as e:
        log("静音背景音频失败: %s" % str(e)[:120])


def unmute_background():
    vol = _mute_state.get("vol")
    if vol is None:
        return
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        vol.SetMute(0, None)
    except Exception:
        pass
    _mute_state["vol"] = None


FILLER_PATTERNS = [
    r"嗯+[，,]?", r"呃+[，,]?", r"唔+[，,]?",
    r"那个[，,]", r"就是说[，,]", r"然后呢[，,]",
    r"这个这个[，,]?", r"对对对[，,]",
]


def clean_fillers(t):
    if not CLEAN_FILLERS or not t:
        return t
    for pat in FILLER_PATTERNS:
        t = re.sub(pat, "", t)
    return t.lstrip("，, ").strip()


class Recorder:
    def __init__(self):
        self.q = queue.Queue()
        self.stream = None
        self.frames = []
        self.start_at = 0
        self.device = None   # 每次录音前重新解析
        self.samplerate = SAMPLE_RATE

    def _cb(self, indata, frames, time_info, status):
        self.q.put(indata.copy())
        try:
            ui_state["level"] = float(np.sqrt(np.mean(indata ** 2)))
        except Exception:
            pass

    def _pick(self):
        """按名字找设备；同名多个时优先选 2 声道的（多轨口容易取到空声道）。
        device_hint 留空时返回 (None, 1)，也就是用系统默认输入设备。"""
        hint = (DEVICE_HINT or "").lower()
        if not hint:
            return None, 1
        try:
            cands = []
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and hint in d.get("name", "").lower():
                    cands.append((i, d.get("max_input_channels", 0), d.get("name", "")))
            if not cands:
                log("没有名字含「%s」的输入设备，改用系统默认" % DEVICE_HINT)
                return None, 1
            cands.sort(key=lambda x: (0 if x[1] == 1 else 1 if x[1] == 2 else 2, x[0]))
            idx, ch, name = cands[0]
            log("使用输入设备: [%d] %s (%d声道)" % (idx, name, ch))
            return idx, 2 if ch >= 2 else 1
        except Exception as e:
            log("设备解析失败: %s" % e)
            return None, 1

    def start(self):
        self.frames = []
        self.start_at = time.time()
        self.device, nch = self._pick()
        last = None
        chans = [nch, 2, 1] if nch >= 2 else [1]
        rates = [SAMPLE_RATE, 44100, 48000]
        preferred = None
        try:
            preferred = int(sd.query_devices(self.device)["default_samplerate"])
        except Exception:
            pass
        if preferred and preferred not in rates:
            rates.append(preferred)
        for ch in chans:
            for sr in rates:
                try:
                    self.stream = sd.InputStream(
                        samplerate=sr, channels=ch, dtype="float32",
                        device=self.device, callback=self._cb)
                    self.stream.start()
                    # 等第一帧数据到达，避免刚开流就被结束导致 DEVICE_NO_DATA
                    t_wait = time.time() + 0.6
                    while time.time() < t_wait and self.q.empty():
                        time.sleep(0.02)
                    self.channels = ch
                    self.samplerate = sr
                    if sr != SAMPLE_RATE:
                        log("设备采样率 %d Hz，已自动重采样到 %d" % (sr, SAMPLE_RATE))
                    return
                except Exception as e:
                    last = e
                    self.stream = None
        raise RuntimeError("打开输入失败: %s" % last)

    def stop(self):
        if self.stream is None:
            return None
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.stream = None
        while not self.q.empty():
            self.frames.append(self.q.get())
        if not self.frames:
            return None
        data = np.concatenate(self.frames, axis=0)
        if data.ndim == 2 and data.shape[1] > 1:
            rms = [float(np.sqrt(np.mean(data[:, i] ** 2))) for i in range(data.shape[1])]
            k = int(np.argmax(rms))
            mono = data[:, k].astype(np.float32)
        else:
            mono = data.flatten().astype(np.float32)
        return to_16k(mono, getattr(self, "samplerate", SAMPLE_RATE))


def join_segments(segments):
    """Whisper 的分段通常对应一句话，段间补句号。"""
    out = ""
    for seg in segments:
        t = (seg.text or "").strip()
        if not t:
            continue
        if out and out[-1] not in "。！？，；：":
            out += "。"
        if out and out[-1] == "，":
            out = out[:-1] + "。"
        out += t
    return out.strip()


WHISPER_FLAG = os.path.join(BASE_DIR, "whisper.txt")
_whisper = {"mtime": 0.0, "on": False}


def whisper_on():
    """whisper.txt 内容为 1/on/true/开 时启用耳语模式；改文件即时生效。"""
    try:
        m = os.path.getmtime(WHISPER_FLAG)
    except OSError:
        return False
    if m != _whisper["mtime"]:
        try:
            v = open(WHISPER_FLAG, encoding="utf-8").read().strip().lower()
        except Exception:
            v = ""
        _whisper["on"] = v in ("1", "on", "true", "开", "yes")
        _whisper["mtime"] = m
        log("耳语模式: %s" % ("开" if _whisper["on"] else "关"))
    return _whisper["on"]


def foreground_app():
    """当前前台窗口的进程名，用来记录文字粘贴到了哪个程序。"""
    try:
        import psutil
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return "?"
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "?"
        return psutil.Process(pid.value).name()
    except Exception:
        return "?"


def to_16k(x, sr):
    """把任意采样率的单声道音频重采样到 16k。"""
    if sr == 16000 or len(x) == 0:
        return x
    try:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(sr), 16000)
        return resample_poly(x, 16000 // g, int(sr) // g).astype(np.float32)
    except Exception:
        n = int(len(x) * 16000 / sr)
        return np.interp(np.linspace(0, len(x) - 1, n),
                         np.arange(len(x)), x).astype(np.float32)


_hw = {"mtime": 0.0, "pairs": []}


def load_hotwords():
    """文件改动后自动重载，不用重启。"""
    try:
        m = os.path.getmtime(HOTWORDS)
    except OSError:
        return _hw["pairs"]
    if m == _hw["mtime"]:
        return _hw["pairs"]
    pairs = []
    try:
        with open(HOTWORDS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|") if x.strip()]
                if len(parts) < 2:
                    continue
                correct = parts[0]
                for wrong in parts[1:]:
                    if wrong != correct:
                        pairs.append((wrong, correct))
    except Exception as e:
        log("热词表读取失败: %s" % e)
        return _hw["pairs"]
    pairs.sort(key=lambda x: -len(x[0]))      # 长词优先，避免被短词先替换
    _hw["pairs"] = pairs
    _hw["mtime"] = m
    log("热词表已加载 %d 条" % len(pairs))
    return pairs


def apply_hotwords(text):
    if not text:
        return text
    for wrong, correct in load_hotwords():
        if wrong in text:
            text = text.replace(wrong, correct)
    return text


PUNCT_MAP = {",": "，", "?": "？", "!": "！", ";": "；", ":": "：",
             "(": "（", ")": "）"}


def fix_punct(t):
    """Whisper 中文输出习惯用半角逗号且句末无句号，这里补齐。"""
    if not t:
        return t
    for a, b in PUNCT_MAP.items():
        t = t.replace(a, b)
    t = t.strip()
    if t and t[-1] not in "。！？，；：）":
        t += "。"
    return t


# ---------------- 文字注入（不碰剪贴板） ----------------
PUL = ctypes.POINTER(ctypes.c_ulong)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", PUL)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


def _send(units):
    n = len(units)
    if not n:
        return
    arr = (_INPUT * n)()
    for i, u in enumerate(units):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = u
    _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))


def _kb(vk=0, scan=0, flags=0):
    k = _KEYBDINPUT()
    k.wVk = vk
    k.wScan = scan
    k.dwFlags = flags
    k.time = 0
    k.dwExtraInfo = None
    return k


def type_text(t):
    """逐字符以 Unicode 方式注入，完全不使用剪贴板。"""
    if not t:
        return
    batch = []
    for ch in t:
        if ch == "\n":
            batch += [_kb(0x0D, 0, 0), _kb(0x0D, 0, _KEYEVENTF_KEYUP)]
            continue
        if ch == "\t":
            batch += [_kb(0x09, 0, 0), _kb(0x09, 0, _KEYEVENTF_KEYUP)]
            continue
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i:i + 2], "little")
            batch.append(_kb(0, unit, _KEYEVENTF_UNICODE))
            batch.append(_kb(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    for i in range(0, len(batch), 64):
        _send(batch[i:i + 64])
        time.sleep(0.005)


def paste_text(text):
    if not text:
        return
    try:
        type_text(text)
    except Exception as e:
        log("注入失败: %s" % str(e)[:100])


def main():
    log("=" * 50)
    model = load_model()
    rec = Recorder()
    st = {"recording": False, "src": None, "last_evt": 0.0,
          "kb_latched": False, "kb_press_at": 0.0}
    lock = threading.Lock()

    def finish(tag):
        audio = rec.stop()
        unmute_background()
        ui_state["status"] = "识别中…"
        with lock:
            st["recording"] = False
            st["src"] = None
            st["kb_latched"] = False
            st["last_evt"] = time.time()
        try:
            app = foreground_app()
            if audio is None:
                log("[%s] DEVICE_NO_DATA 没有采到音频 | app=%s" % (tag, app))
                beep(400, 120)
                return
            secs = len(audio) / SAMPLE_RATE
            if len(audio) < SAMPLE_RATE * MIN_SECONDS:
                log("[%s] TOO_SHORT %.2fs 低于 %.1fs | app=%s" % (tag, secs, MIN_SECONDS, app))
                beep(400, 120)
                return
            # 耳语模式：降低"有声"门槛，并把音量归一化
            sil_db, sil_ratio = (SILENT_DB - 10.0, 0.05) if whisper_on() else (SILENT_DB, SILENT_RATIO)
            peak0 = float(np.max(np.abs(audio))) or 1e-9
            if peak0 < 1e-6:
                log("[%s] DEVICE_SILENT 设备送的是数字静音 | app=%s" % (tag, app))
                beep(400, 120)
                return
            if whisper_on():
                audio = (audio * (0.25 / peak0)).astype(np.float32)
            frame = 1600
            n = max(1, len(audio) // frame)
            active = 0
            for i in range(n):
                seg = audio[i * frame:(i + 1) * frame]
                r = float(np.sqrt(np.mean(seg * seg))) or 1e-9
                if 20 * np.log10(r) > sil_db:
                    active += 1
            ratio = active / n
            if ratio < sil_ratio:
                log("[%s] NO_SPEECH_DETECTED 有声帧 %.0f%% (门槛 %.0f%%, %s) | app=%s"
                    % (tag, ratio * 100, sil_ratio * 100,
                       "耳语" if whisper_on() else "常规", app))
                beep(400, 120)
                return
            if secs > MAX_SECONDS:
                audio = audio[: SAMPLE_RATE * MAX_SECONDS]
                secs = MAX_SECONDS
            t0 = time.time()
            try:
                segments, _ = model.transcribe(
                    audio, language=LANGUAGE, vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 300},
                    beam_size=5, condition_on_previous_text=False)
                text = join_segments(segments)
                if not text and ratio > 0.30:
                    segments, _ = model.transcribe(
                        audio, language=LANGUAGE, vad_filter=False,
                        beam_size=5, condition_on_previous_text=False)
                    text = join_segments(segments)
            except Exception as e:
                log("[%s] ASR_ERROR %s | app=%s" % (tag, str(e)[:120], app))
                return
            cost = time.time() - t0
            if not text:
                log("[%s] ASR_EMPTY_RESULT %.1fs 音频 %.1fs 处理 | app=%s" % (tag, secs, cost, app))
                return
            text = fix_punct(clean_fillers(apply_hotwords(text)))
            ui_state["status"] = "完成 %d 字" % len(text)
            log("[%s] OK %.1fs 音频 -> %.1fs，%d 字 | app=%s | %s"
                % (tag, secs, cost, len(text), app, text[:60]))
            paste_text(text)
            time.sleep(0.35)          # 让"完成"状态短暂可见
        finally:
            ui_state["active"] = False
    def watchdog():
        while True:
            time.sleep(1)
            with lock:
                if st["recording"] and time.time() - rec.start_at > MAX_SECONDS:
                    threading.Thread(target=finish, args=("超时",), daemon=True).start()

    threading.Thread(target=watchdog, daemon=True).start()

    def start_rec(src):
        mute_background()
        try:
            rec.start()
        except Exception as e:
            log("录音失败: %s" % e)
            return False
        st["recording"] = True
        st["src"] = src
        ui_state["status"] = "● 录音中"
        ui_state["active"] = True
        beep(880, 80)
        log("开始录音 (%s)" % src)
        return True

    # 键盘：轻点=开关式，按住=对讲式
    def kb_down(_e=None):
        # 用事件自带时间戳，避免打开音频设备的耗时污染时长判断
        now = getattr(_e, "time", None) or time.time()
        with lock:
            if st["kb_latched"]:
                st["kb_latched"] = False
                threading.Thread(target=finish, args=("键盘-点按",), daemon=True).start()
                return
            if st["recording"]:
                return
            st["kb_press_at"] = now
            start_rec("键盘")

    def kb_up(_e=None):
        rel = getattr(_e, "time", None) or time.time()
        with lock:
            if not st["recording"] or st["src"] != "键盘" or st["kb_latched"]:
                return
            held = rel - st["kb_press_at"]
            if held < KB_TAP:
                st["kb_latched"] = True
                ui_state["status"] = "● 录音中（再按结束）"
                log("键盘连续录音中，再按一次结束")
                return
        threading.Thread(target=finish, args=("键盘",), daemon=True).start()

    # 鼠标侧键：按一下开，再按一下关
    def ms_down():
        now = time.time()
        with lock:
            if now - st["last_evt"] < DEBOUNCE:
                return
            st["last_evt"] = now
            if st["recording"] and st["src"] == "鼠标":
                threading.Thread(target=finish, args=("鼠标",), daemon=True).start()
                return
            if st["recording"]:
                return
            start_rec("鼠标")

    def ms_up():
        return

    try:
        keyboard.on_press_key(HOTKEY, kb_down, suppress=False)
        keyboard.on_release_key(HOTKEY, kb_up, suppress=False)
        kb_ok = HOTKEY
    except Exception as e:
        log("键盘注册失败: %s" % e)
        kb_ok = None

    install_mouse_hook(ms_down, ms_up, SUPPRESS_SIDE_BUTTONS)
    threading.Thread(target=ui_thread, daemon=True).start()

    log("就绪。键盘 %s 按住说；鼠标侧键 按一下开、再按一下关。"
        % (kb_ok or "无"))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    if "--devices" in sys.argv:
        list_devices()
    else:
        main()
