"""戴尔端语音代理（v5：热键事件走发送队列）。

职责：
  1. 轮询右 Ctrl，把按下/松开事件放进发送队列
  2. 独立发送线程负责 sendall 和心跳，热键线程不碰网络

SYF 负责录音、识别和投递；本代理不再接收或注入识别文本。
"""
import ctypes
import json
import os
import queue
import socket
import threading
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "remote_agent.log")

VK_RCONTROL = 0xA3
HEARTBEAT_INTERVAL = 2.0
SOCKET_TIMEOUT = 12.0
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


LOG_MAX_BYTES = 5 * 1024 * 1024   # 超过 5MB 就轮转，只保留上一份


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
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


def load_remote_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log("读取 config.json 失败: %s" % str(e)[:120])
        return None

    host = cfg.get("remote_host")
    if not isinstance(host, str) or not host.strip():
        log("config.json 的 remote_host 无效")
        return None
    try:
        port = int(cfg.get("remote_port"))
    except (TypeError, ValueError):
        log("config.json 的 remote_port 无效")
        return None
    if not 1 <= port <= 65535:
        log("config.json 的 remote_port 超出范围: %d" % port)
        return None
    token = cfg.get("remote_token")
    if not isinstance(token, str) or not token:
        log("config.json 的 remote_token 无效")
        return None
    return host.strip(), port, token


_mutex_handle = None


def acquire_single_instance(name):
    global _mutex_handle
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        log("创建单实例互斥失败: %d" % err)
        return False
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        log("已有实例在运行，本实例退出")
        return False
    _mutex_handle = h
    return True


_lock = threading.Lock()
_conn = {"sock": None, "outq": None, "stop": None}


def send(obj):
    """只负责入队；连接不存在时返回 False，不阻塞热键线程。"""
    with _lock:
        s = _conn["sock"]
        q = _conn["outq"]
    if s is None or q is None:
        return False
    q.put(obj)
    return True


def sender_loop(s, outq, stop):
    last_ping = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_ping >= HEARTBEAT_INTERVAL:
            obj = {"t": "ping"}
            last_ping = now
        else:
            try:
                obj = outq.get(timeout=max(0.05, HEARTBEAT_INTERVAL - (now - last_ping)))
            except queue.Empty:
                continue
        try:
            s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as e:
            log("发送失败: %s" % str(e)[:80])
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            break


def reader(s):
    buf = b""
    while True:
        data = s.recv(4096)
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if msg.get("t") == "ack":
                log("鉴权通过")


def connect_loop(host, port, token):
    while True:
        s = None
        stop = threading.Event()
        outq = queue.Queue()
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.settimeout(5)
            s.sendall((json.dumps({"t": "hello", "token": token}) + "\n").encode("utf-8"))
            s.settimeout(SOCKET_TIMEOUT)
            with _lock:
                _conn["sock"] = s
                _conn["outq"] = outq
                _conn["stop"] = stop
            log("已连接 SYF %s:%d" % (host, port))
            threading.Thread(target=sender_loop, args=(s, outq, stop), daemon=True).start()
            reader(s)
        except socket.timeout:
            log("连接/心跳超时")
        except Exception as e:
            log("连接失败/断开: %s" % str(e)[:120])
        finally:
            stop.set()
            with _lock:
                _conn["sock"] = None
                _conn["outq"] = None
                _conn["stop"] = None
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            log("连接已关闭，3 秒后重连")
        time.sleep(3)


def hotkey_loop():
    """轮询右 Ctrl；只把事件放进队列，不做任何网络 IO。"""
    log("开始轮询右 Ctrl（队列发送，无钩子）")
    last = False
    while True:
        try:
            down = bool(user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000)
            if down != last:
                last = down
                send({"t": "hotkey", "action": "down" if down else "up"})
                log("右 Ctrl %s" % ("按下" if down else "松开"))
        except Exception as e:
            log("轮询异常: %s" % str(e)[:80])
        time.sleep(0.015)


def main():
    if not acquire_single_instance("Local\\VoiceLinkAgent"):
        return
    cfg = load_remote_config()
    if cfg is None:
        return

    log("=== 代理启动 v5（队列发送，心跳，单实例）===")
    threading.Thread(target=connect_loop, args=cfg, daemon=True).start()
    hotkey_loop()


if __name__ == "__main__":
    main()
