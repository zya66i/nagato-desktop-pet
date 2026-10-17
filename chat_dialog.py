"""长门的对话框：无边框、置顶、跟随桌宠的简易聊天窗。

线程模型
--------
DeepSeek 的流式请求跑在后台线程里，逐段文本通过 Qt 信号回到主线程刷新 UI
（Qt 信号跨线程发射是安全的，接收端在主线程事件循环里排队处理）。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

import brain

QSS = """
#panel { background: rgba(28, 24, 34, 238); border: 1px solid #b9975b; border-radius: 10px; }
#title { color: #f0d9a8; font-size: 13px; font-weight: bold; }
#hint  { color: #8d8496; font-size: 11px; }
QTextBrowser { background: transparent; border: none; color: #ece7f2; font-size: 13px; }
QLineEdit {
    background: rgba(255,255,255,0.07); border: 1px solid #5d5470; border-radius: 6px;
    padding: 6px 8px; color: #f5f2fa; font-size: 13px;
}
QLineEdit:focus { border: 1px solid #b9975b; }
QPushButton {
    background: #7a5c2e; border: none; border-radius: 6px; color: #ffeccb;
    padding: 6px 14px; font-size: 12px;
}
QPushButton:hover { background: #96703a; }
QPushButton:disabled { background: #4a4155; color: #9a92a5; }
#close { background: transparent; color: #b9b0c4; font-size: 14px; padding: 2px 8px; }
#close:hover { color: #ff9d9d; }
"""


class ImeLineEdit(QLineEdit):
    """修无边框窗口下"输入法候选框飘到屏幕左上角"。

    Qt 默认把光标矩形按控件坐标上报，无边框/自绘窗口在部分输入法下会被算成 (0,0)。
    这里显式换算成屏幕坐标返回，输入法就能把候选框贴到光标处。
    """

    def inputMethodQuery(self, query, argument=None):
        from PySide6.QtCore import QRect
        try:
            val = (super().inputMethodQuery(query) if argument is None
                   else super().inputMethodQuery(query, argument))
        except TypeError:
            val = super().inputMethodQuery(query)
        try:
            if query == Qt.ImCursorRectangle and val is not None:
                r = val if isinstance(val, QRect) else QRect(val)
                return QRect(self.mapToGlobal(r.topLeft()), r.size())
        except Exception:  # noqa: BLE001
            pass
        return val


class ChatDialog(QWidget):
    """简单的对话窗：历史 + 输入框。所有推理交给 brain.py。"""

    delta = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, cfg: dict, context_provider, parent=None, on_action=None):
        super().__init__(parent)
        self.cfg = cfg
        self.context_provider = context_provider      # 取桌面情报的回调
        self.on_action = on_action                    # 模型要求做动作时的回调
        self.history: list[dict] = []
        self.streaming = ""
        self.busy = False

        self.setWindowFlags(
            Qt.Window | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("长门 · 对话")
        self.setStyleSheet(QSS)
        self.resize(420, 340)

        panel = QWidget(self)
        panel.setObjectName("panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel("长门")
        self.title.setObjectName("title")
        self.hint = QLabel("· 重樱旗舰")
        self.hint.setObjectName("hint")
        head.addWidget(self.title)
        head.addWidget(self.hint)
        head.addStretch(1)
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("close")
        self.close_btn.clicked.connect(self.hide)
        head.addWidget(self.close_btn)
        lay.addLayout(head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        lay.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.input = QLineEdit()        # 回到原生输入框：稳定优先，不做自定义光标上报
        self.input.setPlaceholderText("对长门说点什么…（回车发送，Esc 关闭）")
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton("呈上")
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self.delta.connect(self._on_delta)
        self.finished.connect(self._on_finished)
        self.failed.connect(self._on_failed)

        self._drag_from: QPoint | None = None
        self._summarized_upto = 0          # 已总结到第几条，避免重复记忆
        self.render()

    # ------------------------------------------------------------------ 渲染
    def render(self):
        parts = []
        for msg in self.history:
            if msg["role"] == "user":
                parts.append(
                    f'<p style="margin:6px 0 2px 0;color:#9fd0ff;"><b>主公</b> '
                    f'{self._esc(msg["content"])}</p>')
            else:
                parts.append(
                    f'<p style="margin:2px 0 6px 0;color:#f3e6c8;"><b>长门</b> '
                    f'{self._esc(msg["content"])}</p>')
        if self.busy:
            text = self.streaming or "……"
            parts.append(
                f'<p style="margin:2px 0 6px 0;color:#f3e6c8;"><b>长门</b> '
                f'{self._esc(text)}<span style="color:#b9975b;">▍</span></p>')
        elif not parts:
            parts.append('<p style="color:#8d8496;">（她静静看着你，等你开口。）</p>')
        self.view.setHtml("".join(parts))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    @staticmethod
    def _esc(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))

    def _reassert_topmost(self):
        """非激活方式保持在最前：不抢输入焦点，但始终盖在桌宠之上。"""
        try:
            import ctypes
            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                                             SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:  # noqa: BLE001
            pass

    def _force_foreground(self):
        """强制把对话窗提到前台并拿键盘（Win32 SetForegroundWindow 兜底）。"""
        try:
            self.raise_()
            self.activateWindow()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.input.setFocus()
        except Exception:  # noqa: BLE001
            pass

    def focusOutEvent(self, event):
        # 故意不做任何事：**点别处不应该把对话窗关掉**（原来会，体验很差）
        event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        try:
            self.input.setEnabled(True)
            self.send_btn.setEnabled(True)
            self.busy = False
            QTimer.singleShot(80, self._force_foreground)          # 顺带清掉可能残留的"发送中"状态
            self.input.setFocus()
        except Exception:  # noqa: BLE001
            pass

    def hideEvent(self, event):
        """对话窗关掉 = 这次对话结束 -> 让她自己总结并记进长期记忆。"""
        self.remember_conversation()
        super().hideEvent(event)

    def remember_conversation(self):
        if self.busy or len(self.history) < 2 or len(self.history) == self._summarized_upto:
            return
        self._summarized_upto = len(self.history)
        snapshot = list(self.history)

        def work():
            try:
                brain.remember_conversation(self.cfg, snapshot)
            except Exception as exc:  # noqa: BLE001
                print(f"[chat] 记忆失败: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def set_busy(self, busy: bool):
        self.busy = busy
        # 输入框**永远保持可编辑**：发送中的重复回车由 self.busy 判断挡住即可。
        # （之前这里 setEnabled(not busy)，一旦某次流式请求异常没结束，输入框就永久变灰）
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        if not busy:
            self.input.setFocus()

    # ------------------------------------------------------------------ 交互
    def send(self):
        text = self.input.text().strip()
        if not text or self.busy:
            return
        self.input.clear()
        self.history.append({"role": "user", "content": text})
        self.streaming = ""
        self.set_busy(True)
        self.render()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            info = self.context_provider() if self.context_provider else None
            reply = brain.chat_stream(self.history, self.cfg,
                                      lambda piece: self.delta.emit(piece), info,
                                      on_action=self.on_action)
            self.finished.emit(reply)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))

    def _on_delta(self, piece: str):
        self.streaming += piece
        self.render()

    def _on_finished(self, reply: str):
        if reply:
            self.history.append({"role": "assistant", "content": reply})
        self.streaming = ""
        self.set_busy(False)
        self.render()

    def _on_failed(self, message: str):
        self.history.append({"role": "assistant", "content": f"（吾这边出了些岔子：{message}）"})
        self.streaming = ""
        self.set_busy(False)
        self.render()

    def open_near(self, pet_rect: QRect):
        """贴着桌宠弹出，优先放右侧，放不下就放左侧，并保证不越出屏幕。"""
        scr = self.screen().availableGeometry()
        x = pet_rect.right() + 12
        if x + self.width() > scr.right():
            x = pet_rect.left() - self.width() - 12
        x = max(scr.left() + 8, min(x, scr.right() - self.width() - 8))
        y = pet_rect.top() + pet_rect.height() // 2 - self.height() // 2
        y = max(scr.top() + 8, min(y, scr.bottom() - self.height() - 8))
        self.move(QPoint(x, y))
        self.show()
        self.raise_()
        self.input.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # 无边框窗口自己实现拖动（拖标题栏区域）
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 40:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_from = None
        event.accept()


class SpeechBubble(QWidget):
    """桌宠头顶的气泡：主动搭话时用，不抢焦点、几秒后自动消失。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(320)
        self.label.setStyleSheet(
            "background: rgba(28,24,34,240); color:#f3e6c8; border:1px solid #b9975b;"
            "border-radius:9px; padding:8px 11px; font-size:12px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_at(self, text: str, anchor: QRect, seconds: float = 7.0):
        self.label.setText(text)
        self.adjustSize()
        scr = self.screen().availableGeometry()
        x = anchor.center().x() - self.width() // 2
        y = anchor.top() - self.height() - 8
        x = max(scr.left() + 6, min(x, scr.right() - self.width() - 6))
        if y < scr.top() + 6:
            y = anchor.bottom() + 8
        self.move(QPoint(x, y))
        self.show()
        self.raise_()
        self._timer.start(int(max(2.0, seconds) * 1000))


class ImageBubble(QWidget):
    """头顶的"想法气泡"：显示一张小图 + 一句旁白（发呆时脑子里想的东西 / 主动互动时画的图）。"""

    # 图片区 128px（不到长门身高 267 的一半）：像素图小尺寸观感才好看
    IMG_MAX = 128
    W, H, PAD = IMG_MAX + 16, IMG_MAX + 34, 8

    def __init__(self):
        super().__init__()
        from PySide6.QtCore import Qt, QTimer, QPoint
        from PySide6.QtGui import QPixmap, QPainter, QColor, QPainterPath, QFont
        self._Qt = Qt
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._pix = None
        self._caption = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_image(self, path: str, caption: str, anchor, seconds: float = 9.0):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QPixmap
        px = QPixmap(path)
        if px.isNull():
            return False
        inner = self.IMG_MAX
        # 像素风：用最近邻缩放，保持硬边（平滑缩放会把像素糊掉）
        self._pix = px.scaled(inner, inner, self._Qt.KeepAspectRatio,
                              self._Qt.FastTransformation)
        self._caption = caption or ""
        self.setFixedSize(self.W, self.H)
        scr = self.screen().availableGeometry()
        x = anchor.center().x() - self.W // 2
        y = anchor.top() - self.H - 6
        x = max(scr.left() + 6, min(x, scr.right() - self.W - 6))
        if y < scr.top() + 6:
            y = anchor.bottom() + 6
        self.move(QPoint(x, y))
        self.show()
        self.raise_()
        self._timer.start(int(max(3.0, seconds) * 1000))
        return True

    def paintEvent(self, event):
        from PySide6.QtCore import Qt, QRect
        from PySide6.QtGui import QPainter, QColor, QPainterPath, QFont
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 14, 14)
        p.fillPath(path, QColor(28, 28, 34, 232))
        p.setPen(QColor(150, 150, 170, 220))
        p.drawPath(path)
        if self._pix is not None:
            p.drawPixmap((self.width() - self._pix.width()) // 2,
                         self.PAD, self._pix)
        if self._caption:
            f = QFont()
            f.setPointSizeF(8.5)
            p.setFont(f)
            p.setPen(QColor(235, 235, 245))
            rect = QRect(self.PAD, self.height() - 26, self.width() - self.PAD * 2, 20)
            p.drawText(rect, Qt.AlignHCenter | Qt.AlignVCenter, self._caption[:22])
        p.end()
