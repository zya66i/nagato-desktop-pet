# 长门桌宠（Nagato Desktop Pet）

一只住在你桌面上的 Q 版桌宠：会自己走动、扒屏幕边缘、发呆走神，也会跟你聊天、记得你。
基于 **Python + PySide6 + DeepSeek**，形象与动作由本地素材驱动。

---

## 目录

- [一、效果与功能](#一效果与功能)
- [二、环境要求](#二环境要求)
- [三、下载](#三下载)
- [四、安装依赖](#四安装依赖)
- [五、配置 API](#五配置-api)
- [六、运行](#六运行)
- [七、使用说明](#七使用说明)
- [八、可选的进阶功能](#八可选的进阶功能)
- [九、常见问题](#九常见问题)
- [十、目录结构](#十目录结构)
- [十一、声明](#十一声明)

---

## 一、效果与功能

- **桌面常驻**：无边框、可置顶、可拖动、可拎起来
- **动作**：走路（上/下/左/右，上楼梯背对你、下楼梯正对你）、扒屏幕左右边缘、睡觉（可盖被子）、发呆、眨眼、被戳
- **会聊天的性格**：由 DeepSeek 驱动，人设写在 `persona.txt` 里可自己改（第一人称「吾」，称呼你「主公」）
- **长期记忆**：每轮对话自动总结写入 `memory.json`，下次接着记得
- **发呆想事**：走神时会自己想一个念头，并在头顶弹出一个小小的念头画面
- **桌面感知**：知道时间、你正在用的程序、窗口标题、鼠标方位、屏幕上开着哪些窗口
- **可选识图**：接火山方舟视觉模型后，你在对话里说「帮我看看屏幕上有什么」，她真的会看屏幕再回答
- **陪伴时长**、**余额查询**等小功能在右键菜单里

---

## 二、环境要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | **Windows 10 / 11** | 置顶、屏幕边缘、截屏都用到 Win32 API，目前仅 Windows 完整可用 |
| Python | **3.10 或更高** | 代码用了 `int | None` 这种新语法，3.9 及以下会语法报错 |
| 磁盘 | 约 100 MB | 主要是动作帧图片 |
| 网络 | 需要能访问 `api.deepseek.com` | 想用识图还需访问 `ark.cn-beijing.volces.com` |

> 项目**不需要** GPU，也**不需要**本地跑模型。

---

## 三、下载

### 方式一：git clone（推荐，文件最全）

```bash
git clone https://github.com/zya66i/nagato-desktop-pet.git
cd nagato-desktop-pet
```

### 方式二：网页下载 ZIP

仓库页 → 绿色 **Code** 按钮 → **Download ZIP** → 解压到你想放的目录。

> ⚠️ **解压后不要打乱目录结构**，尤其是 `assets/` 目录必须和 `pet.py` 在同一层。
> 皮肤素材缺失会导致她"看不见"或动作缺帧。

### 方式三：只下源码（不推荐）

只下载 `.py` 文件也能启动，但会**看不到形象**——所有动作帧都在 `assets/` 里。

---

## 四、安装依赖

进入项目目录后执行（**Windows 命令提示符 / PowerShell**）：

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 依赖说明

| 包 | 用途 |
|---|---|
| `PySide6` | 窗口、渲染、动画 |
| `pillow` | 图片处理（抠图合成、截图编码） |
| `numpy` | 透明通道计算 |
| `requests` | 网络请求（可选路径会用到） |

### 如果 `python` 命令不存在

把 `python` 换成你的实际路径，例如：

```bash
C:\Users\你的用户名\AppData\Local\Programs\Python\Python311\python.exe -m venv .venv
```

---

## 五、配置 API

### 5.1 DeepSeek（对话能力，必需才有聊天）

1. 打开 <https://platform.deepseek.com/> 注册 / 登录
2. 左侧 **API keys** → **创建 API key** → 复制（形如 `sk-xxxxxxxx`）
3. **充值**（DeepSeek 需要余额才能调用，几元就够玩很久）
4. 把 key 设置成环境变量（**推荐**，不会写进任何文件）：

```powershell
# PowerShell（当前用户永久生效，重开终端后用）
setx DEEPSEEK_API_KEY "sk-你复制的那串"

# 命令行（cmd）
set DEEPSEEK_API_KEY=sk-你复制的那串
```

也可以直接填在 `config.json` 的 `api_key` 字段（**注意别把这个文件提交到公开仓库**）。

> 没配置 key 也能启动桌宠，只是**不能聊天**——走动、扒边、睡觉这些本地动作都正常。

### 5.2 模型选择

`config.json` 里：

```json
{
  "api_base": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "temperature": 0.8,
  "max_tokens": 600
}
```

- `deepseek-chat`：便宜、够用，**推荐**
- `deepseek-reasoner`：更会推理，但更慢更贵

### 5.3 火山方舟视觉模型（可选：让她看屏幕）

1. 打开 <https://console.volcengine.com/ark> 登录
2. **模型广场** → 找视觉模型（如 `Doubao-Seed-2.1-lite`）→ **开通**
3. 在 **API Key 管理** 里创建一个 key（形如 `sk-...` 或 `ARK_API_KEY`）
4. 设置环境变量：

```powershell
setx ARK_API_KEY "你的方舟key"
```

5. 在 `config.json` 里确认模型 ID（**要带日期后缀**，从控制台复制最稳）：

```json
{ "vision_model": "doubao-seed-2-1-lite-260915" }
```

> 模型 ID 写错会报 `404 InvalidEndpointOrModel.NotFound`——那就去控制台复制准确的 ID。

### 5.4 余额查询（可选）

菜单里的「查询余额（DeepSeek）」用 DeepSeek key 即可；
「查询余额（豆包·方舟）」需要**火山账号的 AK/SK**：

```powershell
setx VOLC_ACCESSKEY "你的AK"
setx VOLC_SECRETKEY "你的SK"
```

并可在 `config.json` 填 `volc_account_id`。
AK/SK 建议用**子账号 + 只读账单权限**，不要用主账号密钥。

### 5.5 配置文件优先级

程序读 key 的顺序：

1. 环境变量（`DEEPSEEK_API_KEY` / `ARK_API_KEY`）
2. `config.json` 里的 `api_key` / `ark_api_key`
3. 都没有 → 该功能静默不可用（不会崩溃）

---

## 六、运行

```bash
# 无控制台窗口（日常使用）
.venv\Scripts\pythonw.exe pet.py

# 有控制台（能看到日志，排查问题用）
.venv\Scripts\python.exe pet.py
```

**重复启动不会开第二只**：程序有单实例保护，已有一只时再次启动会**静默退出**。

想开机自启：把 `pythonw.exe pet.py` 做成快捷方式（目标指向 pythonw、起始位置填项目目录），
放进 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`。

---

## 七、使用说明

**右键她** 打开菜单：

| 菜单项 | 作用 |
|---|---|
| 对话… | 打开对话窗，回车发送，Esc 关闭 |
| 查询余额（DeepSeek） | 查 DeepSeek 余额 |
| 查询余额（豆包·方舟） | 查火山方舟余额（需 AK/SK） |
| 让她看看屏幕 | 截当前屏幕 → 视觉模型 → 她描述给你（约 20–60 秒） |
| 陪伴时间 | 累计陪你的时长 |
| 总在最前 | 开关窗口置顶 |
| 换衣服 | 和服 / 华服 两套皮肤 |
| 睡觉 | 不盖被子 / 盖被子 |
| 播放动作 | 走动、眨眼、戳一下、发呆、扒左边、扒右边、往上走、往下走、往左走、往右走 |
| 大小 | 切换角色尺寸 |
| 重新加载素材 | 改了图片后热重载 |
| 退出 | 关闭 |

**交互**：

- **单击**：戳一下（有概率主动回应你）
- **拖动**：把她挪到别处
- **长按 0.45 秒再拖**：把她"拎起来"
- **拖到屏幕边缘**：自动进入扒边

**对话里可以这么说**：

- 「往上走一点」「往下走一点」「往左走」「往右走」
- 「走到左下角然后扒在左边边上」（会拆成先上下、再左右、再扒边）
- 「你现在在哪」（她按实时位置回答，不会乱动）
- 「帮我看看屏幕上有什么」（需配置视觉模型）

---

## 八、可选的进阶功能

### 8.1 免费生图（念头小画）

她的念头画面默认走一个**本地免费生图中转**（Pollinations）。需要 Node.js：

```bash
node --version    # 没有就装 Node.js LTS
```

如果你不需要这个功能，**不用管**——她只是不会画念头小图，其它一切正常。

### 8.2 自定义人设

编辑 `persona.txt`（纯文本，保存即生效）：

```
你是《碧蓝航线》的长门……
第一人称用「吾」，称呼对方「主公」……
```

### 8.3 自定义动作/皮肤

`manifest.json` 是核心配置：动作帧序列、fps、时长、皮肤部件表都在里面。
皮肤合并规则：**以 `skins.default.parts` 为基准**，用所选皮肤覆盖同名键，
皮肤没做的帧自动沿用默认皮肤——所以新皮肤可以一帧一帧往上加。

---

## 九、常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 双击后一闪就没 | 依赖没装 / Python 版本低 | 用 `python pet.py`（带控制台）跑一次看报错 |
| 窗口空白、看不见她 | `assets/` 丢了 / 路径被打乱 | 确认 `assets/` 与 `pet.py` 同层，别只拷 `.py` |
| 打不进字 | 旧版本遗留 | 更新到最新代码；对话窗需要鼠标点一下输入框 |
| 她说话回「网络异常」 | key 没配 / 欠费 / 被墙 | 检查 `DEEPSEEK_API_KEY` 与余额 |
| 识图报 404 | 视觉模型 ID 不对/未开通 | 控制台复制准确 ID，确认已开通 |
| 走路很快像滑过去 | 旧版本固定时长 | 更新代码；速度在 `manifest.json` 的 `walk_speed_px_s` |
| 做动作换皮肤 | 旧版本皮肤部件串味 | 更新到最新代码（已修复） |
| 语音/输入法候选框位置不对 | 系统输入法差异 | 已在较新版本修复窗口标志问题 |

日志在项目目录的 `pet.log`，排查问题时**发最后 15 行**最有用。

---

## 十、目录结构

```
nagato-desktop-pet/
├─ pet.py              桌宠窗口、动作、拖拽、扒边、菜单
├─ brain.py            模型调用、人设、记忆、生图、识图
├─ chat_dialog.py      对话窗与气泡
├─ manifest.json       动作 / 皮肤 / 窗口行为配置
├─ persona.txt         人设（可直接改）
├─ config.json         API 与模型配置（密钥留空）
├─ requirements.txt    Python 依赖
├─ README.md
└─ assets/
   ├─ parts/                 和服皮肤 + 四方向走帧 + 楼梯帧
   ├─ master/                母图（改绘用）
   ├─ skins/wedding/         华服皮肤（含专属动作帧）
   └─ archive/               历史版本素材
```

运行时自动生成（已在 `.gitignore` 里）：

`memory.json`（记忆）、`stats.json`（陪伴时长）、`settings.json`（皮肤/置顶）、
`pet_state.json`（位置）、`companion.log` / `pet.log`（日志）

---

## 十一、声明

- 本项目为**个人学习与同人创作**用途，角色形象来自《碧蓝航线》（长门），
  图片素材由本地 AI 生成/改绘，不作为商业用途。
- 请勿把 `config.json`、`.credentials.yaml`、`memory.json` 等**含密钥或个人数据**的文件提交到公开仓库。
- 使用第三方 API（DeepSeek / 火山方舟）请自行遵守其服务条款并注意费用。
