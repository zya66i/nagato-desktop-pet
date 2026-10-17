"""Q版长门 桌面宠物 (DeepSeek Harness 生成的素材 + PySide6 运行时)。

数据驱动：角色、图层、部件、动作、尺寸全部来自 manifest.json。
换贴图或加动作都不需要改这个文件，规则见 README.md。

运行：
    .venv\\Scripts\\pythonw.exe pet.py            正常启动（无控制台窗口）
    .venv\\Scripts\\python.exe  pet.py --selftest out  渲染自测图，不显示窗口
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QGuiApplication, QImage, QPainter, QRegion, QTransform,
)
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget

try:                      # 对话能力是可选依赖：坏了也不该拖垮桌宠本体
    import brain
except Exception as _exc:  # noqa: BLE001
    brain = None
    _BRAIN_IMPORT_ERROR = str(_exc)

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
STATE_FILE = ROOT / "state.json"
PET_STATE_FILE = ROOT / "pet_state.json"
STATS_FILE = ROOT / "stats.json"               # 陪伴时长累计
SETTINGS_FILE = ROOT / "settings.json"         # 界面选项（例如盖被子）
COMPANION_LOG = ROOT / "companion.log"         # 陪伴时长检查点（只追加，永不覆盖）      # 长门自己的心境/记忆（由她自己的决策写入）
LOG_FILE = ROOT / "pet.log"

FPS = 60
MASK_DILATE = 2           # 点击遮罩膨胀像素，覆盖呼吸带来的亚像素位移
ALPHA_THRESHOLD = 8


def log(msg: str):
    """pythonw 下 stdout 可能是 None 且被块缓冲，所以同时落到文件里。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def acquire_single_instance(name: str = "nagato-pet-single-instance"):
    """建命名互斥体：快捷方式被连点两次时不至于出现两只桌宠。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
    if not handle:
        return None
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        return None
    return handle


# --------------------------------------------------------------------------- 动作数学
def motion_breathe(t: float, m: dict):
    """待机呼吸：绕底边锚点的纵向缩放 + 横向反向补偿。"""
    period = max(float(m.get("period", 3.0)), 0.05)
    amount = float(m.get("amount", 0.012))
    s = math.sin(2 * math.pi * t / period)
    return 1.0 - 0.55 * amount * s, 1.0 + amount * s, 0.0


def motion_pop(u: float, m: dict):
    """点击回弹：阻尼余弦，附带一个很轻的上跳。"""
    if u < 0:
        return 1.0, 1.0, 0.0
    period = max(float(m.get("period", 0.42)), 0.05)
    amount = float(m.get("amount", 0.07))
    decay = float(m.get("decay", 3.4))
    env = math.exp(-decay * u)
    c = math.cos(2 * math.pi * u / period)
    dy = -0.012 * env * max(0.0, math.sin(math.pi * u / 0.30))
    return 1.0 - 0.5 * amount * env * c, 1.0 + amount * env * c, dy


def motion_tilt(angle: float, m: dict):
    """被拖拽时按角度侧倾，并沿长轴轻微拉伸。"""
    max_deg = float(m.get("max_deg", 10.0))
    stretch = float(m.get("stretch", 0.03))
    f = min(1.0, abs(angle) / max_deg) if max_deg else 0.0
    return 1.0 - 0.5 * stretch * f, 1.0 + stretch * f, angle


# --------------------------------------------------------------------------- 图像工具
def image_alpha(img: QImage) -> np.ndarray:
    """取 ARGB32 图像的 alpha 通道为 (h, w) uint8 数组。

    必须返回**拷贝**：QImage 的内存由 C++ 侧持有，只返回 numpy 视图的话，函数返回后
    源 QImage 一旦被回收，视图就是野指针——表现为随机崩溃（0xC0000005 访问违规）。
    """
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    alpha = buf.reshape(h, img.bytesPerLine())[:, : w * 4].reshape(h, w, 4)[..., 3]
    return np.array(alpha, copy=True)


def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """可分离膨胀，避免 (2r+1)^2 次搬移。"""
    if r <= 0:
        return mask
    out = mask
    for axis in (0, 1):
        acc = out.copy()
        for k in range(1, r + 1):
            acc |= np.roll(out, k, axis=axis)
            acc |= np.roll(out, -k, axis=axis)
        out = acc
    return out


def mask_rects(mask: np.ndarray):
    """把布尔遮罩压成矩形列表：逐行取 run，再纵向合并相同 run。

    Qt6 的 QPixmap 不再支持 1 位深，QBitmap.fromImage(mono) 会直接抛访问违规
    （0xC0000005），所以「位图 -> QRegion」那条老路不能用，只能自己给矩形。
    """
    h, w = mask.shape
    rows = []
    for y in range(h):
        row = mask[y]
        if row.any():
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
            rows.append({(int(a), int(b - a)) for a, b in zip(edges[0::2], edges[1::2])})
        else:
            rows.append(set())

    rects = []
    active: dict[tuple[int, int], int] = {}   # (x, width) -> 起始行
    for y, runs in enumerate(rows):
        for key in [k for k in active if k not in runs]:
            x, wd = key
            y0 = active.pop(key)
            rects.append((x, y0, wd, y - y0))
        for key in runs:
            active.setdefault(key, y)
    for (x, wd), y0 in active.items():
        rects.append((x, y0, wd, h - y0))
    return rects


def region_from_mask(mask: np.ndarray) -> QRegion:
    region = QRegion()
    for x, y, wd, ht in mask_rects(mask):
        region = region.united(QRegion(x, y, wd, ht))
    return region


def load_layer(path: Path, size: int) -> QImage:
    img = QImage(str(path))
    if img.isNull():
        raise SystemExit(f"无法载入图层: {path}")
    img = img.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return img.convertToFormat(QImage.Format_ARGB32_Premultiplied)


# --------------------------------------------------------------------------- 素材
class PetAssets:
    """一个角色在给定显示尺寸下的全部图像资源与几何。"""

    def __init__(self, char_cfg: dict, target_char_h: float, dpr: float):
        self.cfg = char_cfg
        canvas_w, canvas_h = char_cfg["canvas"]
        char_h = float(char_cfg["character_height_px"])
        # 窗口尺寸要让「角色本身」等于目标高度，而不是让整张画布等于目标高度
        self.window_logical = max(48, round(target_char_h * canvas_h / char_h))
        self.device = max(48, round(self.window_logical * dpr))
        self.dpr = dpr
        # 锚点：角色底边（画布坐标 -> 0..1 比例）
        self.pivot_x = 0.5
        self.pivot_y = 1.0 - float(char_cfg.get("anchor_bottom_margin_px", 0)) / canvas_h

        self.layers = {k: load_layer(ROOT / v, self.device) for k, v in char_cfg["layers"].items()}
        self.parts = {k: load_layer(ROOT / v, self.device) for k, v in char_cfg.get("parts", {}).items()}

        # 点击穿透遮罩：只算一次（逻辑坐标），膨胀几像素覆盖呼吸的亚像素位移。
        # 取 base 与所有部件 alpha 的**并集**——行走时迈出去的腿会伸出底图轮廓，
        # 只按底图算的话那部分会点不到（直接穿透）。
        acc = None
        for img in [self.layers["base"], *self.parts.values()]:
            small = img.scaled(self.window_logical, self.window_logical,
                               Qt.IgnoreAspectRatio, Qt.FastTransformation)
            a = image_alpha(small) > ALPHA_THRESHOLD
            acc = a if acc is None else (acc | a)
        self.hit_region = region_from_mask(dilate(acc, MASK_DILATE))

    def _draw_body(self, p: QPainter, key):
        if key and key in self.parts:
            p.drawImage(0, 0, self.parts[key])
        else:
            p.drawImage(0, 0, self.layers["base"])

    def compose(self, sx: float, sy: float, angle: float, dx: float, dy: float,
                body: str | None = None, overlays=(), prev_body=None,
                blend: float = 1.0, overlay_alpha: float = 1.0) -> QImage:
        """body 是**替换**底图的身体帧（全身图，叠加会变重影）；overlays 是叠在身体上的
        面部羽化带。prev_body + blend<1 时，把上一个身体帧按 blend 淡出到新帧上，
        这样站立↔行走↔睡觉↔扒边之间不会"啪"地跳。"""
        img = QImage(self.device, self.device, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        cx, cy = self.pivot_x * self.device, self.pivot_y * self.device
        tr = QTransform()
        tr.translate(cx + dx * self.device, cy + dy * self.device)
        tr.rotate(angle)
        tr.scale(sx, sy)
        tr.translate(-cx, -cy)
        p.setTransform(tr)
        if prev_body != body and blend < 1.0:
            self._draw_body(p, prev_body)                # 旧姿势
            p.setOpacity(max(0.0, min(1.0, blend)))
            self._draw_body(p, body)                     # 新姿势淡入
            p.setOpacity(1.0)
        else:
            self._draw_body(p, body)
        if overlays:
            p.setOpacity(max(0.0, min(1.0, overlay_alpha)))
            for key in overlays:
                if key in self.parts:
                    p.drawImage(0, 0, self.parts[key])
            p.setOpacity(1.0)
        p.end()
        img.setDevicePixelRatio(self.dpr)
        return img


# --------------------------------------------------------------------------- 窗口
class PetWindow(QWidget):
    bubble_text = Signal(str)          # 后台线程 -> 主线程显示气泡
    decision_ready = Signal(str)       # 后台线程 -> 主线程应用"自主决定"的结果
    info_text = Signal(str)            # 后台线程 -> 主线程显示"信息类"气泡（停久一点）
    image_ready = Signal(str, str)     # 后台线程 -> 主线程显示"想法气泡"（图片路径, 旁白）
    action_request = Signal(str)       # **网络线程 -> GUI 线程**（Qt 控件只能主线程碰）

    def __init__(self, manifest: dict):
        super().__init__()
        self.manifest = manifest
        self.window_cfg = manifest.get("window", {})
        self.char_name = manifest.get("active", next(iter(manifest["characters"])))
        self.char_cfg = manifest["characters"][self.char_name]
        self.motions = self.char_cfg.get("motions", {})
        self.actions = self.char_cfg.get("actions", {})
        self.target_char_h = float(self.window_cfg.get("target_character_logical_height", 267))
        self.presets = self.window_cfg.get("size_presets", [self.target_char_h])
        self.dpr = self.screen().devicePixelRatio() if self.screen() else 1.0

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
            | (Qt.WindowStaysOnTopHint if self.window_cfg.get("always_on_top", True) else Qt.Widget)
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("长门 · 桌宠")

        self.dragging = False
        self.assets: PetAssets | None = None
        self.build_assets()

        self.now = time.perf_counter()
        self.tilt = 0.0
        self.tilt_target = 0.0
        self.dragging = False
        self.drag_offset = QPoint()
        self.vel_x = 0.0
        self.last_mouse_x = 0.0
        self.blink_until = 0.0
        self.next_blink = self.now + random.uniform(*self.actions.get("blink", {}).get("every", [3.5, 8.0]))
        self.reaction_until = 0.0
        self.reaction_start = 0.0
        self.reaction_overlay: str | None = None
        self.frame: QImage | None = None

        # ---- 巡游 / 对话状态
        self.behaviour = manifest.get("behaviour", {})
        self.roam_cfg = self.behaviour.get("roam", {})
        self.roam_target: float | None = None
        self.roam_dir = 1                      # +1 向右 / -1 向左，决定用哪套行走帧
        self.roam_pause_until = self.now + random.uniform(2.0, 5.0)
        self.roam_resume_at = 0.0
        self.walking = False
        self.walk_demo_until = 0.0
        self.chat = None
        self.bubble = None
        self._brain_cfg = None
        self.target_x = None               # 正在走向的 x（行走）
        self.action_name = None            # 当前长动作（sleep / cling_* 之类）
        self.action_until = 0.0
        self.action_frames = []
        self.cling_return_x = None
        self._cling_at_press = None
        self._drag_start_x = 0
        self._body_group = None
        self._last_body_key = None
        self._fade_from_body = None
        self._fade_start = 0.0
        self._overlay_alpha = 1.0
        self.action_start = 0.0
        self.blink_start = 0.0
        self.proactive_cfg = self.behaviour.get("proactive", {})
        self._proactive_none_streak = 0
        self.pet_state = self.load_pet_state()
        self.launch_time = time.time()
        self.quilt_on = bool(self.load_settings().get("quilt", False))
        self.always_on_top = bool(self.load_settings().get(
            "always_on_top", self.window_cfg.get("always_on_top", True)))
        self._topmost_next = 0.0
        self.skin = str(self.load_settings().get("skin", "default"))
        self.stats = self.load_stats()
        self._stats_flushed = 0.0
        self._balance = None
        self.next_proactive_at = self.now + self.next_proactive_delay()
        self.bubble_text.connect(self.show_bubble)
        self.decision_ready.connect(self.show_decision)
        self.info_text.connect(lambda t: self.show_bubble(t, seconds=15))
        self.image_ready.connect(self.show_thought_image)
        # 关键：DeepSeek 的 on_action 在**网络线程**上被调用；直接操作控件/QTimer 会卡死界面，
        # 所以统一走信号，由主线程执行 play_action。
        self.action_request.connect(self.play_action)
        self.img_bubble = None
        self._thinking = False
        self._press_pos: QPoint | None = None
        self._press_time = 0.0
        self.drag_cfg = self.behaviour.get("drag", {})
        self._live_fn = None
        self._live_until = 0.0
        self._live_next = 0.0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.trigger_click_action)

        self.restore_position()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(max(1, round(1000 / FPS)))
        self.tick()

    # ---------------------------------------------------------------- 资源
    def build_assets(self):
        self.assets = PetAssets(self.char_cfg, self.target_char_h, self.dpr)
        self.setFixedSize(QSize(self.assets.window_logical, self.assets.window_logical))
        self.frame = None
        self.apply_mask()

    def reload_assets(self):
        """重新读取 manifest.json 与所有图片：换贴图后无需重启。"""
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log(f"manifest 读取失败，保留当前素材: {exc}")
            return
        self.manifest = manifest
        self.window_cfg = manifest.get("window", self.window_cfg)
        self.char_name = manifest.get("active", self.char_name)
        self.char_cfg = manifest["characters"][self.char_name]
        self.motions = self.char_cfg.get("motions", {})
        self.actions = self.char_cfg.get("actions", {})
        self.build_assets()
        self.tick()
        log("素材已重新加载")

    # ---------------------------------------------------------------- 位置
    def restore_position(self):
        pos = None
        if self.window_cfg.get("remember_position", True) and STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                pos = QPoint(int(data["x"]), int(data["y"]))
            except Exception:  # noqa: BLE001
                pos = None
        if pos is None:
            geo = self.screen().availableGeometry()
            pos = QPoint(geo.right() - self.width() - 40, geo.bottom() - self.height() - 10)
        self.move(self.clamp(pos))

    def clamp(self, pos: QPoint) -> QPoint:
        geo = self.screen().availableGeometry()
        x = min(max(pos.x(), geo.left() - self.width() // 2), geo.right() - self.width() // 2)
        y = min(max(pos.y(), geo.top()), geo.bottom() - self.height() + 1)
        return QPoint(x, y)

    def save_position(self):
        if not self.window_cfg.get("remember_position", True):
            return
        try:
            STATE_FILE.write_text(
                json.dumps({"x": self.x(), "y": self.y(), "char_height": self.target_char_h}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log(f"位置保存失败: {exc}")

    # ---------------------------------------------------------------- 主循环
    def tick(self):
        self.now = time.perf_counter()
        if (self._press_pos is not None and not self.dragging and self._press_time
                and time.monotonic() - self._press_time >= float(
                    self.drag_cfg.get("hold_seconds", 0.45))):
            self._begin_lift()                          # 长按 -> 拎起来
        if self._live_fn:
            mono = time.monotonic()
            if mono >= self._live_until:
                self._live_fn = None
            elif mono >= self._live_next:
                self._live_next = mono + 1.0
                self.show_bubble(self._live_fn(), seconds=max(2.0, self._live_until - mono))
        if self.always_on_top and time.monotonic() >= self._topmost_next:
            self._topmost_next = time.monotonic() + 2.0
            # 对话窗开着时**不要**抢置顶，否则会盖住输入框、吃掉点击（没法打字）
            chatting = self.chat is not None and self.chat.isVisible()
            if not chatting:
                # 聊天时完全不碰任何窗口（对话窗自带 StaysOnTop，系统会管层级）
                # 之前这里每 2 秒操作对话窗，反而把它的焦点与输入法状态搞乱
                self._reassert_topmost()
        if self.now - self._stats_flushed > 600.0:     # 每 10 分钟：写检查点 + 落盘
            self._stats_flushed = self.now
            self.checkpoint()
            self.save_stats()

        # 呼吸：动作可以指定自己的呼吸参数（例如睡着时更慢更深）
        sx, sy, dy = 1.0, 1.0, 0.0
        angle = 0.0
        act_key = "drag" if self.dragging else (self.action_name or "")
        act_now = self.actions.get(act_key, {})
        if self.action_name and act_now.get("motions"):
            for mname in act_now["motions"]:
                mm = self.motions.get(mname) or {}
                if mm.get("kind", "breathe") == "breathe":
                    r = motion_breathe(self.now, mm)
                    sx *= r[0]; sy *= r[1]; dy += r[2]
        elif not self.action_name:
            b = motion_breathe(self.now, self.motions.get("breathe", {}))
            sx *= b[0]; sy *= b[1]; dy += b[2]

        # 拖拽侧倾
        tilt_cfg = self.motions.get("drag_tilt", {})
        smooth = float(tilt_cfg.get("smooth", 0.2))
        if self.dragging:
            self.tilt += (self.tilt_target - self.tilt) * smooth
        elif abs(self.tilt) > 0.05:
            self.tilt *= (1.0 - smooth)
        else:
            self.tilt = 0.0
        if self.tilt:
            r = motion_tilt(self.tilt, tilt_cfg)
            sx *= r[0]; sy *= r[1]; angle += r[2]
        if self.dragging:                       # 被拎在手里时轻轻晃
            dangle = float(tilt_cfg.get("dangle_deg", 2.6))
            period = max(0.2, float(tilt_cfg.get("dangle_period", 1.7)))
            angle += dangle * math.sin(2 * math.pi * self.now / period)

        # 动作计时 / 随机自动互动 / 行走
        if self.action_name and self.now >= self.action_until:
            self.end_action()
        if self.proactive_cfg.get("enabled") and self.now >= self.next_proactive_at:
            self.next_proactive_at = self.now + self.next_proactive_delay()
            if self.can_interact():
                self.do_random_interaction()
        self.update_walk()

        # 眨眼调度
        face_overlay = None
        blink_cfg = self.actions.get("blink", {})
        if self.now >= self.blink_until and self.now >= self.next_blink:
            self.blink_start = self.now
            self.blink_until = self.now + float(blink_cfg.get("duration", 0.16))
            every = blink_cfg.get("every", [3.5, 8.0])
            self.next_blink = self.now + random.uniform(float(every[0]), float(every[1]))
        self._overlay_alpha = 1.0
        if self.now < self.blink_until:
            face_overlay = blink_cfg.get("overlay")
            span = max(0.06, self.blink_until - self.blink_start)
            u = (self.now - self.blink_start) / span
            ramp = float(blink_cfg.get("ramp", 0.35))
            self._overlay_alpha = (max(0.0, min(1.0, min(u, 1.0 - u) / ramp))
                                   if ramp > 0 else 1.0)

        # 点击反应
        if self.now < self.reaction_until:
            if self.reaction_overlay:
                face_overlay = self.reaction_overlay
            p = motion_pop(self.now - self.reaction_start, self.motions.get("click_pop", {}))
            sx *= p[0]; sy *= p[1]; dy += p[2]

        # 身体帧：行走优先，其次才是当前动作的姿势帧（行走帧整体替换底图）
        body = None
        if self.walking:
            frames, fps = self.active_walk_frames()
            if frames:
                body = frames[int(self.now * fps) % len(frames)]
                bob = float(self.roam_cfg.get("bob", 0.0))
                if bob:
                    step_phase = 2 * math.pi * self.now * fps / max(1, len(frames))
                    dy += bob * 0.5 * math.sin(step_phase)
        elif self.dragging and act_now.get("body"):
            body = act_now["body"]              # 被拎起来的姿势
        elif self.action_frames:
            fps = float(act_now.get("fps", 0.0))
            if len(self.action_frames) == 1 or fps <= 0:
                body = self.action_frames[0]
            else:
                idx = int(max(0.0, self.now - self.action_start) * fps)
                if act_now.get("loop", True):
                    body = self.action_frames[idx % len(self.action_frames)]
                else:                       # 不循环：走到最后一帧就停住（睡觉的坐->躺）
                    body = self.action_frames[min(idx, len(self.action_frames) - 1)]

        # 面部部件（眨眼/惊讶）是从**正面**母图上裁的羽化带。走路时身体是侧面帧，
        # 硬叠上去会突然闪出一小块正脸，所以 body 是行走帧时一律不叠面部层。
        # 身体"组"切换时才交叉淡入（组内换帧不能淡，"否则走路会糊成一团）
        group = ("walk" if self.walking
                 else "lift" if (self.dragging and act_now.get("body"))
                 else (self.action_name if self.action_frames else None))
        if group != self._body_group:
            self._fade_from_body = self._last_body_key
            self._fade_start = self.now
            self._body_group = group
        self._last_body_key = body
        fade = float(self.behaviour.get("transition_seconds", 0.15))
        blend = 1.0 if fade <= 0 else min(1.0, (self.now - self._fade_start) / fade)

        # 眨眼：如果当前皮肤自带整帧闭眼图（blink_frame），就用它**整帧替换**。
        # 这样不会像"面部贴带"那样出现换脸/错位问题（贴带来自旧皮肤时尤其明显）。
        if (face_overlay and "blink_frame" in self.assets.parts
                and not self.walking and not self.dragging and not self.action_frames
                and blend >= 1.0):
            body = "blink_frame"
            face_overlay = None
        front_bodies = self.behaviour.get("face_overlay_bodies") or []
        overlays = ([face_overlay] if (face_overlay and blend >= 1.0
                                       and (not body or body in front_bodies)) else [])
        self.frame = self.assets.compose(sx, sy, angle, 0.0, dy, body=body, overlays=overlays,
                                         prev_body=self._fade_from_body, blend=blend,
                                         overlay_alpha=self._overlay_alpha)
        self.update()

    # ---------------------------------------------------------------- 巡游
    def roam_paused(self) -> bool:
        if not self.roam_cfg.get("enabled", False) or self.dragging:
            return True
        if self.now < self.roam_resume_at:
            return True
        chat_visible = self.chat is not None and self.chat.isVisible()
        if chat_visible and self.behaviour.get("chat", {}).get("pause_roam_while_typing", True):
            return True
        return False

    def update_roam(self):
        """随机溜达：挑一个目标点 -> 走过去 -> 停一会儿 -> 再挑下一个。"""
        self.walking = False
        if self.now < self.walk_demo_until:          # 菜单里手动播放的走路演示
            self.walking = True
            return
        if self.roam_paused():
            self.roam_target = None
            return

        geo = self.screen().availableGeometry()
        margin = float(self.roam_cfg.get("edge_margin", 24))
        lo = geo.left() + margin
        hi = geo.right() - self.width() - margin
        if hi <= lo:
            return

        if self.roam_target is None:
            if self.now < self.roam_pause_until:
                return
            if random.random() < float(self.roam_cfg.get("chase_cursor_chance", 0.0)):
                self.roam_target = QCursor.pos().x() - self.width() / 2
            else:
                self.roam_target = random.uniform(lo, hi)
            self.roam_target = max(lo, min(hi, float(self.roam_target)))

        step = float(self.roam_cfg.get("speed_px_s", 70)) / FPS
        delta = self.roam_target - self.x()
        if abs(delta) <= step:
            self.move(int(self.roam_target), self.y())
            self.roam_target = None
            pause = self.roam_cfg.get("pause_s", [4, 13])
            self.roam_pause_until = self.now + random.uniform(float(pause[0]), float(pause[1]))
            self.save_position()
            return
        self.roam_dir = 1 if delta > 0 else -1
        self.move(int(self.x() + math.copysign(step, delta)), self.y())
        self.walking = True

    # ---------------------------------------------------------------- 对话
    def brain_config(self) -> dict:
        if self._brain_cfg is None:
            if brain is None:
                log(f"brain 模块不可用: {_BRAIN_IMPORT_ERROR}")
                self._brain_cfg = {"model": "deepseek-chat"}
            else:
                self._brain_cfg = brain.load_config()
        # 把可触发的动作名交给模型（它会用 [[action:名字]] 使唤她）
        self._brain_cfg["_actions"] = [n for n in self.actions if n not in ("idle", "drag")]
        return self._brain_cfg

    def desktop_info(self) -> dict:
        """给模型的桌面情报。默认只含前台程序名，窗口标题要显式开启。"""
        if brain is None:
            return {}
        try:
            info = brain.desktop_context(self.brain_config())
            # 把自己在屏幕上的位置也给她，才能说清"鼠标在我左边还是右边"
            info["pet"] = [self.x(), self.y(), self.width(), self.height()]
            return info
        except Exception as exc:  # noqa: BLE001
            log(f"桌面情报采集失败: {exc}")
            return {}

    def open_chat(self):
        if brain is None:
            log("brain 模块不可用，无法对话")
            return
        if self.chat is None:
            from chat_dialog import ChatDialog
            self.chat = ChatDialog(self.brain_config(), self.desktop_info,
                                   on_action=self.action_request.emit)
            # 关键：对话窗存在时，Qt 不会再因为桌宠窗口关闭而自动退出程序，
            # 于是旧进程会一直活着占住单实例互斥体 -> 表现为"关了就打不开"。
            self.chat.setAttribute(Qt.WA_QuitOnClose, False)
        self.chat.open_near(self.frameGeometry())
        self.chat.raise_()          # 让对话窗永远在桌宠之上
        self.chat.activateWindow()
        log("打开对话窗")

    def apply_mask(self):
        """透明区域点击穿透。拖拽期间整窗可点——用户正握着鼠标，此时不需要穿透。"""
        if self.dragging:
            self.clearMask()
        else:
            self.setMask(self.assets.hit_region)

    def paintEvent(self, _event):
        if self.frame is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(0, 0, self.frame)

    # ---------------------------------------------------------------- 交互
    def trigger_click_action(self):
        cfg = self.actions.get("click", {})
        dur = float(cfg.get("duration", 0.7))
        self.reaction_start = self.now
        self.reaction_until = self.now + dur
        self.reaction_overlay = cfg.get("overlay")
        self.overlay_until = self.now + float(cfg.get("overlay_duration", dur))

    def trigger_action(self, name: str):
        """菜单点动作的统一入口。

        叠加类动作（眨眼/惊讶）只有一块"面部羽化带"，没有身体帧。走 play_action 时
        它的触发时机容易被自动眨眼调度覆盖，所以这里**直接点亮**那一帧，并拉长到
        至少 0.45 秒（自动眨眼只有 0.17 秒，肉眼看不清）。
        """
        act = self.actions.get(name, {})
        if act.get("overlay") and not act.get("frames") and not act.get("body"):
            span = max(0.45, float(act.get("duration", 0.17)) * 2.6)
            self.blink_start = self.now
            self.blink_until = self.now + span
            every = act.get("every") or [3.5, 8.0]
            self.next_blink = self.now + span + float(every[0])   # 别让自动眨眼把它顶掉
            log(f"叠加动作: {name} 显示 {span:.2f} 秒")
            return
        self.play_action(name)

    def play_action(self, name: str, duration: float | None = None, return_x: float | None = None):
        """统一动作入口：右键菜单、DeepSeek 的 [[action:...]] 指令、随机自动互动都走这里。"""
        if name in ("", "none", "idle"):
            self.end_action()
            return
        if name == "blink":
            self.blink_until = self.now + float(self.actions.get("blink", {}).get("duration", 0.16))
            return
        if name == "click":
            self.trigger_click_action()
            log("动作: click")
            return
        if name == "drag":
            self.tilt_target = float(self.motions.get("drag_tilt", {}).get("max_deg", 10.0)) * 0.8
            QTimer.singleShot(450, lambda: setattr(self, "tilt_target", 0.0))
            log("动作: drag")
            return
        cfg = self.actions.get(name)
        if not isinstance(cfg, dict):
            log(f"未知动作: {name}")
            return
        self.end_action(silent=True)
        if cfg.get("frames_right") or cfg.get("frames_left"):
            dist = cfg.get("distance_px", [150, 350])      # 行走 = 走一段路，不是摆姿势
            self.walk_to(self.x() + random.uniform(float(dist[0]), float(dist[1]))
                         * random.choice([-1, 1]))
            log("动作: walk")
            return
        self.action_name = name
        self.action_start = self.now
        self.action_until = self.now + float(
            duration if duration is not None else cfg.get("duration", 8.0))
        if cfg.get("overlay") and cfg.get("body") is None and not cfg.get("frames"):
            # 叠加类动作（眨眼/惊讶）：手动触发。自动眨眼只有 0.17s，点菜单时看不清，
            # 所以手动触发一律拉长到至少 0.45 秒，并写日志便于排查。
            span = max(0.45, float(cfg.get("duration", 0.16)) * 2.6)
            self.blink_start = self.now
            self.blink_until = self.now + span
            self.next_blink = self.now + span + float(
                (cfg.get("every") or [3.5, 8.0])[0])
            log(f"叠加动作触发: {self.action_name} 显示 {span:.2f} 秒")
        frames = cfg.get("frames_quilt") if (self.quilt_on and cfg.get("frames_quilt")) else cfg.get("frames")
        self.action_frames = list(frames or ([] if not cfg.get("body") else [cfg["body"]]))
        # 只保留四个方向；斜向由 brain 拆成两段（先上下、再左右）
        CORNERS = {"go_up": (0.5, 0.0), "go_down": (0.5, 1.0),
                   "go_left": (0.0, 0.5), "go_right": (1.0, 0.5)}
        if name.startswith(("go_", "cling_")) and getattr(self, "_glide", None) is not None:
            try:
                if self._glide.isActive():
                    self._queue = getattr(self, "_queue", [])
                    self._queue.append(name)            # 走完这一段再做下一段（先上下、再左右）
                    log(f"走位中，排队: {name}（队列 {self._queue}）")
                    return
            except Exception:  # noqa: BLE001
                pass
        if name in CORNERS:
            scr = self.screen().availableGeometry()
            tx, ty = self.x(), self.y()          # 每个方向只动一个轴
            if name == "go_left":
                tx = scr.left()
            elif name == "go_right":
                tx = scr.right() - self.width()
            elif name == "go_up":
                ty = scr.top()
            elif name == "go_down":
                ty = scr.bottom() - self.height()
            self._walk_mode = name if name in ("go_up", "go_down") else None
            self.glide_to(tx, ty)
            log(f"走到{name}: ({tx},{ty}) 模式={self._walk_mode}")
            return
        if name == "daze":
            self.think_and_draw()          # 发呆时：脑子里想什么就画什么
        edge = cfg.get("window_edge")
        if edge:
            # return_x：扒边结束后往哪儿爬回去（从拖拽进入时用拖拽起点，否则用当前点）
            self.cling_return_x = return_x if return_x is not None else self.x()
            self.move_to_edge(edge)
        log(f"动作: {name}（{self.action_until - self.now:.0f} 秒）")

    # ---------------------------------------------------------------- 行走
    def active_walk_frames(self):
        mode = getattr(self, "_walk_mode", None)
        if mode:
            cfg = self.actions.get(mode) or {}
            fr = cfg.get("frames")
            if fr:
                return list(fr), float(cfg.get("fps", 5.0))
        cfg = self.actions.get("walk", {})
        key = "frames_right" if self.roam_dir >= 0 else "frames_left"
        return cfg.get(key) or cfg.get("frames_right") or [], float(cfg.get("fps", 4.5))

    def walk_to(self, x: float):
        # 走路要能把睡觉打断（原来只有 play_action / 拖动会打断）
        if self.action_name and self.action_name != "walk":
            self.end_action(silent=True, no_return=True)
        geo = self.screen().availableGeometry()
        self.target_x = max(geo.left() + 8, min(geo.right() - self.width() - 8, float(x)))

    def update_walk(self):
        # 平滑移动（glide_to）进行中：**不要清掉 walking 标记**，
        # 否则每帧 tick 都会把它置 False，走路动画就播不出来（只剩平移）。
        g = getattr(self, "_glide", None)
        if g is not None and g.isActive():
            return
        self.walking = False
        if self.dragging or self.target_x is None:
            return
        step = float(self.roam_cfg.get("speed_px_s", 70)) / FPS
        delta = self.target_x - self.x()
        self.roam_dir = 1 if delta >= 0 else -1
        if abs(delta) <= step:
            self.move(int(self.target_x), self.y())
            self.target_x = None
            self.save_position()
            return
        self.move(int(self.x() + math.copysign(step, delta)), self.y())
        self.walking = True

    # ---------------------------------------------------------------- 动作状态
    def end_action(self, silent: bool = False, no_return: bool = False):
        if self.action_name and not silent:
            log(f"动作结束: {self.action_name}")
        if self.cling_return_x is not None and not no_return:
            self.walk_to(self.cling_return_x)          # 扒边结束 -> 从屏幕边爬回来
        self.cling_return_x = None
        self.action_name = None
        self.action_until = 0.0
        self.action_frames = []

    def maybe_snap_to_edge(self):
        """拖到屏幕左右两端附近松手时，直接进入扒边模式。"""
        cfg = self.behaviour.get("drag", {})
        snap = float(cfg.get("edge_snap_px", 90))
        if snap <= 0:
            return
        geo = self.screen().availableGeometry()
        left_gap = self.x() - geo.left()
        right_gap = geo.right() - (self.x() + self.width())
        if left_gap <= snap:
            log(f"贴到左边缘（间距 {left_gap}px）-> 进入扒边")
            self.play_action("cling_left", return_x=self._drag_start_x)
        elif right_gap <= snap:
            log(f"贴到右边缘（间距 {right_gap}px）-> 进入扒边")
            self.play_action("cling_right", return_x=self._drag_start_x)

    def move_to_edge(self, edge: str):
        geo = self.screen().availableGeometry()
        x = geo.left() if edge == "left" else geo.right() - self.width() + 1
        self.move(x, self.y())

    # ---------------------------------------------------------------- 随机自动互动
    def next_proactive_delay(self) -> float:
        """基础间隔随机 + 退避：它连续选"什么都不做"，问它的间隔就越拉越长（省 token）。"""
        every = self.proactive_cfg.get("every_minutes", [6, 22])
        base = random.uniform(float(every[0]), float(every[1]))
        factor = float(self.proactive_cfg.get("backoff_factor", 1.6)) ** min(self._proactive_none_streak, 6)
        return max(float(self.proactive_cfg.get("min_minutes", 4)),
                   min(float(self.proactive_cfg.get("max_minutes", 45)), base * factor)) * 60.0

    def load_settings(self) -> dict:
        try:
            if SETTINGS_FILE.exists():
                return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def save_settings(self, data: dict):
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log(f"设置保存失败: {exc}")

    def _reassert_topmost(self):
        """重新置顶，但**不抢输入焦点**。

        之前直接 raise_() 会让她不断盖到对话窗上、把点击吃掉（表现为"没法打字"）。
        改用 SetWindowPos + SWP_NOACTIVATE：只改层级、不夺焦点。
        """
        try:
            import ctypes
            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                                             SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:  # noqa: BLE001
            pass

    def glide_to(self, tx: float, ty: float, steps: int | None = None, interval: int = 16):
        """平滑移动到任意点（**支持上下**）。用于“走到某个角落/某一边”这类指令。"""
        try:
            if getattr(self, "_glide", None) is not None:
                self._glide.stop()
        except Exception:  # noqa: BLE001
            pass
        sx, sy = float(self.x()), float(self.y())
        tx, ty = float(tx), float(ty)
        if steps is None:
            # 按速度走：时长 = 距离 / 速度（远就久、近就短，观感才自然）
            dist = max(abs(tx - sx), abs(ty - sy))
            speed = float(self.behaviour.get("walk_speed_px_s", 180))
            steps = max(18, int(dist / max(30.0, speed) * (1000.0 / interval)))
        state = {"n": 0}

        def step():
            state["n"] += 1
            u = min(1.0, state["n"] / max(1, steps))
            e = u * u * (3 - 2 * u)                     # smoothstep，起步收尾更自然
            nx = sx + (tx - sx) * e
            ny = sy + (ty - sy) * e
            # 用**屏幕可用区域**夹取（拖动那套 clamp 会限制 y，导致"往下去不了"）
            scr2 = self.screen().availableGeometry()
            nx = max(scr2.left(), min(int(nx), scr2.right() - self.width()))
            ny = max(scr2.top(), min(int(ny), scr2.bottom() - self.height()))
            self.move(nx, ny)
            self.walking = True
            self.roam_dir = 1 if tx >= sx else -1
            if u >= 1.0:
                self._glide.stop()
                self.walking = False
                self.save_position()
                q = getattr(self, "_queue", [])
                if q:
                    nxt = q.pop(0)
                    log(f"走位结束，接着执行: {nxt}（剩余 {q}）")
                    QTimer.singleShot(120, lambda x=nxt: self.play_action(x))
                else:
                    self._walk_mode = None

        self._glide = QTimer(self)
        self._glide.timeout.connect(step)
        self._glide.start(interval)

    def set_topmost(self, on: bool):
        """总在最前开关。改窗口标志会让窗口隐藏，所以改完要重新 show + 重建遮罩。"""
        self.always_on_top = bool(on)
        data = self.load_settings()
        data["always_on_top"] = self.always_on_top
        self.save_settings(data)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.always_on_top)
        self.show()
        self.apply_mask()
        if self.always_on_top:
            self.raise_()
        log(f"总在最前: {'开' if self.always_on_top else '关'}")

    def sleep_with(self, quilt: bool):
        """按指定方式睡觉：quilt=True 盖被子，False 只躺在毯子上。"""
        self.set_quilt(quilt)
        if self.action_name != "sleep":
            self.play_action("sleep")

    def skin_defs(self) -> dict:
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8")).get("skins") or {}
        except Exception:  # noqa: BLE001
            return {}

    def set_skin(self, name: str):
        """换衣服：把该皮肤的部件合并进部件表再重载素材。

        合并规则：以 skins.default.parts 为基准，用所选皮肤覆盖同名的键；
        该皮肤没做的帧自动沿用默认皮肤 —— 所以新皮肤可以一帧一帧往上加。
        """
        defs = self.skin_defs()
        if name not in defs:
            log(f"没有这套皮肤: {name}")
            return
        base = (defs.get("default") or {}).get("parts") or {}
        over = (defs.get(name) or {}).get("parts") or {}
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
            data["characters"]["nagato"]["parts"] = {**base, **over}
            layers = data["characters"]["nagato"].setdefault("layers", {})
            layers["base"] = ((defs.get(name) or {}).get("base")
                              or (defs.get("default") or {}).get("base")
                              or layers.get("base"))
            MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log(f"换衣服失败（写 manifest）: {exc}")
            return
        self.skin = name
        data = self.load_settings()
        data["skin"] = name
        self.save_settings(data)
        self.reload_assets()
        log(f"换衣服: {name}（{len(over)} 个专属部件，其余沿用默认皮肤）")

    def set_quilt(self, on: bool):
        """盖被子开关（默认关）。正睡着时立刻换成盖被子的版本。"""
        self.quilt_on = bool(on)
        data = self.load_settings()
        data["quilt"] = self.quilt_on
        self.save_settings(data)
        log(f"盖被子: {'开' if self.quilt_on else '关'}")
        if self.action_name == "sleep":
            self.play_action("sleep")

    def load_pet_state(self) -> dict:
        try:
            if PET_STATE_FILE.exists():
                return json.loads(PET_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def save_pet_state(self):
        try:
            PET_STATE_FILE.write_text(json.dumps(self.pet_state, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log(f"自我状态保存失败: {exc}")

    def note_user_activity(self):
        """主公一动手，就把退避清零，让她重新活络一点。"""
        self._proactive_none_streak = 0
        self.next_proactive_at = min(self.next_proactive_at, self.now + 90.0)

    def can_interact(self) -> bool:
        if self.dragging or self.action_name or self.walking or self.target_x is not None:
            return False
        if self.chat is not None and self.chat.isVisible():
            return False
        return True

    def do_random_interaction(self):
        """自主行动：**由 DeepSeek 自己决定**这一刻做什么。

        本地计时器只负责"叫醒"（零 token）；决定、台词、心情全部交给模型。
        proactive.mode 设成 "random" 可退回旧的纯随机行为（不调模型）。
        """
        if brain is None or self.proactive_cfg.get("mode", "model") == "random":
            if random.random() < float(self.proactive_cfg.get("speak_chance", 0.5)):
                self.speak_spontaneously()
            else:
                options = self.proactive_cfg.get("actions") or []
                if options:
                    self.play_action(random.choice(options))
            return

        cfg = self.brain_config()
        info = self.desktop_info()
        state = dict(self.pet_state)

        def work():
            try:
                decision = brain.decide(cfg, info, state)
                self.decision_ready.emit(json.dumps(decision, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                log(f"自主决定失败: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def show_decision(self, payload: str):
        """在主线程应用模型的决定：none=什么都不做 / say=说一句 / act[_say]=做动作。"""
        try:
            d = json.loads(payload)
        except Exception:  # noqa: BLE001
            return
        mood = str(d.get("mood") or "").strip()
        if mood:
            self.pet_state["mood"] = mood
        do = str(d.get("do") or "none").lower()
        say = str(d.get("say") or "").strip()
        act = str(d.get("action") or "").strip()
        if do == "none":
            self._proactive_none_streak += 1
            log(f"自主：不动（心境：{mood or '-'}）")
        else:
            self._proactive_none_streak = 0
            self.pet_state["last_do"] = do
            if act and act in self.actions:
                self.play_action(act)
                self.pet_state["last_action"] = act
            if say:
                self.show_bubble(say)
                self.pet_state["last_say"] = say
        draw = str(d.get("draw") or "").strip()
        if draw:
            self.think_and_draw(say, draw)     # 她主动画一张给你看
            log(f"自主：{do}  动作={act or '-'}  台词={say[:24]}")
        self.save_pet_state()
        self.next_proactive_at = self.now + self.next_proactive_delay()

    # ---------------------------------------------------------------- 陪伴时长与余额
    def load_stats(self) -> dict:
        data = {"total_seconds": 0.0}
        try:
            if STATS_FILE.exists():
                data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        # 会话滚动：把上次没结算的会话时长补进累计（程序被强杀也不会丢）
        prev = data.pop("session_start", None)
        if prev:
            data["total_seconds"] = float(data.get("total_seconds", 0.0)) + max(
                0.0, time.time() - float(prev))
        data["session_start"] = time.time()
        if float(data.get("total_seconds", 0.0)) <= 0.0:
            fallback = self.recover_from_log()          # 兜底：从检查点找回
            if fallback > 0:
                data["total_seconds"] = fallback
                log(f"累计时长从检查点恢复: {fallback:.0f} 秒")
        return data

    def save_stats(self):
        """落盘。注意：**不能**把当前会话加进 total_seconds —— 否则每次落盘都重复累加。

        total_seconds 只装"已完成会话"的时长；本次会话靠 session_start 滚动结算。
        """
        try:
            self.stats["session_start"] = self.launch_time
            self.stats["last_exit"] = time.strftime("%Y-%m-%d %H:%M")
            STATS_FILE.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log(f"陪伴统计保存失败: {exc}")

    def checkpoint(self):
        """把累计陪伴时长**追加**一行到 companion.log。

        只追加、不覆盖：即使将来 stats.json 被写坏或清零，也能从这里找回大致时间。
        """
        try:
            total = self.total_companion_seconds()
            with open(COMPANION_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t累计={int(total)}秒\t"
                         f"本次={int(self.session_seconds())}秒\t{self.human_duration(total)}\n")
        except Exception as exc:  # noqa: BLE001
            log(f"陪伴检查点写入失败: {exc}")

    def recover_from_log(self) -> float:
        """stats.json 里的累计若丢了，从 companion.log 最后一行兜底恢复。"""
        try:
            if COMPANION_LOG.exists():
                for ln in reversed(COMPANION_LOG.read_text(encoding="utf-8").split("\n")):
                    m = re.search(r"累计=(\d+)秒", ln)
                    if m:
                        return float(m.group(1))
        except Exception as exc:  # noqa: BLE001
            log(f"陪伴检查点读取失败: {exc}")
        return 0.0

    def session_seconds(self) -> float:
        """本次打开程序以来的陪伴时长。"""
        return max(0.0, time.time() - self.launch_time)

    def total_companion_seconds(self) -> float:
        return float(self.stats.get("total_seconds", 0.0)) + self.session_seconds()

    @staticmethod
    def human_duration(seconds: float) -> str:
        seconds = int(seconds)
        day, rem = divmod(seconds, 86400)
        hour, rem = divmod(rem, 3600)
        minute, sec = divmod(rem, 60)
        if day:
            return f"{day} 天 {hour} 小时 {minute} 分"
        if hour:
            return f"{hour} 小时 {minute} 分"
        if minute:
            return f"{minute} 分 {sec} 秒"
        return f"{sec} 秒"

    def show_live(self, fn, seconds: float = 25.0):
        """每秒钟刷新一次文字的气泡（用于实时计时）。"""
        self._live_fn = fn
        self._live_until = time.monotonic() + seconds
        self._live_next = 0.0
        self.show_bubble(fn(), seconds=seconds)

    def companionship_text(self) -> str:
        return (f"主公，此番吾已伴你 {self.human_duration(self.session_seconds())}；"
                f"累计 {self.human_duration(self.total_companion_seconds())}。")

    def show_companionship(self):
        self.show_live(self.companionship_text, seconds=25)
        log(f"陪伴时长: {self.companionship_text()}")

    def refresh_ark_balance(self):
        """查火山方舟（豆包）余额：计费 OpenAPI + AK/SK 签名，与 DeepSeek 独立。"""
        if brain is None:
            self.show_bubble("吾此刻连不上方舟的库房。", seconds=12)
            return
        cfg = self.brain_config()

        def work():
            try:
                b = brain.fetch_ark_balance(cfg)
            except Exception as exc:  # noqa: BLE001
                log(f"豆包余额查询失败: {exc}")
                self.info_text.emit(f"方舟库房探不着：{exc}")
                return
            self.info_text.emit(
                f"主公，火山方舟尚有 {b.get('available')} 元可用"
                f"（现金 {b.get('cash')}，赠金 {b.get('granted') or '0'}）。")

        threading.Thread(target=work, daemon=True).start()

    def refresh_balance(self):
        """查 DeepSeek 余额，用气泡报出来（省得开浏览器）。"""
        if brain is None:
            self.show_bubble("吾此刻连不上主公的库房。", seconds=12)
            return
        cfg = self.brain_config()

        def work():
            try:
                b = brain.fetch_balance(cfg)
            except Exception as exc:  # noqa: BLE001
                log(f"余额查询失败: {exc}")
                self.info_text.emit("库房一时探不着，主公稍后再问吾。")
                return
            self._balance = b
            if not b.get("available", True):
                self.info_text.emit("主公，库房已空，需速速补给了。")
                return
            cur = b.get("currency", "CNY")
            symbol = {"CNY": "¥", "RMB": "¥", "USD": "$"}.get(cur, "")
            self.info_text.emit(
                f"主公，库房尚有 {symbol}{b.get('total', '?')} {cur} 可用"
                f"（赠金 {b.get('granted') or '0'}，充值 {b.get('topped') or '0'}）。")

        threading.Thread(target=work, daemon=True).start()

    def maybe_react(self, chance: float = 0.35):
        """主公刚跟她互动过（点一下 / 拖一下）→ 有概率主动回应。

        刻意不打断正在进行的动作（走路/睡觉/长动作时跳过），只做轻量反应。
        """
        try:
            if random.random() > chance:
                return
            busy = (self.dragging or self.walking
                    or (self.action_name in ("sleep", "drag")) or bool(self.action_frames))
            if busy:
                return
            if random.random() < 0.6:
                self.speak_spontaneously()          # 后台线程，不阻塞
                log("交互反应: 主动说一句")
            else:
                self.play_action("blink")           # 温和的即时反应
                log("交互反应: 眨眼")
        except Exception as exc:  # noqa: BLE001
            log(f"交互反应失败: {exc}")

    def look_now(self):
        """截当前屏幕交给方舟视觉模型，然后用气泡说出她看到了什么（后台线程，不卡界面）。"""
        if brain is None:
            return
        if getattr(self, "_looking", False):
            return
        self._looking = True
        self.bubble_text.emit("……吾看看。")
        cfg = self.brain_config()

        def work():
            try:
                text = brain.look_screen(cfg) or ""
            except Exception as exc:  # noqa: BLE001
                text = ""
                log(f"识图异常: {exc}")
            if text:
                log(f"识图结果: {text[:80]}")
                self.bubble_text.emit("（抬眼看）" + text)
            else:
                self.bubble_text.emit("（眯起眼）……看不太清，主公再让吾看一次？")
            self._looking = False

        threading.Thread(target=work, daemon=True).start()

    def speak_spontaneously(self):
        """让模型结合桌面情报主动说一句，用气泡显示（不走对话窗）。"""
        if brain is None:
            return
        cfg = self.brain_config()
        info = self.desktop_info()

        def work():
            try:
                line = brain.spontaneous_line(cfg, info)
                if line:
                    self.bubble_text.emit(line)
            except Exception as exc:  # noqa: BLE001
                log(f"主动搭话失败: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def review_own_image(self, path: str):
        """画完图后：让豆包看一眼这张画，再让 chat 用自己的口吻转述给主公。

        （视觉模型负责"看懂"，deepseek-chat 负责"说人话"——这就是主公要的中转）
        """
        if brain is None:
            return
        cfg = self.brain_config()

        def work():
            try:
                desc = brain.describe_image(cfg, path) or ""
            except Exception as exc:  # noqa: BLE001
                log(f"看自己画的图失败: {exc}")
                return
            if not desc:
                return
            log(f"她画的内容(豆包识图): {desc[:80]}")
            say = ""
            try:
                say = brain.chat_stream(
                    [{"role": "user", "content": (
                        "你刚画完一张画。豆包看图后说：" + desc +
                        "。请用你的口吻、第一人称，跟主公说一句话描述你画了什么（不要提豆包）。")}],
                    cfg, lambda _t: None) or ""
            except Exception as exc:  # noqa: BLE001
                log(f"转述失败: {exc}")
            self.bubble_text.emit((say.strip() or ("（看了看画）" + desc))[:160])

        threading.Thread(target=work, daemon=True).start()

    def show_thought_image(self, path: str, caption: str):
        """头顶显示"想法气泡"：她在想的东西 / 主动给你画的小图。"""
        try:
            if self.img_bubble is None:
                from chat_dialog import ImageBubble
                self.img_bubble = ImageBubble()
            if not self.img_bubble.show_image(path, caption, self.frameGeometry(), 14.0):
                self.show_bubble("（想画点什么，却没画成…）", seconds=6)
        except Exception as exc:  # noqa: BLE001
            log(f"想法气泡显示失败: {exc}")
        finally:
            self._thinking = False

    def think_and_draw(self, caption: str = "", draw_prompt: str = ""):
        """后台：自己想一个念头（或直接用给定提示词）-> 免费生图 -> 交给主线程显示。"""
        if self._thinking or brain is None:
            log(f"生图跳过（thinking={self._thinking}, brain={brain is not None}）")
            return
        self._thinking = True
        log("想法：开始构思…")
        cfg = self.brain_config()
        info = self.desktop_info()

        def work():
            try:
                if not draw_prompt:
                    d = brain.daze_thought(cfg, info)
                    caption_ = str(d.get("think") or "").strip()
                    prompt = str(d.get("draw") or "").strip()
                else:
                    caption_, prompt = caption, draw_prompt
                if caption_:
                    log(f"想法：{caption_}")
                    self.bubble_text.emit(caption_)      # 先立刻冒出一句话
                if not prompt:
                    log("想法：没有生图提示词，跳过生图")
                    self._thinking = False
                    return
                log("想法：开始生图…")
                path = brain.draw_image(prompt, "thought")
                if path:
                    log("想法：图已生成，显示气泡")
                    self.image_ready.emit(path, caption_)

                else:
                    log("想法：生图返回空")
                    self._thinking = False
            except Exception as exc:  # noqa: BLE001
                log(f"生图失败: {exc}")
                self._thinking = False

        threading.Thread(target=work, daemon=True).start()

    def show_bubble(self, text: str, seconds: float | None = None):
        if self.bubble is None:
            from chat_dialog import SpeechBubble
            self.bubble = SpeechBubble()
        self.bubble.show_at(text, self.frameGeometry(),
                            float(seconds or self.proactive_cfg.get("speech_seconds", 7)))
        log(f"主动搭话: {text[:40]}")

    def _begin_lift(self):
        """真正开始"拎起来"：进入拖动状态，显示出被拎起的姿势。"""
        self.dragging = True
        self.tilt_target = 0.0
        self.vel_x = 0.0
        self.last_mouse_x = QCursor.pos().x()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = False          # 先不进入拖动：见 tick 里的长按判定
            self._press_time = time.monotonic()
            self.tilt_target = 0.0
            self.vel_x = 0.0
            self.last_mouse_x = event.globalPosition().x()
            self.note_user_activity()
            self._press_pos = event.globalPosition().toPoint()
            self._drag_start_x = self.x()
            self._cling_at_press = self.action_name if (self.action_name or "").startswith("cling") else None
            self.drag_offset = self._press_pos - self.frameGeometry().topLeft()
            self.roam_resume_at = self.now + float(self.roam_cfg.get("resume_after_drag_s", 6.0))
            self.roam_target = None
            self.clearMask()
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if not self.dragging and self._press_pos is not None:
            moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if moved > float(self.drag_cfg.get("start_px", 14)):
                self._begin_lift()                      # 快速拖动 -> 也算拎起来
        if not self.dragging:
            event.ignore()
            return
        gp = event.globalPosition()
        # 真正拖走时才退出扒边（纯点击不算，免得点一下就掉下来）
        if self._cling_at_press and (gp.toPoint() - self._press_pos).manhattanLength() > 6:
            self.end_action(silent=True, no_return=True)
            self._cling_at_press = None
        self.move(self.clamp(gp.toPoint() - self.drag_offset))
        self.vel_x = 0.6 * self.vel_x + 0.4 * (gp.x() - self.last_mouse_x)
        self.last_mouse_x = gp.x()
        max_deg = float(self.motions.get("drag_tilt", {}).get("max_deg", 10.0))
        self.tilt_target = max(-max_deg, min(max_deg, self.vel_x * 0.45))
        event.accept()

    def mouseReleaseEvent(self, event):
        self._press_time = 0.0
        if self.dragging or self._press_pos is not None:
            moved = 0
            if self._press_pos is not None:
                moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self.dragging = False
            self.tilt_target = 0.0
            self._press_pos = None
            self.save_position()
            self.apply_mask()
            # 几乎没移动 = 单击：延迟一点再反应，好让双击有机会取消它
            reaction_on = self.behaviour.get("chat", {}).get("reaction_on", "single_click")
            if moved < 6 and reaction_on == "single_click":
                self._click_timer.start(240)
            else:
                self.maybe_snap_to_edge()          # 拖到屏幕两端松手 -> 直接扒边
            self._cling_at_press = None
            QTimer.singleShot(320, self.maybe_react)     # 互动后有概率主动回应
            event.accept()
        else:
            event.ignore()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._click_timer.stop()
            self.open_chat()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#2b2b31;color:#eee;border:1px solid #4a4a55;padding:4px;}"
            "QMenu::item{padding:5px 22px 5px 14px;border-radius:4px;}"
            "QMenu::item:selected{background:#5a4a7a;}"
        )
        menu.addAction("对话…", self.open_chat)
        menu.addAction("查询余额（DeepSeek）", self.refresh_balance)
        menu.addAction("查询余额（豆包·方舟）", self.refresh_ark_balance)
        menu.addAction("让她看看屏幕", self.look_now)
        menu.addAction("陪伴时间", self.show_companionship)
        top = menu.addAction("总在最前")
        top.setCheckable(True)
        top.setChecked(self.always_on_top)
        top.toggled.connect(self.set_topmost)
        wear = menu.addMenu("换衣服")
        for key, sdef in (self.skin_defs() or {"default": {"label": "和服（默认）"}}).items():
            item = wear.addAction(str(sdef.get("label") or key))
            item.setCheckable(True)
            item.setChecked(key == self.skin)
            item.triggered.connect(lambda _=False, k=key: self.set_skin(k))
        sleep_menu = menu.addMenu("睡觉")
        sleep_menu.addAction("睡觉（不盖被子）", lambda: self.sleep_with(False))
        sleep_menu.addAction("睡觉（盖被子）", lambda: self.sleep_with(True))
        menu.addSeparator()
        act = menu.addMenu("播放动作")
        labels = {"walk": "走动", "blink": "眨眼", "click": "戳一下", "daze": "发呆",
                  "drag": "拎起来", "cling_left": "扒左边",
                  "cling_right": "扒右边", "sleep": "睡觉", "idle": "发呆"}
        act.addAction = act.addAction
        for name in (n for n in self.actions
                     if n not in ("sleep", "click", "drag", "idle")):
            CN_DIR = {"go_up": "往上走", "go_down": "往下走",
                      "go_left": "往左走", "go_right": "往右走"}
            act.addAction(CN_DIR.get(name) or labels.get(name, name),
                          lambda n=name: self.trigger_action(n))
        size_menu = menu.addMenu("大小")
        for h in self.presets:
            label = f"{int(h)} px" + ("（当前）" if abs(h - self.target_char_h) < 1 else "")
            size_menu.addAction(label, lambda h=h: self.set_size(h))
        menu.addSeparator()
        menu.addAction("重新加载素材", self.reload_assets)
        menu.addSeparator()
        menu.addAction("退出", self.close)
        menu.exec(event.globalPos())

    def set_size(self, height: float):
        self.target_char_h = float(height)
        self.build_assets()
        self.move(self.clamp(self.pos()))
        self.tick()
        self.save_position()

    def closeEvent(self, event):
        self.save_position()
        self._click_timer.stop()
        self.timer.stop()
        if self.chat is not None:
            self.chat.hide()
            self.chat.deleteLater()
            self.chat = None
        if self.bubble is not None:
            self.bubble.hide()
            self.bubble.deleteLater()
            self.bubble = None
        super().closeEvent(event)
        # 显式退出：只要存在过第二个顶层窗口，Qt 就不会自己退，进程会赖着不走
        QApplication.quit()


# --------------------------------------------------------------------------- 自测
def selftest(outdir: Path):
    """把几个代表性状态渲染成图，用来在没有窗口的情况下检查渲染正确性。"""
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    char = manifest["characters"][manifest.get("active", "nagato")]
    dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    assets = PetAssets(char, float(manifest["window"]["target_character_logical_height"]), dpr)

    card = assets.device + 40
    states = [
        ("idle_t0", 1.0, 1.0, 0.0, 0.0, None, None),
        ("idle_inhale", 1.0 - 0.55 * 0.014, 1.0 + 0.014, 0.0, 0.0, None, None),
        ("idle_exhale", 1.0 + 0.55 * 0.014, 1.0 - 0.014, 0.0, 0.0, None, None),
        ("blink", 1.0, 1.0, 0.0, 0.0, None, "eyes_closed"),
        ("click_pop", 1.0 - 0.5 * 0.075, 1.0 + 0.075, 0.0, 0.0, None, "face_surprised"),
        ("drag_tilt", 1.0 - 0.5 * 0.035, 1.0 + 0.035, 10.0, 0.0, None, None),
        ("walk_r1", 1.0, 1.0, 0.0, 0.014, "walk_r1", None),
        ("walk_r2", 1.0, 1.0, 0.0, -0.006, "walk_r2", None),
        ("walk_l1", 1.0, 1.0, 0.0, 0.014, "walk_l1", None),
        ("walk_l2", 1.0, 1.0, 0.0, -0.006, "walk_l2", None),
    ]
    sheet = QImage(card * len(states), card, QImage.Format_ARGB32)
    sheet.fill(QColor(245, 245, 248))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for i, (name, sx, sy, ang, dy, body, ov) in enumerate(states):
        x0 = i * card
        for gy in range(0, card, 16):
            for gx in range(0, card, 16):
                if ((gx // 16) + (gy // 16)) % 2 == 0:
                    p.fillRect(x0 + gx, gy, 16, 16, QColor(226, 226, 232))
        frame = assets.compose(sx, sy, ang, 0.0, dy, body=body,
                               overlays=[ov] if ov else [])
        p.drawImage(x0 + 20, 20, frame)
        p.setPen(QColor(40, 40, 40))
        p.drawText(QRect(x0, card - 22, card, 20), Qt.AlignCenter, name)
        frame.save(str(outdir / f"{name}.png"))
    p.end()
    path = outdir / "selftest_sheet.png"
    sheet.save(str(path))
    print(f"[selftest] 角色显示尺寸: 逻辑 {assets.window_logical}px / 设备 {assets.device}px (dpr={dpr})")
    print(f"[selftest] 输出: {path}")


def main():
    app = QApplication(sys.argv)
    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        outdir = Path(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else ROOT / "assets" / "preview" / "selftest"
        selftest(outdir)
        return 0
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if "--probe" in sys.argv:
        return probe()
    guard = acquire_single_instance()
    if guard is None:
        log("已有实例在运行，本次启动退出（如需强制重开：先在任务管理器结束所有 pythonw.exe）")
        log("已有实例在运行，本次启动静默退出（保留当前实例）")
        return 0
    win = PetWindow(manifest)
    win.show()
    log(f"{win.char_name} 已启动：窗口 {win.width()}x{win.height()} 逻辑像素，"
        f"素材 {win.assets.device}px，位置 ({win.x()}, {win.y()})")
    return app.exec()


def probe():
    """显示窗口并打印真实状态，用来判断"进程活着但看不见"这类问题。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    win = PetWindow(manifest)
    win.show()

    def report():
        scr = win.screen()
        mask = win.mask()
        log(
            "probe: visible={v} geom={g} winId={w} mask_empty={m} frame={f} "
            "screen={s} dpr={d} on_top={t}".format(
                v=win.isVisible(), g=win.geometry().getRect(), w=int(win.winId()),
                m=mask.isEmpty(), f=win.frame is not None,
                s=scr.geometry().getRect(), d=scr.devicePixelRatio(),
                t=bool(win.windowFlags() & Qt.WindowStaysOnTopHint),
            )
        )
        QApplication.quit()

    QTimer.singleShot(1500, report)
    return QApplication.instance().exec()


if __name__ == "__main__":
    raise SystemExit(main())
