# -*- coding: utf-8 -*-
"""本地语音输入。

  - 键盘右 Ctrl：按住说话，松开出字（对讲式）
  - 鼠标侧键  ：按一下开始，再按一下结束（开关式）；该事件被拦截，不会触发后退/前进

识别结果自动粘贴到光标处。
"""
import os
import atexit
import re
import time
import ctypes
import socket
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
LOG = os.path.join(BASE_DIR, "voice_input.log")
HOTWORDS = os.path.join(BASE_DIR, "hotwords.txt")


LOG_MAX_BYTES = 5 * 1024 * 1024   # 超过 5MB 就轮转，只保留上一份


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.getsize(LOG) > LOG_MAX_BYTES:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_instance_mutex = None


def acquire_single_instance(name):
    """获取 Local\\ 单实例互斥；重复启动写日志后返回 False。"""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        log("创建单实例互斥失败: %d" % err)
        return False
    if err == 183:      # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(h)
        log("已有实例在运行，本实例退出")
        return False
    _instance_mutex = h
    return True

DEFAULTS = {
    "hotkey": "right ctrl",         # 键盘热键
    "model": "auto",                # auto = 有显卡用 large-v3，没显卡退回 small
    "language": "zh",               # 语言代码；填 "" 则自动判断
    "device_hint": "",              # 麦克风名字包含此串就绑定；留空 = 系统默认输入设备
    "mute_background": True,        # 录音时静音其它音频，防回声
    "clean_fillers": True,          # 去掉"嗯、那个、就是说"
    "suppress_side_buttons": True,  # 拦截鼠标侧键，防止触发前进/后退
    "remote_port": 0,               # 远端入口端口；0 = 关闭
    "remote_token": "",             # 远端鉴权口令
    "remote_bind": "0.0.0.0",       # 远端监听地址；填具体网卡 IP 可收窄暴露面
    "remote_disconnect_grace": 2.0, # 远端断开后等待多久丢弃本次录音
    "output": "inject",             # inject = 逐字注入（默认）；clipboard = 写剪贴板并合成粘贴
    "beeps": True,                  # 是否播放开始/结束提示音
    "clipboard_paste_method": "auto",   # sendinput / message(WM_PASTE) / auto
    "clipboard_sync_delay": 0.30,   # 写剪贴板后等 MWB 同步/目标程序稳定的秒数
    "clipboard_verify_timeout": 0.80,   # 粘贴后等目标控件文本变化的秒数
    "clipboard_rewrite_tries": 3,   # 粘贴前发现剪贴板被改写时最多重写几次
    "paste_mod_wait": 1.50,         # 粘贴前等物理修饰键松开的最长秒数
    "force_release_modifiers": False,   # 超时后是否补发 KEYUP（默认不补，避免和物理按键打架）
    "paste_key_gap": 0.03,          # Ctrl 按下到 V 按下、松开之间的间隔
    "paste_hold": 0.06,             # V 按住时长
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


def _cfg_float(key, default, minimum=0.0):
    raw = CFG.get(key, default)
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        log("config.json 的 %s=%r 无效，已用默认值 %r" % (key, raw, default))
        return default


def _cfg_int(key, default, minimum=1):
    raw = CFG.get(key, default)
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        log("config.json 的 %s=%r 无效，已用默认值 %r" % (key, raw, default))
        return default

HOTKEY = CFG["hotkey"]
SUPPRESS_SIDE_BUTTONS = CFG["suppress_side_buttons"]
MODEL_SIZE = CFG["model"]
LANGUAGE = CFG["language"] or None
DEVICE_HINT = CFG["device_hint"]
MUTE_BACKGROUND = CFG["mute_background"]
CLEAN_FILLERS = CFG["clean_fillers"]
OUTPUT = (CFG.get("output") or "inject").lower()
if OUTPUT not in ("inject", "clipboard"):
    print("config.json 的 output=%r 无效，已改用 inject" % OUTPUT)
    OUTPUT = "inject"
CLIP_SYNC_DELAY = _cfg_float("clipboard_sync_delay", 0.30, 0.0)
PASTE_MOD_WAIT = _cfg_float("paste_mod_wait", 1.50, 0.2)
REMOTE_PORT = _cfg_int("remote_port", 0, 0)
REMOTE_BIND = CFG.get("remote_bind") or "0.0.0.0"   # 只绑这块网卡，减少局域网暴露面
REMOTE_TOKEN = CFG.get("remote_token") or ""
REMOTE_DISCONNECT_GRACE = _cfg_float("remote_disconnect_grace", 2.0, 0.0)
PASTE_METHOD = (CFG.get("clipboard_paste_method") or "auto").lower()
if PASTE_METHOD not in ("auto", "sendinput", "message"):
    print("config.json 的 clipboard_paste_method=%r 无效，已改用 auto" % PASTE_METHOD)
    PASTE_METHOD = "auto"
CLIP_VERIFY_TIMEOUT = _cfg_float("clipboard_verify_timeout", 0.80, 0.1)
CLIP_REWRITE_TRIES = _cfg_int("clipboard_rewrite_tries", 3, 1)
FORCE_RELEASE_MODIFIERS = bool(CFG.get("force_release_modifiers", False))
PASTE_KEY_GAP = _cfg_float("paste_key_gap", 0.03, 0.0)
PASTE_HOLD = _cfg_float("paste_hold", 0.06, 0.0)

SAMPLE_RATE = 16000
MIN_SECONDS = 0.4
MAX_SECONDS = 120
DEBOUNCE = 0.30
KB_TAP = 0.50              # 键盘按住短于此视为"轻点"，进入连续录音
SILENT_RATIO = 0.10
SILENT_DB = -45.0
_SIDED_KEYS = {
    "ctrl": ("left ctrl", "right ctrl"),
    "alt": ("left alt", "right alt"),
    "shift": ("left shift", "right shift"),
}


def exact_scan_codes(name):
    """只保留这个键独占的扫描码。

    keyboard 库解析 'right ctrl' 会连带返回左 Ctrl 的 29，导致 Ctrl+C 的
    Ctrl 也被当成热键。这里减掉同一修饰键另一侧共有的码。
    """
    try:
        codes = set(keyboard.key_to_scan_codes(name))
    except Exception:
        return set()
    for pair in _SIDED_KEYS.values():
        if name in pair:
            other = pair[0] if name == pair[1] else pair[1]
            try:
                codes -= set(keyboard.key_to_scan_codes(other))
            except Exception:
                pass
    if not codes:
        codes = set(keyboard.key_to_scan_codes(name))
    return codes


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

        def place_on_screen():
            """合盖换屏后分辨率变了，浮窗会停在屏幕外；出界就重新摆到当前屏底部居中。"""
            try:
                vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
                vw, vh = root.winfo_vrootwidth(), root.winfo_vrootheight()
                x, y = root.winfo_x(), root.winfo_y()
                if (x + W >= vx and y + H >= vy
                        and x <= vx + vw and y <= vy + vh):
                    return
                sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
                root.geometry("%dx%d+%d+%d" % (W, H, (sw - W) // 2, sh - H - 140))
            except Exception:
                pass

        def tick():
            try:
                if ui_state["active"]:
                    if not root.winfo_ismapped():
                        place_on_screen()
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


_TONE_HIGH = _make_tone(880, 160, volume=0.65)
_TONE_LOW = _make_tone(520, 200, volume=0.65)
_BEEP_FILES = {}


def _init_beep_files(force=False):
    """内存播放不支持 SND_ASYNC，落成 wav 才能异步播、不阻塞主流程。
    优先写程序目录：临时目录会被系统当垃圾清掉，清掉后就退回 winsound.Beep
    的蜂鸣器方波，又刺耳又难听。"""
    import tempfile
    for name, data in (("hi", _TONE_HIGH), ("lo", _TONE_LOW)):
        for base in (BASE_DIR, tempfile.gettempdir()):
            try:
                p = os.path.join(base, "voice_input_%s.wav" % name)
                if force or not os.path.exists(p) or os.path.getsize(p) != len(data):
                    with open(p, "wb") as f:
                        f.write(data)
                _BEEP_FILES[name] = p
                break
            except Exception:
                continue


def beep(freq, ms=80):
    """播放提示音：优先异步播临时 wav，失败退回同步内存播放/蜂鸣器。"""
    if not CFG.get("beeps", True):
        return
    name = "hi" if freq >= 700 else "lo"
    try:
        p = _BEEP_FILES.get(name)
        if p and not os.path.exists(p):
            _init_beep_files(force=True)      # 文件被系统清理过，就地重写
            p = _BEEP_FILES.get(name)
        if p:
            winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.PlaySound(_TONE_HIGH if name == "hi" else _TONE_LOW,
                               winsound.SND_MEMORY)
        log("提示音 %s" % ("高" if name == "hi" else "低"))
    except Exception as e:
        log("提示音播放失败: %s" % str(e)[:80])
        try:                                   # 最后退路才用蜂鸣器方波
            winsound.PlaySound(_TONE_HIGH if name == "hi" else _TONE_LOW,
                               winsound.SND_MEMORY)
        except Exception:
            try:
                winsound.Beep(freq, ms)
            except Exception:
                pass


_init_beep_files()


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
_mute_lock = threading.Lock()
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
    with _mute_lock:
        if _mute_state.get("vol") is not None:
            return
    try:
        import comtypes
        comtypes.CoInitialize()      # 每个线程都要初始化 COM
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities
        vol = AudioUtilities.GetSpeakers().EndpointVolume
        with _mute_lock:
            if _mute_state.get("vol") is None and not vol.GetMute():
                vol.SetMute(1, None)
                _mute_state["vol"] = vol
                try:
                    open(_mute_flag_path(), "w").close()
                except Exception:
                    pass
    except Exception as e:
        log("静音背景音频失败: %s" % str(e)[:120])


def _mute_flag_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "voice_input_muted.flag")


def recover_stale_mute():
    """被强杀时 atexit 不执行，我们设的静音会留下来 —— 靠标记文件精确回收。"""
    p = _mute_flag_path()
    if not os.path.exists(p):
        return
    try:
        import comtypes
        comtypes.CoInitialize()
        from pycaw.pycaw import AudioUtilities
        v = AudioUtilities.GetSpeakers().EndpointVolume
        if v.GetMute():
            v.SetMute(0, None)
            log("上次异常退出残留了静音，已恢复播放设备")
    except Exception as e:
        log("回收残留静音失败: %s" % str(e)[:120])
    try:
        os.remove(p)
    except Exception:
        pass


def output_muted():
    """当前默认播放端点是否静音；读不到返回 None。"""
    try:
        import comtypes
        comtypes.CoInitialize()
        from pycaw.pycaw import AudioUtilities
        return bool(AudioUtilities.GetSpeakers().EndpointVolume.GetMute())
    except Exception:
        return None


def unmute_background():
    # 状态只在真正取消静音成功后才清掉：曾因先清状态、再静默吞异常，
    # 一次失败就把整个设备永久锁在静音上，提示音和系统所有声音一起哑。
    with _mute_lock:
        vol = _mute_state.get("vol")
    if vol is None:
        return
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    for attempt in (1, 2):
        v = vol
        if attempt == 2:
            try:
                from pycaw.pycaw import AudioUtilities
                v = AudioUtilities.GetSpeakers().EndpointVolume
            except Exception as e:
                log("恢复音量失败: 取不到播放设备 %s" % str(e)[:120])
                return
        try:
            v.SetMute(0, None)
            if not v.GetMute():
                with _mute_lock:
                    _mute_state["vol"] = None
                try:
                    os.remove(_mute_flag_path())
                except Exception:
                    pass
                return
            log("恢复音量失败: 设备仍静音 (第 %d 次)" % attempt)
        except Exception as e:
            log("恢复音量失败 (第 %d 次): %s" % (attempt, str(e)[:120]))


atexit.register(unmute_background)


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
        device_hint 留空时返回 (None, 1)，也就是用系统默认输入设备。
        hint 设了却找不到就抛异常，绝不静默改用默认麦——合盖唤醒后 X8 要过一会儿
        才重新枚举，这段时间默认麦录到的是静音，Whisper 会把它编成英文句子。"""
        hint = (DEVICE_HINT or "").lower()
        if not hint:
            return None, 1
        try:
            deadline = time.time() + 1.2
            while True:
                cands = []
                for i, d in enumerate(sd.query_devices()):
                    if d.get("max_input_channels", 0) > 0 and hint in d.get("name", "").lower():
                        cands.append((i, d.get("max_input_channels", 0), d.get("name", "")))
                if cands:
                    cands.sort(key=lambda x: (0 if x[1] == 2 else 1, x[0]))
                    idx, ch, name = cands[0]
                    log("使用输入设备: [%d] %s (%d声道)" % (idx, name, ch))
                    return idx, 2 if ch >= 2 else 1
                if time.time() >= deadline:
                    break
                time.sleep(0.08)
        except Exception as e:
            log("设备解析失败: %s" % e)
            return None, 1
        raise RuntimeError("名字含「%s」的输入设备没出现" % DEVICE_HINT)

    def start(self):
        # 设备列表会随 USB 重新枚举而过期（合盖唤醒后尤其明显），过期时开流会报
        # -9997。_terminate/_initialize 实测约 0ms，干脆每次开录前刷一遍，
        # 省掉"先失败一次再重试"的那 0.7 秒
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        last = None
        for attempt in range(2):
            try:
                return self._start_once()
            except Exception as e:
                last = e
                if attempt == 0:
                    # USB 设备重新枚举后，进程里的设备列表会过期；刷新一次再试
                    log("音频设备打开失败，重新枚举设备后重试: %s" % str(e)[:80])
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
        raise RuntimeError("打开输入失败: %s" % last)

    def _start_once(self):
        self.frames = []
        while not self.q.empty():      # 上一次失败尝试可能留下旧音频
            try:
                self.q.get_nowait()
            except Exception:
                break
        self.start_at = time.time()
        self.device, nch = self._pick()
        last = None
        chans = [nch, 2, 1] if nch >= 2 else [1]
        preferred = None
        try:
            preferred = int(sd.query_devices(self.device)["default_samplerate"])
        except Exception:
            pass
        rates = []
        for r in ([preferred] if preferred else []) + [SAMPLE_RATE, 44100, 48000]:
            if r not in rates:
                rates.append(r)
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


_HALLUCINATION_PATTERNS = (
    "thank you", "thanks for watching", "thank you for watching",
    "subtitles by", "subtitles", "amara.org", "transcribed by",
    "dramatisk musikk", "teksting av", "undertekster", "takk for",
    "sous-titres", "untertitel", "vielen dank",
    "请不吝点赞", "明镜与点点", "字幕由", "字幕提供", "关注我的频道",
    "ご視聴ありがとうございました", "チャンネル登録",
)


def looks_hallucinated(parts, info, secs):
    """判断是不是 Whisper 在静音/底噪上编出来的句子：
    没有语音概率高、语言置信度极低，或命中常见的字幕幻觉短语。"""
    if not parts:
        return False
    avg_ns = sum(float(getattr(s, "no_speech_prob", 0.0) or 0.0)
                 for s in parts) / len(parts)
    prob = float(getattr(info, "language_probability", 1.0) or 1.0)
    text = " ".join((s.text or "") for s in parts).strip().lower()
    if avg_ns > 0.6:
        return True
    if prob < 0.35 and secs < 4.0:
        return True
    if prob < 0.6:
        for p in _HALLUCINATION_PATTERNS:
            if p in text and len(text) <= len(p) + 8:
                return True
    return False


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


LANGUAGE_FLAG = os.path.join(BASE_DIR, "language.txt")
LANG_MODES = ("auto", "zh", "en", "tr")
LANG_LABEL = {"auto": "自动", "zh": "中文", "en": "英文", "tr": "翻译(中→英)"}
_lang = {"mtime": 0.0, "mode": None}


def language_mode():
    """language.txt 决定语言档位：auto / zh / en / tr。改文件即时生效。"""
    try:
        m = os.path.getmtime(LANGUAGE_FLAG)
    except OSError:
        default = "zh" if CFG.get("language") == "zh" else (
            "en" if CFG.get("language") == "en" else "auto")
        return default
    if m != _lang["mtime"]:
        try:
            v = open(LANGUAGE_FLAG, encoding="utf-8").read().strip().lower()
        except Exception:
            v = ""
        if v not in LANG_MODES:
            v = "auto"
        _lang["mode"] = v
        _lang["mtime"] = m
        log("语言档位: %s" % LANG_LABEL[v])
    return _lang["mode"] or "auto"


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
    if not re.search(r"[一-鿿]", t):
        return t.strip()          # 非中文输出不补中文标点
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
    _anonymous_ = ("u",)      # 让 _INPUT(ki=...) 真正写进 u.ki，而不是塞进 __dict__
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1

_injecting = {"n": 0, "last": 0.0}   # n>0 或刚发完都算合成按键，钩子必须放过
_deliver_lock = threading.Lock()


def _begin_inject():
    _injecting["n"] += 1


def _end_inject():
    _injecting["n"] = max(0, _injecting["n"] - 1)
    _injecting["last"] = time.time()


def _send(units):
    n = len(units)
    if not n:
        return 0
    arr = (_INPUT * n)()
    for i, u in enumerate(units):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = u
    _begin_inject()
    try:
        sent = _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    finally:
        _end_inject()
    if sent != n:
        log("SEND_SHORT 按键事件只被接受 %d/%d，错误 %d"
            % (sent, n, ctypes.get_last_error()))
    return sent


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
    sent = 0
    for i in range(0, len(batch), 64):
        sent += _send(batch[i:i + 64])
        time.sleep(0.005)
    if sent < len(batch):
        log("TYPE_SHORT 逐字注入只被接收 %d/%d 个事件" % (sent, len(batch)))


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
_KEYEVENTF_EXTENDEDKEY = 0x0001

_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

_user32.OpenClipboard.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_user32.MapVirtualKeyW.restype = wintypes.UINT

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_PASTE = 0x0302
SMTO_ABORTIFHUNG = 0x0002


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", _RECT)]


_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
_kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

_MODIFIERS = (
    ("左Ctrl", VK_LCONTROL, 0x1D, 0),
    ("右Ctrl", VK_RCONTROL, 0x1D, _KEYEVENTF_EXTENDEDKEY),
    ("左Shift", VK_LSHIFT, 0x2A, 0),
    ("右Shift", VK_RSHIFT, 0x36, 0),
    ("左Alt", VK_LMENU, 0x38, 0),
    ("右Alt", VK_RMENU, 0x38, _KEYEVENTF_EXTENDEDKEY),
    ("左Win", VK_LWIN, 0x5B, _KEYEVENTF_EXTENDEDKEY),
    ("右Win", VK_RWIN, 0x5C, _KEYEVENTF_EXTENDEDKEY),
)


def foreground_window():
    """前台窗口的 hwnd / pid / 进程名 / 标题，只用于日志和诊断。"""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": 0, "pid": 0, "app": "?", "title": ""}
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 256)
        app = "?"
        h = _kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                app = buf.value.split("\\")[-1]
            _kernel32.CloseHandle(h)
        return {"hwnd": int(hwnd), "pid": pid.value, "app": app, "title": title.value[:60]}
    except Exception:
        return {"hwnd": 0, "pid": 0, "app": "?", "title": ""}


def _class_name(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else ""


def focus_window():
    """前台线程真正拿到键盘焦点的控件。

    GetForegroundWindow 只说明窗口在前台，不代表焦点在编辑区：焦点可能在
    地址栏、工具栏、webview 外壳，甚至已经不在了。合成粘贴是不是落在编辑
    框里，要看 hwndFocus。
    """
    fg = foreground_window()
    hwnd = fg["hwnd"]
    if not hwnd:
        return {"hwnd": 0, "class": "", "fg": fg}
    tid = _user32.GetWindowThreadProcessId(hwnd, None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    focus = 0
    if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        focus = int(info.hwndFocus or info.hwndActive or hwnd)
    else:
        focus = int(hwnd)
    return {"hwnd": focus, "class": _class_name(focus), "fg": fg}


def _send_message_timeout(hwnd, msg, wparam, lparam, timeout=300):
    """跨进程取文本/发 WM_PASTE。超时返回 None，避免卡在挂起的窗口上。"""
    result = ctypes.c_size_t(0)
    ok = _user32.SendMessageTimeoutW(
        hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout, ctypes.byref(result))
    if not ok:
        return None
    return int(result.value)


def _is_text_class(cls):
    c = (cls or "").lower()
    return any(hint in c for hint in ("edit", "richedit", "scintilla"))


def read_control_text(hwnd, timeout=400):
    """读标准编辑控件的文本，用于判断粘贴到底有没有生效。

    只有 Edit/RichEdit/Scintilla 这类控件能可靠跨进程 WM_GETTEXT；读不到
    就返回 None，表示"无法确认"，调用方不能据此重试，否则可能粘两遍。
    """
    if not hwnd:
        return None
    length = _send_message_timeout(hwnd, WM_GETTEXTLENGTH, 0, 0, timeout)
    if length is None or length < 0 or length > 4000000:
        return None
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    n = _send_message_timeout(
        hwnd, WM_GETTEXT, length + 1,
        ctypes.cast(buf, ctypes.c_void_p).value, timeout)
    if n is None or n <= 0:
        return None
    return buf.value


def _open_clipboard(retries=12, wait=0.05):
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(wait)
    return False


def get_clipboard_text():
    """读回 CF_UNICODETEXT，用来确认刚写进去的内容还在。"""
    if not _open_clipboard(4, 0.03):
        return None
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    except Exception:
        return None
    finally:
        _user32.CloseClipboard()


def _set_clipboard_text_once(text):
    """写一次剪贴板。内存先准备好再抢剪贴板，SetClipboardData 成功后句柄归系统。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        log("CLIP_ALLOC_FAIL 剪贴板内存分配失败")
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        log("CLIP_LOCK_FAIL 剪贴板内存加锁失败")
        return False
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        _kernel32.GlobalFree(h)
        log("CLIP_BUSY 剪贴板被占用，写入失败")
        return False
    try:
        if not _user32.EmptyClipboard():
            _kernel32.GlobalFree(h)
            log("CLIP_EMPTY_FAIL EmptyClipboard 失败，错误 %d" % ctypes.get_last_error())
            return False
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)
            log("CLIP_SET_FAIL SetClipboardData 失败，错误 %d" % ctypes.get_last_error())
            return False
        return True
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text):
    """写 CF_UNICODETEXT，失败时尽量不让剪贴板变成空的。

    EmptyClipboard 之后 SetClipboardData 仍可能失败（剪贴板被别的进程抢走、
    内存句柄无效等）。这里重试三次；仍然失败就尽量把原内容放回去，至少保证
    用户手动 Ctrl+V 不会粘出空内容。
    """
    old = get_clipboard_text()
    for _ in range(3):
        if _set_clipboard_text_once(text):
            return True
        time.sleep(0.05)
    log("CLIP_WRITE_FAIL 连续 3 次写入失败")
    if old is not None and old != text:
        if _set_clipboard_text_once(old):
            log("CLIP_RESTORED 已恢复原剪贴板内容（%d 字）" % len(old))
        else:
            log("CLIP_RESTORE_FAIL 原剪贴板内容也没能恢复")
    return False


def _send_key(vk, scan=0, flags=0):
    """发一个按键事件。返回 True 表示系统接收了这 1 个事件。"""
    unit = _INPUT()
    unit.type = INPUT_KEYBOARD
    unit.u.ki = _kb(vk, scan, flags)
    _begin_inject()
    try:
        n = _user32.SendInput(1, ctypes.byref(unit), ctypes.sizeof(_INPUT))
    finally:
        _end_inject()
    return n == 1


def _mods_down():
    return [(name, vk, scan, ext) for name, vk, scan, ext in _MODIFIERS
            if _user32.GetAsyncKeyState(vk) & 0x8000]


def wait_modifiers_released(timeout=None, force=None):
    """粘贴前等物理修饰键松开。

    默认不补发 KEYUP：用户物理按着 Shift/Ctrl 时，我们补一个 KEYUP 会让
    Windows 的按键状态和真实键盘脱节，用户后面松开时还会再发一次 up。
    超时就放弃本次合成粘贴（剪贴板里已经有文字，可以手动 Ctrl+V）。
    """
    if timeout is None:
        timeout = PASTE_MOD_WAIT
    if force is None:
        force = FORCE_RELEASE_MODIFIERS
    end = time.time() + max(0.0, timeout)
    while time.time() < end:
        if not _mods_down():
            return True
        time.sleep(0.02)
    stuck = _mods_down()
    if not stuck:
        return True
    names = ",".join(x[0] for x in stuck)
    if not force:
        log("PASTE_MOD_STUCK 修饰键未松开: %s，放弃粘贴（剪贴板保留识别文本）" % names)
        return False
    log("PASTE_MOD_STUCK 修饰键未松开: %s，按配置补发 KEYUP" % names)
    for _name, vk, scan, ext in stuck:
        _send_key(vk, scan, _KEYEVENTF_KEYUP | ext)
    time.sleep(0.05)
    return not _mods_down()


def _send_ctrl_v():
    """分四步发 Ctrl+V，并只松开自己按下过的键。

    分步发送能控制按住时长，更接近真实键盘；关键是 Ctrl 按下失败时绝不能
    补发 Ctrl up，否则会把用户物理按住的 Ctrl 状态打乱。
    """
    sc_ctrl = _user32.MapVirtualKeyW(VK_CONTROL, 0) or 0x1D
    sc_v = _user32.MapVirtualKeyW(VK_V, 0) or 0x2F
    ctrl_down = False       # 当前是否还按着，供 finally 收尾用
    v_down = False
    ctrl_pressed = False    # Ctrl 是否真的按下过；不受收尾逻辑影响
    v_pressed = False       # V 是否真的按下过；不受收尾逻辑影响
    sent = 0
    try:
        if not _send_key(VK_CONTROL, sc_ctrl, 0):
            log("PASTE_CTRL_DOWN_FAIL Ctrl 按下未被接收")
            return False
        ctrl_down = True
        ctrl_pressed = True
        sent += 1
        time.sleep(PASTE_KEY_GAP)
        if not _send_key(VK_V, sc_v, 0):
            log("PASTE_V_DOWN_FAIL V 按下未被接收")
            return False
        v_down = True
        v_pressed = True
        sent += 1
        time.sleep(PASTE_HOLD)
        if _send_key(VK_V, sc_v, _KEYEVENTF_KEYUP):
            v_down = False
            sent += 1
        else:
            log("PASTE_V_UP_FAIL V 松开未被接收")
        time.sleep(PASTE_KEY_GAP)
    finally:
        if v_down and _send_key(VK_V, sc_v, _KEYEVENTF_KEYUP):
            v_down = False
            sent += 1
        if ctrl_down and _send_key(VK_CONTROL, sc_ctrl, _KEYEVENTF_KEYUP):
            ctrl_down = False
            sent += 1
    log("PASTE_SENT Ctrl+V 发送 %d/4 | gap=%.0fms hold=%.0fms | fg=%s"
        % (sent, PASTE_KEY_GAP * 1000, PASTE_HOLD * 1000, foreground_window()))
    # 只要 Ctrl 和 V 的按下都被系统接收，就按"已投递"处理。V-up 丢了也不能
    # 回退逐字注入，否则可能粘一遍再打一遍。
    # 注意用 v_pressed：v_down 会在 V-up 成功后清掉，不能再拿它判断"按过"。
    # Ctrl 同理，用 ctrl_pressed，避免收尾清状态后把成功误判成失败。
    return ctrl_pressed and v_pressed


def _clip_equal(a, b):
    """剪贴板回读比较；只把换行差异归一化，其它字符必须一致。"""
    if a is None or b is None:
        return a is b
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def _paste_message(hwnd):
    """直接给焦点控件发 WM_PASTE，绕过低级键盘钩子（MWB 那一层）。"""
    if not hwnd:
        return False
    result = _send_message_timeout(hwnd, WM_PASTE, 0, 0, 600)
    ok = result is not None
    log("PASTE_MSG WM_PASTE hwnd=%d class=%s -> %s"
        % (hwnd, _class_name(hwnd) or "?", "ok" if ok else "fail"))
    return ok


def _verify_paste(focus_hwnd, before, timeout=None):
    """True=确认文本变了，False=确认没变，None=控件不支持读回，无法确认。"""
    if before is None or not focus_hwnd:
        return None
    end = time.time() + (CLIP_VERIFY_TIMEOUT if timeout is None else timeout)
    while time.time() < end:
        after = read_control_text(focus_hwnd)
        if after is None:
            return None
        if after != before:
            return True
        time.sleep(0.05)
    return False


PASTE_OK = "ok"
PASTE_UNVERIFIED = "unverified"
PASTE_FAILED = "failed"
PASTE_NOT_SENT = "not_sent"
PASTE_BLOCKED = "blocked"


def paste_clipboard(text, sync_delay=None, remote=False):
    """写剪贴板 -> 回读校验 -> 合成粘贴 -> 尽量验证。

    返回值：
      ok          已粘贴，且标准编辑控件文本确认变化
      unverified  按键已发出，但目标控件不支持读回，不能确认
      failed      能确认目标控件文本没变（SendInput 和 WM_PASTE 都没生效）
      not_sent    剪贴板没写成功/按键没发出，可以安全回退逐字注入
      blocked     修饰键没松开或没有前台窗口，主动放弃，不回退
    remote=True 时只发一次 Ctrl+V，验证结果只写日志，不尝试 WM_PASTE
    或逐字注入；剪贴板写入失败/按键失败也不回退。
    先写剪贴板，后面任何一步失败时剪贴板里都保留识别文本。
    """
    delay = CLIP_SYNC_DELAY if sync_delay is None else sync_delay
    if not set_clipboard_text(text):
        log("CLIP_WRITE_FAIL 剪贴板写入失败，未发粘贴键")
        return PASTE_NOT_SENT
    back = get_clipboard_text()
    if _clip_equal(back, text):
        log("CLIP_OK 已写剪贴板 %d 字" % len(text))
    else:
        log("CLIP_VERIFY_MISMATCH 回读不一致，重写一次")
        if set_clipboard_text(text) and _clip_equal(get_clipboard_text(), text):
            log("CLIP_REWRITE_OK 重写后回读一致")
        else:
            log("CLIP_REWRITE_FAIL 剪贴板里可能不是本次文本")

    # 先等物理键松开，再进入 MWB 同步窗口；等太久也不怕，剪贴板已经写好了。
    if not wait_modifiers_released():
        log("PASTE_ABORT_MOD 修饰键未松开，放弃合成粘贴，剪贴板保留识别文本")
        return PASTE_BLOCKED

    if delay > 0:
        time.sleep(delay)

    # MWB 可能在延迟期间同步/重写剪贴板；发键前再确认并重写。
    stable = _clip_equal(get_clipboard_text(), text)
    tries = 0
    while not stable and tries < CLIP_REWRITE_TRIES:
        tries += 1
        log("CLIP_CHANGED 粘贴前剪贴板被改写，第 %d 次重写" % tries)
        if not set_clipboard_text(text):
            break
        time.sleep(0.05)
        stable = _clip_equal(get_clipboard_text(), text)
    if not stable:
        log("CLIP_UNSTABLE 剪贴板内容反复变化，%s"
            % ("远端触发，放弃本次投递" if remote else "放弃合成粘贴，退回逐字注入"))
        return PASTE_NOT_SENT

    # 前台窗口不等于输入焦点：焦点可能落在地址栏/工具栏/外壳上。
    focus = focus_window()
    fg = focus["fg"]
    if not fg["hwnd"]:
        log("PASTE_ABORT_FG 没有前台窗口，放弃合成粘贴，剪贴板保留识别文本")
        return PASTE_BLOCKED
    native = _is_text_class(focus["class"])
    before = read_control_text(focus["hwnd"]) if native else None
    log("PASTE_TARGET fg=%s|%s hwnd=%d focus=%d class=%s verify=%s"
        % (fg["app"], fg["title"], fg["hwnd"], focus["hwnd"],
           focus["class"] or "?", "on" if native else "off"))

    # 读焦点/读文本可能花几百毫秒，期间用户可能切了窗口；发键前再确认一次。
    now_fg = foreground_window()
    if now_fg["hwnd"] != fg["hwnd"]:
        log("PASTE_ABORT_FG_CHANGED 前台窗口已变化（%d -> %d|%s），放弃合成粘贴"
            % (fg["hwnd"], now_fg["hwnd"], now_fg["app"]))
        return PASTE_BLOCKED

    changed = None
    sent = False
    if remote:
        sent = _send_ctrl_v()
        if not sent:
            log("PASTE_SEND_FAIL Ctrl+V 没有完整送入，剪贴板保留识别文本")
            return PASTE_NOT_SENT
        changed = _verify_paste(focus["hwnd"], before)
        if changed is True:
            log("PASTE_OK 目标控件文本已变化")
            return PASTE_OK
        if changed is None:
            log("PASTE_UNVERIFIED 已发 Ctrl+V，目标控件不支持读回，无法确认")
            return PASTE_UNVERIFIED
        log("PASTE_NO_CHANGE Ctrl+V 已发，但目标控件文本没变（远端触发，不回退）")
        return PASTE_FAILED

    if PASTE_METHOD in ("sendinput", "auto"):
        sent = _send_ctrl_v()
        if not sent:
            log("PASTE_SEND_FAIL Ctrl+V 没有完整送入，剪贴板保留识别文本")
        else:
            changed = _verify_paste(focus["hwnd"], before)
            if changed is True:
                log("PASTE_OK 目标控件文本已变化")
                return PASTE_OK
            if changed is None:
                log("PASTE_UNVERIFIED 已发 Ctrl+V，目标控件不支持读回，无法确认")
                return PASTE_UNVERIFIED
            log("PASTE_NO_CHANGE Ctrl+V 已发，但目标控件文本没变")

    if PASTE_METHOD == "message" or (PASTE_METHOD == "auto" and changed is False):
        if _paste_message(focus["hwnd"]):
            changed = _verify_paste(focus["hwnd"], before)
            if changed is True:
                log("PASTE_OK WM_PASTE 后目标控件文本已变化")
                return PASTE_OK
            if changed is None:
                log("PASTE_UNVERIFIED WM_PASTE 已发，目标控件不支持读回，无法确认")
                return PASTE_UNVERIFIED
            log("PASTE_NO_CHANGE WM_PASTE 后目标控件文本仍没变")
        else:
            log("PASTE_MSG_FAIL WM_PASTE 发送失败")

    if changed is False:
        return PASTE_FAILED
    if sent:
        return PASTE_UNVERIFIED
    return PASTE_NOT_SENT


def paste_text(text, remote=False):
    if not text:
        return
    with _deliver_lock:
        if remote:
            try:
                paste_clipboard(text, remote=True)
            except Exception as e:
                log("远端剪贴板投递异常: %s" % str(e)[:150])
            return
        if OUTPUT != "clipboard":
            try:
                type_text(text)
            except Exception as e:
                log("注入失败: %s" % str(e)[:100])
            return
        try:
            result = paste_clipboard(text)
            if result in (PASTE_OK, PASTE_UNVERIFIED):
                return
            if result == PASTE_BLOCKED:
                log("剪贴板投递被前置检查拦下（%s），不退回逐字注入" % result)
                return
            log("剪贴板投递失败（%s），改用逐字注入" % result)
        except Exception as e:
            log("剪贴板投递异常: %s" % str(e)[:150])
        try:
            type_text(text)
        except Exception as e:
            log("逐字注入也失败: %s" % str(e)[:100])


def main():
    if not acquire_single_instance("Local\\VoiceLinkInput"):
        return
    log("=" * 50)
    recover_stale_mute()
    model = load_model()
    rec = Recorder()
    st = {"recording": False, "src": None, "last_evt": 0.0,
          "kb_latched": False, "kb_press_at": 0.0, "remote": False,
          "finishing": False, "remote_connected": False,
          "remote_conns": 0, "rec_gen": 0}
    lock = threading.RLock()
    rec_lock = threading.Lock()

    def finish(tag):
        with lock:
            if not st["recording"] or st["finishing"]:
                return
            st["finishing"] = True
            remote_rec = st["remote"]
        audio = None
        try:
            with rec_lock:
                audio = rec.stop()
        except Exception as e:
            log("[%s] STOP_ERROR %s" % (tag, str(e)[:120]))
        try:
            unmute_background()
        except Exception:
            pass
        beep(660, 60)          # 说完停下的一声；必须在解除静音之后，否则又被静掉
        ui_state["status"] = "识别中…"
        with lock:
            st["recording"] = False
            st["src"] = None
            st["remote"] = False
            st["kb_latched"] = False
            st["last_evt"] = time.time()
            st["finishing"] = False
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
            mode = language_mode()
            lang = None if mode in ("auto", "tr") else mode
            task = "translate" if mode == "tr" else "transcribe"
            t0 = time.time()
            info = None
            parts = []

            def run(**kw):
                gen, inf = model.transcribe(audio, **kw)
                p = list(gen)
                return join_segments(p), p, inf

            try:
                text, parts, info = run(
                    language=lang, task=task, vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 300},
                    beam_size=5, condition_on_previous_text=False)
                if not text and ratio > 0.30:
                    text, parts, info = run(
                        language=lang, task=task, vad_filter=False,
                        beam_size=5, condition_on_previous_text=False)
                if not text and mode == "tr":
                    # 翻译档没有输出时退回原语言转写，至少让用户拿到内容
                    log("[%s] 翻译无输出，退回原语言转写" % tag)
                    text, parts, info = run(
                        language=None, task="transcribe", vad_filter=False,
                        beam_size=5, condition_on_previous_text=False)
            except Exception as e:
                log("[%s] ASR_ERROR %s | app=%s" % (tag, str(e)[:120], app))
                return
            cost = time.time() - t0
            if not text:
                log("[%s] ASR_EMPTY_RESULT %.1fs 音频 %.1fs 处理 | app=%s" % (tag, secs, cost, app))
                return
            if looks_hallucinated(parts, info, secs):
                log("[%s] ASR_HALLUCINATION 疑似静音编造 %.1fs 置信 %.0f%% | app=%s | %s"
                    % (tag, secs, float(getattr(info, "language_probability", 0) or 0) * 100,
                       app, text[:60]))
                ui_state["status"] = "没听清"
                beep(400, 120)
                return
            text = fix_punct(clean_fillers(apply_hotwords(text)))
            if mode in ("auto", "tr") and info is not None:
                log("[%s] 语音判定: %s %.0f%% | 档位=%s"
                    % (tag, info.language, info.language_probability * 100, LANG_LABEL[mode]))
            ui_state["status"] = "完成 %d 字" % len(text)
            log("[%s] OK %.1fs 音频 -> %.1fs，%d 字 | app=%s | %s"
                % (tag, secs, cost, len(text), app, text[:60]))
            # 远端触发时也走本地剪贴板 + 合成 Ctrl+V：MWB 会把剪贴板和按键
            # 一起送到光标所在的机器，不需要再把文本经 TCP 发回去。
            if remote_rec:
                log("[%s] 远端触发，走剪贴板粘贴" % tag)
            paste_text(text, remote=remote_rec)
            time.sleep(0.35)          # 让"完成"状态短暂可见
        finally:
            ui_state["active"] = False
    def watchdog():
        while True:
            time.sleep(1)
            with lock:
                if (st["recording"] and not st["finishing"]
                        and time.time() - rec.start_at > MAX_SECONDS):
                    threading.Thread(target=finish, args=("超时",), daemon=True).start()
                    continue
                idle = not st["recording"] and not st["finishing"]
            if idle and _mute_state.get("vol") is not None:
                unmute_background()

    threading.Thread(target=watchdog, daemon=True).start()

    def start_rec(src):
        with lock:
            if st["finishing"]:
                log("录音收尾中，忽略本次启动")
                return False
        t_open = time.time()
        try:
            with rec_lock:
                rec.start()
        except Exception as e:
            unmute_background()
            ui_state["status"] = "麦克风没就绪"
            log("录音失败: %s" % e)
            beep(400, 200)
            return False
        with lock:
            st["rec_gen"] += 1
            st["recording"] = True
            st["src"] = src
        ui_state["status"] = "● 录音中"
        unmute_background()    # 上一轮漏掉的静音先还回去，否则这次的起音听不到
        if output_muted():
            log("警告: 系统播放设备处于静音，提示音听不到")
        beep(880, 80)          # 异步播放
        time.sleep(0.20)       # 等音播完再静音，否则被自己掐断
        mute_background()
        ui_state["active"] = True
        # 远端按下到"真正能录"的全过程（开流 + 提示音 + 等音播完 + 静音）都要扣掉，
        # 否则轻点会被这段固定开销顶成"按住"
        cost = (time.time() - st["kb_press_at"]
                if src == "远端" and st.get("kb_press_at") else time.time() - t_open)
        with lock:
            st["open_cost"] = cost
        log("开始录音 (%s) 启动耗时 %.2fs" % (src, cost))
        return True

    def discard_remote_after_grace(gen):
        time.sleep(REMOTE_DISCONNECT_GRACE)
        with lock:
            if st["remote_connected"]:
                return False
            if (not st["recording"] or st["finishing"]
                    or st["rec_gen"] != gen or st["src"] != "远端"
                    or not st["remote"]):
                return False
            st["finishing"] = True
        try:
            with rec_lock:
                rec.stop()
        except Exception as e:
            log("[远端断开] STOP_ERROR %s" % str(e)[:120])
        try:
            unmute_background()
        except Exception:
            pass
        with lock:
            st["recording"] = False
            st["src"] = None
            st["remote"] = False
            st["kb_latched"] = False
            st["last_evt"] = time.time()
            st["finishing"] = False
        ui_state["active"] = False
        ui_state["status"] = "远端断开，已丢弃"
        log("[远端断开] DISCARD_AUDIO 已丢弃本次远端录音")
        beep(400, 120)
        return True

    # 键盘：轻点=开关式，按住=对讲式
    def kb_down(_e=None, remote=False):
        # 用事件自带时间戳，避免打开音频设备的耗时污染时长判断
        now = getattr(_e, "time", None) or time.time()
        with lock:
            if st["kb_latched"]:
                st["kb_latched"] = False
                tag = "远端-点按" if st["remote"] else "键盘-点按"
                threading.Thread(target=finish, args=(tag,), daemon=True).start()
                return
            if st["recording"]:
                return
            st["kb_press_at"] = now
            st["remote"] = remote
            if not start_rec("远端" if remote else "键盘"):
                st["remote"] = False

    def kb_up(_e=None, remote=False):
        rel = getattr(_e, "time", None) or time.time()
        with lock:
            if not st["recording"] or st["src"] not in ("键盘", "远端") or st["kb_latched"]:
                return
            held = rel - st["kb_press_at"]
            # 远端消息是同步处理的：按下那条要先跑完"打开设备"（实测约 1 秒），
            # 松开那条才轮得到，于是轻点被量成 1 秒多。按本次实际打开耗时补偿，
            # 各机型快慢不同也不会误判。
            oc = st.get("open_cost") or 0.0
            tap = KB_TAP + ((oc + 0.20) if remote else 0.0)
            if remote:
                log("远端松开: 量得 %.2fs，扣掉打开耗时 %.2fs 约 %.2fs（轻点门槛 %.2fs）"
                    % (held, oc, held - oc, tap))
            if held < tap:
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
        codes = exact_scan_codes(HOTKEY)

        def _kb_hook(e):
            # 自己合成的事件不能当成热键，否则 clipboard 模式会自我触发录音
            # keyboard 的钩子回调在独立线程里，可能晚于 SendInput 返回；留 200ms 窗口
            if _injecting["n"] or (time.time() - _injecting["last"] < 0.20):
                return
            # 名字按虚拟键码解析（右 Ctrl=0xA3），扫描码只作补充；
            # 只看扫描码会漏掉注入事件，只看名字会漏掉部分键盘的扩展扫描码。
            if e.name != HOTKEY and e.scan_code not in codes:
                return
            if e.event_type == "down":
                kb_down(e)
            elif e.event_type == "up":
                kb_up(e)

        keyboard.hook(_kb_hook, suppress=False)
        kb_ok = "%s [键码 %s]" % (HOTKEY, ",".join(str(c) for c in sorted(codes)))
        log("键盘绑定只认 %s 的键码 %s" % (HOTKEY, sorted(codes)))
    except Exception as e:
        log("键盘注册失败: %s" % e)
        kb_ok = None

    install_mouse_hook(ms_down, ms_up, SUPPRESS_SIDE_BUTTONS)

    # ---- 远端通道：戴尔键盘的右 Ctrl 经 TCP 过来，文本走本机剪贴板转发 ----
    send_lock = threading.Lock()
    remote_io_timeout = 6.0

    def remote_session(conn):
        peer = "%s:%d" % conn.getpeername()[:2]
        log("远端已连接 %s" % peer)
        authed = False
        try:
            conn.settimeout(5.0)
            f = conn.makefile("rwb")
            hello = f.readline()
            if not hello:
                return
            msg = json.loads(hello.decode("utf-8"))
            if msg.get("t") != "hello" or not REMOTE_TOKEN or msg.get("token") != REMOTE_TOKEN:
                log("远端鉴权失败，已断开")
                return

            def send(obj):
                try:
                    with send_lock:
                        f.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                        f.flush()
                    return True
                except Exception as e:
                    log("远端发送失败: %s" % str(e)[:80])
                    return False

            conn.settimeout(remote_io_timeout)
            with lock:
                authed = True
                st["remote_conns"] += 1
                st["remote_connected"] = True
            if not send({"t": "ack"}):
                return
            log("远端鉴权通过 %s" % peer)

            for line in f:
                try:
                    m = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if m.get("t") == "hotkey":
                    if m.get("action") == "down":
                        kb_down(None, remote=True)
                    else:
                        kb_up(None, remote=True)
                elif m.get("t") == "ping":
                    if not send({"t": "pong"}):
                        break
        except socket.timeout:
            log("远端心跳超时，判定离线 %s" % peer)
        except Exception as e:
            log("远端会话异常: %s" % str(e)[:100])
        finally:
            with lock:
                if authed:
                    st["remote_conns"] = max(0, st["remote_conns"] - 1)
                st["remote_connected"] = st["remote_conns"] > 0
                discard_gen = st["rec_gen"] if (
                    st["recording"] and st["src"] == "远端" and st["remote"]
                    and not st["remote_connected"]
                ) else None
            if discard_gen is not None:
                log("远端断开，%.1fs 后丢弃本次远端录音" % REMOTE_DISCONNECT_GRACE)
                threading.Thread(target=discard_remote_after_grace,
                                 args=(discard_gen,), daemon=True).start()
            try:
                conn.close()
            except Exception:
                pass
            log("远端断开 %s" % peer)

    def remote_server():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((REMOTE_BIND, REMOTE_PORT))
            srv.listen(4)
        except Exception as e:
            log("远端通道监听失败（端口 %d）: %s" % (REMOTE_PORT, e))
            return
        log("远端通道监听 端口 %d" % REMOTE_PORT)
        while True:
            try:
                conn, _ = srv.accept()
            except Exception as e:
                log("远端 accept 失败: %s" % str(e)[:80])
                time.sleep(1)
                continue
            threading.Thread(target=remote_session, args=(conn,), daemon=True).start()

    if REMOTE_PORT:
        threading.Thread(target=remote_server, daemon=True).start()
    threading.Thread(target=ui_thread, daemon=True).start()

    log("投递方式: %s" % ("剪贴板 + 合成 Ctrl+V" if OUTPUT == "clipboard" else "逐字注入"))
    log("就绪。键盘 %s 按住说；鼠标侧键 按一下开、再按一下关。"
        % (kb_ok or "无"))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("退出")
    finally:
        unmute_background()


def paste_test(argv):
    """不加载模型，直接验投递链路。先把光标点进目标输入框再运行：

        python voice_input.py --paste-test "测试文字"
        python voice_input.py --paste-test "测试文字" --clipboard
        python voice_input.py --paste-test "测试文字" --inject

    --clipboard / --inject 只是本次覆盖 config.json 的 output，不改文件。
    """
    i = argv.index("--paste-test")
    text = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") \
        else "剪贴板投递测试 ABC 123"
    method = "clipboard" if (OUTPUT == "clipboard" or "--clipboard" in argv) else "inject"
    if "--inject" in argv:
        method = "inject"
    probe = _INPUT(type=1, ki=_kb(VK_CONTROL, 0x1D, 0))
    if probe.u.ki.wVk != VK_CONTROL or probe.u.ki.wScan != 0x1D:
        print("!! INPUT 结构体填充失败：SendInput 只会发出全零事件")
        return 1
    print("投递方式: %s" % method)
    print("3 秒后投递，请先把光标放到目标窗口...")
    for n in (3, 2, 1):
        print(n)
        time.sleep(1)
    if method == "clipboard":
        result = paste_clipboard(text)
        print("paste result: %s (method=%s)" % (result, PASTE_METHOD))
    else:
        type_text(text)
        print("paste result: inject")
    cur = get_clipboard_text()
    print("done. clipboard: %s" % ((cur or "")[:40] or "(empty)"))
    return 0


if __name__ == "__main__":
    if "--devices" in sys.argv:
        list_devices()
    elif "--paste-test" in sys.argv:
        sys.exit(paste_test(sys.argv))
    else:
        main()
