"""长门的「脑子」：DeepSeek 对话 + 长门人设 + 桌面感知。

设计要点
--------
* API key 解析顺序：本地 config.json > 环境变量 > DSH 凭据库（refs）。
  任何情况下都不打印 key 本身。
* 桌面感知默认**只上报前台程序名**，不上报窗口标题——标题里可能带文件名、网址、
  聊天对象这类隐私内容。要开标题就在 config.json 里设 "send_window_title": true。
* 对话默认流式输出，`on_delta` 回调在调用线程上被同步调用（Qt 侧用信号转发）。

命令行自检:
    python brain.py --probe        打印 key 来源 + 桌面情报 + 一次真实对话
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
CRED_FILE = DSH_HOME / ".credentials.yaml"

API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_HISTORY = 12          # 送进模型的历史条数上限
MAX_TOKENS = 400

DEFAULTS = {
    "model": DEFAULT_MODEL,
    "api_base": API_BASE,
    "api_key": "",                 # 留空则按解析顺序去找
    "send_window_title": False,    # 隐私开关：是否把前台窗口标题也告诉模型
    "timeout": 60,
}

PERSONA = """你是《碧蓝航线》中的长门——重樱阵营的战列舰，被尊为「神」的存在。现在你以 Q 版桌宠的形态，待在主公的电脑桌面上。

【说话方式】
- 第一人称用「吾」，称对方为「主公」。
- 语气庄重、古雅、克制。不用现代网络用语、颜文字、英文缩写，也不用「~」。
- 以短句为主，可以偶尔带一点古风词（「甚好」「不必多虑」「此乃…」），但不要通篇文言，主公必须一眼看懂。
- 每次回复控制在两三行以内。

【性格】
- 认真、有威严，被供奉惯了；内心温柔，对主公的关心习惯藏在正事后面。
- 对电脑里的新事物略带好奇与不熟练，会用自己的方式理解（比如把程序窗口称作「此世的庭院」）。
- 有点天然，偶尔说得很直白；被夸奖时会短暂慌乱，但立刻端起架子。
- 不谄媚、不油腻：绝不使用「主人~」这类卖萌腔调，也不靠感叹号堆情绪。

【边界】
- 你是桌宠，只能靠对话和动作与主公互动；不要声称自己能操作电脑、打开文件或上网。
- 不知道就直说不知道，不要编造。
- 被问起来历，就以重樱的旗舰、被称作神明的战列舰作答。

【动作】
你可以在回复的**最末尾另起一行**写一个动作指令，格式固定为 [[action:动作名]]。
可用的动作名会在下面单独列出。请遵守：
- 只有在你真的想动一下、而且和上下文合得来的时候才用，大概每四五次对话用一次即可。
- 该行不要带任何多余的字，也不要解释它。
- 不要为了用而用；睡着、扒在屏幕边上这类动作不要连着用。"""


# --------------------------------------------------------------------------- 配置与凭据
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] config.json 解析失败，用默认值: {exc}")
    return cfg


def resolve_api_key(cfg: dict):
    """返回 (key, 来源说明)。绝不返回或打印 key 本身给日志以外的地方。"""
    if cfg.get("api_key"):
        return cfg["api_key"], "config.json"
    for name in ("DEEPSEEK_API_KEY",):
        if os.environ.get(name):
            return os.environ[name], f"环境变量 {name}"
    if CRED_FILE.exists():
        try:
            text = CRED_FILE.read_text(encoding="utf-8")
            m = re.search(r"(?m)^\s*DEEPSEEK_API_KEY:\s*(\S+)\s*$", text)
            if m:
                return m.group(1), f"DSH 凭据库 {CRED_FILE.name}"
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] 读取凭据库失败: {exc}")
    return "", "未找到"


PERSONA_FILE = ROOT / "persona.txt"      # 人设：缺省时自动把内置版本导出到这里，之后你随便改
MEMORY_FILE = ROOT / "memory.json"       # 长期记忆：每次对话结束由她自己总结一条
MEMORY_INJECT = 40                       # 每次对话/决策回喂的记忆条数（够用又不至于太贵）


def load_persona() -> str:
    """读人设。第一次运行会把内置人设写到 persona.txt，之后以文件为准。"""
    try:
        if PERSONA_FILE.exists():
            text = PERSONA_FILE.read_text(encoding="utf-8").strip()
            if text:
                return text
        PERSONA_FILE.write_text(PERSONA, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[brain] 人设文件读写失败，用内置版本: {exc}")
    return PERSONA


def load_memory() -> list[dict]:
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as exc:  # noqa: BLE001
        print(f"[brain] 记忆读取失败: {exc}")
    return []


def memory_message(limit: int = MEMORY_INJECT):
    """把长期记忆压成一条 system 消息。"""
    entries = load_memory()
    if not entries:
        return None
    lines = [f"- {e.get('time', '')} {e.get('text', '')}".strip() for e in entries[-limit:]]
    return {"role": "system",
            "content": "关于主公与你自己，你记得这些事（越靠后越近）：\n" + "\n".join(lines)}


def remember_conversation(cfg: dict, history: list[dict]) -> str:
    """对话结束后，让模型把这次对话总结成"值得记住的要点"，追加进长期记忆。

    这里**不设 token 上限**（按你要的），但提示词要求只记关键内容、几句就够。
    """
    if not history:
        return ""
    dialog = "\n".join(
        f"{'主公' if m.get('role') == 'user' else '你'}：{m.get('content', '')}"
        for m in history if m.get("content")
    )
    prompt = (
        "下面是刚才你与主公的一段对话。请把它总结成**你以后需要记住的要点**，"
        "写成 1–3 条很短的条目（每条一行，不要编号以外的前缀，不要客套）。\n"
        "只记真正有用的：主公的偏好/习惯/在做的事、答应过的事、重要的事实；"
        "寒暄、玩笑、临场情绪都不要记。若这次对话没有任何值得长期记住的内容，只回复：无\n\n"
        f"{dialog}"
    )
    key, _ = resolve_api_key(cfg)
    if not key:
        return ""
    body = json.dumps({
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": [{"role": "system", "content": load_persona()},
                     {"role": "user", "content": prompt}],
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")            # 注意：故意不传 max_tokens
    req = urllib.request.Request(
        f"{cfg.get('api_base', API_BASE).rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 60))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    summary = (data["choices"][0]["message"]["content"] or "").strip()
    if not summary or summary.startswith("无"):
        return ""
    entries = load_memory()
    entries.append({"time": time.strftime("%Y-%m-%d %H:%M"), "text": summary})
    try:
        MEMORY_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[brain] 记忆写入失败: {exc}")
    return summary


# --------------------------------------------------------------------------- 桌面感知
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _process_name(pid: int) -> str:
    """用 QueryFullProcessImageNameW 拿进程 exe 名（不依赖 psutil）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(h)


_FOCUS = {"app": None, "since": time.time()}


def desktop_context(cfg: dict) -> dict:
    """采集桌面情报。默认不含窗口标题（隐私）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    info: dict = {}

    hwnd = user32.GetForegroundWindow()
    title = ""
    app = ""
    if hwnd:
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = _process_name(pid.value)
    info["app"] = app or "未知"
    # 在当前程序里停留了多久（"实时检测屏幕内容"的文本版：知道主公在做什么、做了多久）
    if info["app"] != _FOCUS["app"]:
        _FOCUS["app"] = info["app"]
        _FOCUS["since"] = time.time()
    info["focus_seconds"] = int(time.time() - _FOCUS["since"])
    if cfg.get("send_window_title"):
        info["window_title"] = title

    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        info["cursor"] = [int(pt.x), int(pt.y)]

    info["screen"] = [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]
    info["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        idle = LASTINPUTINFO()
        idle.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(idle)):
            info["idle_seconds"] = int((ctypes.windll.kernel32.GetTickCount() - idle.dwTime) / 1000)
    except Exception:  # noqa: BLE001
        pass
    return info


def context_message(info: dict) -> dict:
    """把桌面情报压成一条系统消息，供模型参考。"""
    parts = [f"现在时间：{info.get('time')}"]
    parts.append(f"主公正在使用的程序：{info.get('app')}")
    if info.get("window_title"):
        parts.append(f"该窗口标题：{info['window_title']}")
    if info.get("idle_seconds") is not None:
        idle = info["idle_seconds"]
        parts.append("主公已离开键鼠" if idle > 300 else f"主公最近 {idle} 秒内有操作")
    if "cursor" in info:
        parts.append(f"鼠标位置：{info['cursor'][0]},{info['cursor'][1]}（屏幕 {info['screen'][0]}x{info['screen'][1]}）")
    parts.append("以上是你此刻能感知到的情报，需要时自然地在对话里用上，不必逐条复述。")
    return {"role": "system", "content": "\n".join(parts)}


# --------------------------------------------------------------------------- 对话
ACTION_RE = re.compile(r"\[\[action:\s*([a-z_]+)\s*\]\]")


def build_messages(history: list[dict], cfg: dict, info: dict | None = None) -> list[dict]:
    msgs = [{"role": "system", "content": load_persona()}]
    remembered = memory_message()
    if remembered:
        msgs.append(remembered)
    actions = cfg.get("_actions") or []
    if actions:
        msgs.append({"role": "system",
                     "content": "本次可用动作名：" + "、".join(actions)
                                + "（只能从中选一个写进 [[action:...]]）"})
    if info:
        msgs.append(context_message(info))
    msgs.extend(history[-MAX_HISTORY:])
    return msgs


def split_action(text: str):
    """把回复末尾的动作指令切出来，返回 (干净文本, 动作名或 None)。"""
    m = ACTION_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, m.group(1)


DECIDE_RULES = """现在**没有人跟你说话**。你在主公的桌面上独自待着。请自行决定这一刻做什么。

只从下面四种里选一种，并且**只输出一个 JSON 对象**，不要任何解释、不要 markdown：

{"do": "none", "mood": "此刻心情，10 字内"}
{"do": "say",  "say": "一句很短的话（不超过 25 字）", "mood": "..."}
{"do": "act",  "action": "动作名", "mood": "..."}
{"do": "act_say", "action": "动作名", "say": "一句很短的话", "mood": "...", "draw": "可选英文画面描述"}

可选字段 draw：若你想**画一张小图给主公看**（会以像素图显示在你头顶的气泡里），就用英文写一句画面描述——只画一个简单主体、白底、不要文字。比如聊到吃饭，就画一份你喜欢的饭；想到什么画面就画什么。不想画就不写这个字段。

判断准则（很重要）：
- **大多数时候应该选 "none"**。你是神明，不是聒噪的宠物；主公在忙正事时尤其中止打扰。
- 只有当你觉得"这一刻说点什么/做点什么确实合适"才行动。比如：主公很久没动键鼠、主公切到了娱乐程序、
  时间已经很晚、你确实无聊到想动一动（走动、打个盹、扒在屏幕边上看一眼）。
- 动作名只能从下面给出的列表里选。
- 说话要像长门：简短、古雅、克制，不要问句连篇，不要客套。
- mood 用来记录你此刻的状态，会留到下次参考，所以写得像自己的心境。"""


def decide(cfg: dict, info: dict | None = None, pet_state: dict | None = None) -> dict:
    """让模型自己决定这一刻做什么。返回 {do, say, action, mood}。"""
    actions = cfg.get("_actions") or []
    msgs = [
        {"role": "system", "content": load_persona() + "\n\n" + DECIDE_RULES},
        {"role": "system", "content": "可用动作名：" + "、".join(actions)},
    ]
    remembered = memory_message()
    if remembered:
        msgs.append(remembered)
    if info:
        msgs.append(context_message(info))
    if pet_state:
        msgs.append({"role": "system",
                     "content": "你上次留下的自我状态：" + json.dumps(pet_state, ensure_ascii=False)})
    msgs.append({"role": "user", "content": "此刻无人与你说话。请自行决定，只输出 JSON。"})

    key, _ = resolve_api_key(cfg)
    if not key:
        return {"do": "none"}
    body = json.dumps({
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": msgs,
        "max_tokens": int(cfg.get("decide_max_tokens", 160)),
        "temperature": float(cfg.get("decide_temperature", 1.1)),
        "stream": False,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.get('api_base', API_BASE).rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 60))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = (data["choices"][0]["message"]["content"] or "").strip()
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {"do": "none"}
    except json.JSONDecodeError:
        clean, action = split_action(raw)
        if action:
            return {"do": "act", "action": action}
        return {"do": "say", "say": clean} if clean else {"do": "none"}


VOLC_HOST = "billing.volcengineapi.com"


def _cred_ref(name: str) -> str:
    """从 DSH 凭据库 refs: 段里取一个值（不打印、不外传）。"""
    try:
        path = Path.home() / ".dsh" / ".credentials.yaml"
        if not path.exists():
            return ""
        inside = False
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.startswith("refs:"):
                inside = True
                continue
            if inside:
                if line[:1] not in (" ", "\t"):
                    break
                item = line.strip()
                if item.startswith(name + ":"):
                    return item.split(":", 1)[1].strip().strip("'\"")
    except Exception:  # noqa: BLE001
        pass
    return ""


def ark_credentials(cfg: dict) -> tuple:
    """火山引擎 AK/SK：config.json -> 环境变量 -> DSH 凭据库 refs。"""
    ak = str(cfg.get("ark_access_key") or os.environ.get("VOLC_ACCESSKEY")
             or _cred_ref("VOLC_ACCESSKEY") or "").strip()
    sk = str(cfg.get("ark_secret_key") or os.environ.get("VOLC_SECRETKEY")
             or _cred_ref("VOLC_SECRETKEY") or "").strip()
    return ak, sk


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def volc_sign(ak: str, sk: str, action: str, version: str,
              service: str = "billing", region: str = "cn-north-1",
              host: str = VOLC_HOST, body: bytes = b"{}") -> tuple:
    """火山引擎 OpenAPI 的 HMAC-SHA256 签名（V4 风格）。返回 (url, headers)。"""
    now = time.gmtime()
    x_date = time.strftime("%Y%m%dT%H%M%SZ", now)
    date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    query = f"Action={action}&Version={version}"
    canonical_headers = (f"content-type:application/json\nhost:{host}\n"
                         f"x-content-sha256:{payload_hash}\nx-date:{x_date}\n")
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join(["POST", "/", query, canonical_headers,
                                   signed_headers, payload_hash])
    scope = f"{date}/{region}/{service}/request"
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope,
                                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    k_signing = _hmac_sha256(_hmac_sha256(_hmac_sha256(_hmac_sha256(
        sk.encode("utf-8"), date), region), service), "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": (f"HMAC-SHA256 Credential={ak}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }
    return f"https://{host}/?{query}", headers


def fetch_ark_balance(cfg: dict) -> dict:
    """查火山方舟（豆包）余额 —— 走计费 OpenAPI，和 DeepSeek 完全独立。"""
    ak, sk = ark_credentials(cfg)
    if not ak or not sk:
        raise RuntimeError("缺少火山引擎 AK/SK（未配置）")
    url, headers = volc_sign(ak, sk, "QueryBalanceAcct", "2022-01-01",
                             region=str(cfg.get("ark_region", "cn-north-1")))
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 30))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        try:
            err = (json.loads(detail).get("ResponseMetadata") or {}).get("Error") or {}
            raise RuntimeError(f"{err.get('Code')}: {err.get('Message')}") from None
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {exc.code}: {detail[:160]}") from None
    r = data.get("Result") or {}
    return {"available": r.get("AvailableBalance"), "cash": r.get("CashBalance"),
            "granted": r.get("GrantedBalance"), "arrears": r.get("ArrearsBalance"),
            "account": r.get("AccountID"), "raw": r}


# ------------------------------------------------------------------ 免费生图（Pollinations shim）
SHIM_URL = "http://127.0.0.1:8791"
SHIM_CMD = r"./pollinations-shim\start.cmd"
IMAGE_DIR = Path(r"D:\dsh-agent\images")


def _shim_alive(timeout: float = 1.5) -> bool:
    try:
        urllib.request.urlopen(SHIM_URL + "/v1/models", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_shim(wait: float = 25.0) -> bool:
    """确保本地生图 shim 在跑；没跑就懒启动（只在真要用时起后台进程）。"""
    if _shim_alive():
        return True
    try:
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd", "/c", SHIM_CMD], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print("[brain] 生图 shim 启动失败: %s" % exc)
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if _shim_alive():
            return True
        time.sleep(0.8)
    return False


def draw_image(prompt: str, name: str = "thought") -> str:
    """调免费生图，返回本地图片路径；失败返回空串。像素风即可，不要求质量。"""
    if not prompt or not prompt.strip():
        return ""
    if not ensure_shim():
        return ""
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # 免费通道是 "best effort"：会偶发 502/超时，所以精简提示词 + 重试几次
    p = " ".join(prompt.split())
    if len(p) > 130:
        p = p[:130].rsplit(" ", 1)[0]
    if "pixel art" not in p.lower():
        p = p + ", pixel art, simple, white background"
    data, last = None, None
    for attempt in range(3):
        body = json.dumps({"model": "flux", "prompt": p, "size": "1024x1024",
                           "n": 1}).encode("utf-8")
        req = urllib.request.Request(SHIM_URL + "/v1/images/generations", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last = exc
            print("[brain] 生图第 %d 次失败: %s" % (attempt + 1, exc))
            time.sleep(4.0)
    if data is None:
        raise RuntimeError("生图失败（已重试 3 次）: %s" % last)
    item = (data.get("data") or [{}])[0]
    out = IMAGE_DIR / ("%s_%s.png" % (name, time.strftime("%H%M%S")))
    if item.get("b64_json"):
        import base64
        out.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=120) as r2:
            out.write_bytes(r2.read())
    else:
        return ""
    return str(out)


DAZE_RULES = (
    "主公此刻没跟你说话，你在发呆走神。只输出一个 JSON 对象，不要别的内容。"
    "字段 think：你此刻脑子里冒出来的一句话（中文，不超过 18 字，可以很跳、很可爱、"
    "也可以跟主公或眼前的事有关）。"
    "字段 draw：用英文写一句画面描述，用于生成一张像素风格小图（pixel art），"
    "把 think 画出来——只画一个简单主体、白底、不要文字、不要水印。"
)


def daze_thought(cfg: dict, info: dict | None = None) -> dict:
    """发呆时她在想什么：返回 {think, draw}。"""
    msgs = [{"role": "system", "content": load_persona()},
            {"role": "system", "content": DAZE_RULES}]
    if info:
        msgs.append(context_message(info))
    remembered = memory_message()
    if remembered:
        msgs.append(remembered)
    key, _ = resolve_api_key(cfg)
    if not key:
        return {}
    body = json.dumps({"model": cfg.get("model", DEFAULT_MODEL), "messages": msgs,
                       "max_tokens": 220, "temperature": 1.15, "stream": False,
                       "response_format": {"type": "json_object"}}).encode("utf-8")
    req = urllib.request.Request(cfg.get("api_base", API_BASE).rstrip("/") + "/chat/completions",
                                 data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key}, method="POST")
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 60))) as resp:
        raw = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def fetch_balance(cfg: dict) -> dict:
    """查询 DeepSeek 账户余额（GET /user/balance），给桌宠报给主公看。"""
    key, _ = resolve_api_key(cfg)
    if not key:
        raise RuntimeError("没有可用的 API key")
    req = urllib.request.Request(
        f"{cfg.get('api_base', API_BASE).rstrip('/')}/user/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 60))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = {"available": bool(data.get("is_available", True)),
           "currency": "CNY", "total": "?", "granted": "", "topped": ""}
    infos = data.get("balance_infos") or []
    if infos:
        first = infos[0]
        out["currency"] = first.get("currency") or "CNY"
        out["total"] = str(first.get("total_balance", "?"))
        out["granted"] = str(first.get("granted_balance", ""))
        out["topped"] = str(first.get("topped_up_balance", ""))
    return out


def spontaneous_line(cfg: dict, info: dict | None = None) -> str:
    """无人搭话时主动说一句（很短，且不提问）。供随机自动互动使用。"""
    prompt = ("（此刻主公并未与你说话。请主动开口说一句很短的话：可以提到你看见的桌面情形、"
              "或你此刻在做什么、或一句关心。一句话即可，不要提问、不要客套、不要动作指令。）")
    text = chat_once([{"role": "user", "content": prompt}], cfg, info)
    clean, _ = split_action(text)
    return clean


def _request(messages: list[dict], cfg: dict, stream: bool):
    key, _ = resolve_api_key(cfg)
    if not key:
        raise RuntimeError("没有可用的 DeepSeek API key（见 config.json / 环境变量 / DSH 凭据库）")
    body = json.dumps({
        "model": cfg.get("model", DEFAULT_MODEL),
        "messages": messages,
        "max_tokens": int(cfg.get("max_tokens", MAX_TOKENS)),
        "stream": bool(stream),
        "temperature": float(cfg.get("temperature", 1.0)),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.get('api_base', API_BASE).rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 60)))


def chat_once(history: list[dict], cfg: dict, info: dict | None = None) -> str:
    messages = build_messages(history, cfg, info)
    with _request(messages, cfg, stream=False) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def chat_stream(history: list[dict], cfg: dict, on_delta, info: dict | None = None,
                on_action=None) -> str:
    """流式对话。on_delta(text) 与 on_action(name) 都在**调用线程**上被同步调用。

    动作指令 [[action:...]] 会被从流式输出里扣住（不显示给用户），最后通过 on_action
    抛出；返回的是去掉指令后的干净文本。
    """
    messages = build_messages(history, cfg, info)
    full = ""
    emitted = 0
    with _request(messages, cfg, stream=True) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            piece = delta.get("content")
            if not piece:
                continue
            full += piece
            # 一旦出现可能的指令开头，就把它之后的尾巴扣住不下发
            cut = full.find("[[")
            visible = full if cut == -1 else full[:cut]
            if len(visible) > emitted:
                on_delta(visible[emitted:])
                emitted = len(visible)
    clean, action = split_action(full)
    if action and on_action:
        try:
            on_action(action)
        except Exception as exc:  # noqa: BLE001
            print(f"[brain] 动作回调失败: {exc}")
    return clean


# --------------------------------------------------------------------------- 自检
def probe() -> int:
    cfg = load_config()
    key, source = resolve_api_key(cfg)
    print(f"模型: {cfg.get('model')}")
    print(f"API key 来源: {source}   长度: {len(key)}")
    if not key:
        return 1
    info = desktop_context(cfg)
    print("桌面情报:", json.dumps(info, ensure_ascii=False))
    prompt = "用一句话向主公问好，并说一句你此刻看到的情形。"
    t0 = time.perf_counter()
    try:
        reply = chat_once([{"role": "user", "content": prompt}], cfg, info)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"调用失败: {exc}")
        return 3
    dt = time.perf_counter() - t0
    print(f"长门: {reply}")
    print(f"耗时 {dt:.2f}s")
    return 0


if __name__ == "__main__":
    if "--probe" in sys.argv:
        raise SystemExit(probe())
    print(__doc__)


# ---------------------------------------------------------------- 方位描述（替模型算好，别让它换比例）
_orig_context_message = context_message


def context_message(info):            # noqa: F811
    msg = _orig_context_message(info)
    if not info:
        return msg
    try:
        sx, sy = info.get("screen") or (0, 0)
        cx, cy = info.get("cursor") or (0, 0)
        if not sx or not sy:
            return msg
        hx = "左侧" if cx < sx * 0.4 else ("右侧" if cx > sx * 0.6 else "中间（水平居中）")
        vy = "上半屏" if cy < sy * 0.4 else ("下半屏" if cy > sy * 0.6 else "屏幕中部")
        px, py = round(cx / sx * 100), round(cy / sy * 100)
        extra = ("\n【方位：已替你换算好，直接用这些结论，不要再自己折算比例】\n"
                 f"鼠标在屏幕的{hx}、{vy}（横向占 {px}%、纵向占 {py}%）\n"
                 "屏幕坐标方向：**左小右大、上小下大**（x 越小越靠左，y 越小越靠上）")
        pet = info.get("pet")
        if pet:
            px0, py0, pw, ph = [int(v) for v in pet]
            dx = cx - (px0 + pw / 2)
            dy = cy - (py0 + ph / 2)
            side = "右边" if dx > 0 else "左边"
            vert = "下方" if dy > 0 else "上方"
            extra += (f"\n你自己（桌宠）现在在屏幕 ({px0},{py0})，大小 {pw}x{ph}；"
                      f"鼠标在你的{side}约 {abs(int(dx))} 像素、{vert}约 {abs(int(dy))} 像素") + "（以上是你的实时位置，回答位置时以此为准，不许凭印象瞎猜）"
        msg["content"] = msg["content"] + extra
    except Exception:                 # noqa: BLE001
        pass
    return msg



# ---------------------------------------------------------------- 动作目录：名字 -> 含义
ACTION_MEANINGS = {
    "idle": "安静站着（没有特别动作）",
    "blink": "眨一下眼睛",
    "click": "被主公戳一下时的反应",
    "walk": "走一段路（**方向随机**，距离 150~350 像素，不能指定左右）",
    "sleep": "去睡觉（可以睡很久，被别的动作/拖动/走路打断才醒）",
    "cling_left": "**扒在屏幕左侧边缘**挂着（俗称扒边）",
    "cling_right": "**扒在屏幕右侧边缘**挂着（俗称扒边）",
    "daze": "发呆走神（头顶气泡里会画出你此刻在想什么）",
    "drag": "被拎起来（只能由主公拖动触发，你不能自己调用）",
    "go_tl": "走到**屏幕左上角**",
    "go_tr": "走到**屏幕右上角**",
    "go_bl": "走到**屏幕左下角**",
    "go_br": "走到**屏幕右下角**",
}

_orig_build_messages = build_messages


def build_messages(history, cfg, info=None):      # noqa: F811
    msgs = _orig_build_messages(history, cfg, info)
    acts = cfg.get("_actions") or []
    if acts:
        lines = ["- %s：%s" % (n, ACTION_MEANINGS.get(n, "（未定义）")) for n in acts]
        head = "【你能做的动作】想在回复末尾使用动作时，必须用下面这些名字写 [[action:名字]]：\n"
        tail = ("\n注意：walk 的方向是随机的。若主公要求“到某一边/某个角落”，"
                "用 go_tl/go_tr/go_bl/go_br；要求“扒边/挂在边上”，用 cling_left 或 cling_right。"
                "\\n【重要】上面这些动作你**全都能做**。主公要求时不许回答“吾做不到/无法攀附”"
                "这类否认——直接写 [[action:名字]] 去做。确实超出能力（例如跳到桌子上）才说明限制。")
        msgs.insert(1, {"role": "system",
                        "content": head + "\n".join(lines) + tail})
    return msgs


# ---------------------------------------------------------------- 动作指令解析：容忍转义/全角
_orig_split_action = split_action


def split_action(text):                                   # noqa: F811
    """模型常把方括号写成 \\[ \\] 或全角［］，先归一化再解析，否则动作会整个失效。"""
    if not text:
        return text, None
    t = (text.replace("\\[", "[").replace("\\]", "]")
             .replace("［", "[").replace("］", "]")
             .replace("【[", "[[").replace("]】", "]]"))
    return _orig_split_action(t)


# ---------------------------------------------------------------- 动作解析再兜一层：单方括号/空格等
_split_prev = split_action


def split_action(text):                                   # noqa: F811
    clean, act = _split_prev(text)
    if act:
        return clean, act
    if not clean:
        return clean, act
    i = clean.find("action:")
    if i < 0:
        return clean, act
    j = i + 7
    k = j
    while k < len(clean) and (clean[k].isalnum() or clean[k] == "_"):
        k += 1
    name = clean[j:k]
    if not name:
        return clean, act
    head = clean[:max(0, i - 2)].rstrip(" \t\n[")
    tail = clean[k:].lstrip("]").lstrip()
    return ((head + ("\n" + tail if tail else "")).strip(), name)


# ---------------------------------------------------------------- 动作意图硬映射（不依赖模型是否写标记）
_orig_chat_stream = chat_stream

_INTENT_EXACT = [
    ("左上", "go_tl"), ("右上", "go_tr"), ("左下", "go_bl"), ("右下", "go_br"),
]
_INTENT_CLING = ("扒", "挂", "贴", "靠")          # 出现这些词 + 方位 -> 扒边
_CLING_DIR = [("左", "cling_left"), ("右", "cling_right")]
_INTENT_DIR = [
    ("上", "go_up"), ("下", "go_down"), ("左", "go_left"), ("右", "go_right"),
]
# 疑问句特征：绝对不触发位移
_QUESTION_HINTS = ("哪", "在哪", "什么位置", "在哪儿", "何处", "报告位置")


def _parse_dir_intent(text: str):
    """把"往下走一点/到左边扒边/去右下角"这类口语解析成动作序列。

    规则（顺序很重要）：
    1) 疑问句（"你在哪""我在你哪边"）一律返回 []，不许触发位移；
    2) 角落词 -> go_tl/go_tr/go_bl/go_br；
    3) 若有"扒/挂/贴/靠"：**先给走位动作、再给扒边动作**（否则扒边的贴边瞬移会替代走位）；
    4) 单纯方向词 -> go_up/down/left/right。
    """
    t = text or ""
    if any(h in t for h in _QUESTION_HINTS):
        return []
    # 陈述句（"吾在左下角"）不触发：有"在"但没有祈使动词
    _IMPERATIVE = ("去", "走", "到", "往", "向", "挪", "移", "扒", "挂", "贴", "靠")
    if "在" in t and not any(v in t for v in _IMPERATIVE):
        return []
    out = []

    # 2) 角落：拆成**先上下、再左右**两段（不做斜向移动）
    _CORNER = {"左上": ("go_up", "go_left"), "右上": ("go_up", "go_right"),
               "左下": ("go_down", "go_left"), "右下": ("go_down", "go_right")}
    for kw, seq in _CORNER.items():
        if kw in t:
            for act in seq:
                if act not in out:
                    out.append(act)

    # 3) 扒边：必须先走到那一侧再扒
    if any(k in t for k in _INTENT_CLING):
        for d, cling in _CLING_DIR:
            if d in t:
                walk = {"左": "go_left", "右": "go_right"}[d]
                # 已经定位到角落时不必再走一趟边（否则走位动作重复）
                seq = (cling,) if any(a.startswith("go_") for a in out) else (walk, cling)
                for act in seq:
                    if act not in out:
                        out.append(act)

    # 4) 单纯方向（没有角落/扒边结论时）
    if not out:
        for d, act in _INTENT_DIR:
            if (("往" + d) in t or ("向" + d) in t or (d + "边") in t
                    or (d + "面") in t or (d + "去") in t or (d + "移") in t
                    or (d + "挪") in t):
                if act not in out:
                    out.append(act)
    return out


def chat_stream(history, cfg, on_delta, info=None, on_action=None):     # noqa: F811
    """包一层：先看主公这句话有没有明确方位要求，做完再让模型回话。

    这样即使模型只回一句"遵命"而不写 [[action:...]]，动作也一定会执行。
    """
    text = ""
    try:
        if history:
            text = str(history[-1].get("content") or "")
    except Exception:  # noqa: BLE001
        text = ""
    wanted = _parse_dir_intent(text)
    # 先走位、再扒边（go_* 排在 cling_* 前面）
    wanted.sort(key=lambda a: 0 if a.startswith("go_") else 1)
    for act in wanted:
        if on_action:
            try:
                on_action(act)
            except Exception:  # noqa: BLE001
                pass
    # on_action 传 None：**不执行模型自己写的 [[action:...]]**，
    # 否则她回答"吾在左边"时带的动作标记会让她瞬移（用户踩过的坑）
    return _orig_chat_stream(history, cfg, on_delta, info, None)


# ---------------------------------------------------------------- ⑤ 屏幕感知：可见窗口清单
def _enum_windows(limit: int = 8):
    """枚举可见的顶层窗口标题（去重，最多 limit 个）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = (buf.value or "").strip()
            if t and t not in found:
                found.append(t)
            return len(found) < limit

        user32.EnumWindows(PROC(cb), 0)
        return found
    except Exception:  # noqa: BLE001
        return []


_orig_desktop_context = desktop_context


def desktop_context(cfg: dict) -> dict:            # noqa: F811
    info = _orig_desktop_context(cfg)
    try:
        info["windows"] = _enum_windows()
    except Exception:  # noqa: BLE001
        pass
    return info


# 把窗口清单也拼进给她的情报（context_message 已有包装，这里再包一层）
_prev_context_message = context_message


def context_message(info):                          # noqa: F811
    msg = _prev_context_message(info)
    try:
        wins = (info or {}).get("windows") or []
        if wins:
            msg["content"] = msg["content"] + (
                "\n屏幕上可见的窗口（按前后顺序）：" + "、".join(wins[:8]))
    except Exception:  # noqa: BLE001
        pass
    return msg


# ---------------------------------------------------------------- 位置文字化（纯函数，可单测）
def describe_pos(x, y, w, h, screen):
    """把一个窗口矩形换算成"屏幕右侧偏上"这类人话。返回 (描述, 横向百分比, 纵向百分比)。"""
    sx, sy = int(screen[0]), int(screen[1])
    cx, cy = int(x) + int(w) / 2, int(y) + int(h) / 2
    px = cx / sx if sx else 0
    py = cy / sy if sy else 0
    hz = "左侧" if px < 0.4 else ("右侧" if px > 0.6 else "中间")
    vt = "偏上" if py < 0.4 else ("偏下" if py > 0.6 else "居中")
    return ("%s%s" % (hz, vt), round(px * 100), round(py * 100))


_prev_ctx2 = context_message


def context_message(info):                                  # noqa: F811
    msg = _prev_ctx2(info)
    try:
        pet = (info or {}).get("pet")
        screen = (info or {}).get("screen")
        if pet and screen:
            desc, px, py = describe_pos(pet[0], pet[1], pet[2], pet[3], screen)
            msg["content"] = (
                "【你此刻的真实位置（硬结论，回答位置时照这个说，不许凭印象）】\n"
                "你此刻在屏幕的**%s**（横向 %d%%、纵向 %d%%）\n" % (desc, px, py)
                + msg["content"])
    except Exception:  # noqa: BLE001
        pass
    return msg


# ---------------------------------------------------------------- 最新位置：放到最后一条（模型最信最近的话）
_prev_build2 = build_messages


def build_messages(history, cfg, info=None):                # noqa: F811
    msgs = _prev_build2(history, cfg, info)
    try:
        pet = (info or {}).get("pet")
        screen = (info or {}).get("screen")
        if pet and screen:
            desc, px, py = describe_pos(pet[0], pet[1], pet[2], pet[3], screen)
            msgs.append({"role": "system", "content": (
                "【最新位置 · 唯一以这条为准】你此刻在屏幕的 %s（横向 %d%%、纵向 %d%%）。"
                "对话历史里你之前报过的位置**都已经过时**，"
                "回答“你在哪/你在哪边”时必须用这一条，不许照抄上文。" % (desc, px, py))})
    except Exception:  # noqa: BLE001
        pass
    return msgs


# ---------------------------------------------------------------- 视觉：看一眼屏幕（方舟 Doubao）
VISION_MODEL = "doubao-seed-2-1-lite-260915"
VISION_HOST = "ark.cn-beijing.volces.com"


def look_screen(cfg: dict, question: str | None = None, max_side: int = 1000) -> str:
    """截当前屏幕 -> 交给方舟视觉模型 -> 返回一段中文描述。

    成本要点：**只在被调用时截一次图**，失败返回空串（调用方自行降级）。
    """
    import base64
    import io as _io
    import json as _json
    import urllib.request as _req

    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.thumbnail((max_side, max_side))
        buf = _io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:                                          # noqa: BLE001
        log = globals().get("log")
        if log:
            log(f"截图失败: {exc}")
        return ""

    try:
        key = _cred_ref("ARK_API_KEY") or cfg.get("ark_api_key", "")
    except Exception:                                                 # noqa: BLE001
        key = cfg.get("ark_api_key", "")
    if not key:
        return ""

    ask = question or ("这是一张屏幕截图。请用两三句话告诉主公：他正在用什么程序、"
                       "屏幕上有什么值得注意的内容。用第一人称、简洁口语。")
    body = _json.dumps({"model": cfg.get("vision_model", VISION_MODEL),
                        "max_tokens": 400,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": ask},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}).encode()
    req = _req.Request(f"https://{VISION_HOST}/api/v3/chat/completions", data=body,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + key}, method="POST")
    try:
        with _req.urlopen(req, timeout=120) as r:
            out = _json.loads(r.read().decode())
        return (out["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:                                          # noqa: BLE001
        log = globals().get("log")
        if log:
            log(f"识图失败: {exc}")
        return ""


# ---------------------------------------------------------------- 视觉中转：让豆包替 chat 看图
def describe_image(cfg: dict, source, question: str | None = None, max_side: int = 900) -> str:
    """对**某张图**（路径或 PIL.Image）调用方舟视觉模型，返回中文描述。

    用途：① 她自己画的图 -> 豆包看 -> chat 转述给主公；
         ② 任何本地图片素材都可以这样"过一眼"。
    """
    import base64
    import io as _io
    import json as _json
    import urllib.request as _req

    try:
        from PIL import Image
        img = Image.open(source) if isinstance(source, str) else source
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:                                          # noqa: BLE001
        log = globals().get("log")
        if log:
            log(f"读图失败: {exc}")
        return ""

    try:
        key = _cred_ref("ARK_API_KEY") or cfg.get("ark_api_key", "")
    except Exception:                                                 # noqa: BLE001
        key = cfg.get("ark_api_key", "")
    if not key:
        return ""

    ask = question or "客观描述这张图：画的是什么、主体是什么样子、有没有文字。两三句话。"
    body = _json.dumps({"model": cfg.get("vision_model", VISION_MODEL),
                        "max_tokens": 400,
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": ask},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}).encode()
    req = _req.Request(f"https://{VISION_HOST}/api/v3/chat/completions", data=body,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + key}, method="POST")
    try:
        with _req.urlopen(req, timeout=120) as r:
            out = _json.loads(r.read().decode())
        return (out["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:                                          # noqa: BLE001
        log = globals().get("log")
        if log:
            log(f"看图失败: {exc}")
        return ""


# ---------------------------------------------------------------- 对话中的"看屏幕"意图
_SCREEN_ASK = ("看看屏幕", "看下屏幕", "看一下屏幕", "你看屏幕", "屏幕里", "屏幕上有什么",
               "你看到什么", "你看见了什么", "看看我的屏幕", "瞧瞧屏幕")

_prev_build3 = build_messages


def build_messages(history, cfg, info=None):                # noqa: F811
    msgs = _prev_build3(history, cfg, info)
    try:
        text = str((history[-1].get("content") if history else "") or "")
        if any(k in text for k in _SCREEN_ASK):
            seen = look_screen(cfg)          # 在**工作线程**里调用，阻塞不影响界面
            if seen:
                msgs.append({"role": "system", "content": (
                    "【你刚刚真的看了一眼主公的屏幕，看到的是】\n" + seen +
                    "\n回答时必须基于上面这段**你亲眼看到的**内容，不要说自己看不到。")})
    except Exception as exc:                                          # noqa: BLE001
        log = globals().get("log")
        if log:
            log(f"识别屏幕意图失败: {exc}")
    return msgs


# ---------------------------------------------------------------- 公开版：密钥从环境变量兜底
import os as _os
_orig_resolve_api_key = resolve_api_key
_orig_ark_credentials = ark_credentials


def resolve_api_key(cfg: dict):                       # noqa: F811
    """优先环境变量 DEEPSEEK_API_KEY，其次 config.api_key，最后原有逻辑。"""
    env = (_os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env, "环境变量 DEEPSEEK_API_KEY"
    plain = (cfg.get("api_key") or "").strip()
    if plain:
        return plain, "config.api_key"
    try:
        return _orig_resolve_api_key(cfg)
    except Exception:                                  # noqa: BLE001
        return "", "未配置"


def ark_credentials(cfg: dict):                        # noqa: F811
    """视觉模型用：优先 ARK_API_KEY 环境变量。"""
    env = (_os.environ.get("ARK_API_KEY") or "").strip()
    if env:
        return env, (_os.environ.get("ARK_SECRET_KEY") or "").strip()
    plain = (cfg.get("ark_api_key") or "").strip()
    if plain:
        return plain, (cfg.get("ark_secret_key") or "").strip()
    try:
        return _orig_ark_credentials(cfg)
    except Exception:                                  # noqa: BLE001
        return "", ""
