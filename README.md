# 长门桌宠（Nagato Desktop Pet）

一只会走动、会扒边、会发呆、还会跟你聊天的 Q 版桌宠。基于 **PySide6** + **DeepSeek**。

## 特性

- 桌面常驻、总在最前、可拖拽/拎起、扒屏幕边缘、走路（四方向）、睡觉、发呆、眨眼
- 由大模型驱动的性格与对话（可自定义人设 `persona.txt`）
- 长期记忆（每次对话自动总结）、陪伴时长统计
- 发呆时会自己想念头，并用免费生图把念头画成像素小图顶在头上
- **可选**：接火山方舟视觉模型后，可以在对话里说“看看屏幕”，她真的看你的屏幕并回答

## 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS/Linux（注意：窗口相关功能依赖 Win32 API，仅 Windows 完整可用）
.venv/bin/pip install -r requirements.txt
```

## 配置密钥（不要提交到仓库）

```bash
# Windows PowerShell
setx DEEPSEEK_API_KEY "<你的key>"
# 可选：视觉能力（火山方舟）
setx ARK_API_KEY "你的方舟key"
```

也可以复制 `config.json` 为 `config.local.json` 并在里面直接填 `api_key`（该文件已在 `.gitignore` 里）。

模型可在 `config.json` 调整：`model`（对话）、`vision_model`（识图）。

## 运行

```bash
.venv\Scripts\pythonw.exe pet.py      # Windows 无控制台
.venv\Scripts\python.exe pet.py       # 有控制台，方便看日志
```

## 目录

| 路径 | 说明 |
|---|---|
| `pet.py` | 桌宠窗口、动作、拖拽、扒边、菜单 |
| `brain.py` | 模型调用、人设、记忆、生图、识图 |
| `chat_dialog.py` | 对话窗与气泡 |
| `manifest.json` | 动作/皮肤/窗口行为配置 |
| `persona.txt` | 人设（第一人称、称呼、口吻），可直接改 |
| `assets/` | 皮肤与动作帧 |

## 说明

- 仅 Windows 完整可用（窗口置顶、屏幕边缘、截屏都用了 Win32 API）
- 角色形象来自《碧蓝航线》长门，仅作个人学习与同人用途；素材为本地生成/改绘
- 缺少密钥时依然能跑：只有对话与识图会不可用
