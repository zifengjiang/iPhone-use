# iPhone Use

**中文** · [English](README.en.md)

![iPhone Use 在 Codex 中操作真实 iPhone 并实时展示手机屏幕](assets/iphone-use-demo.png)

让 Codex 通过 USB 操作你的真实 iPhone。用自然语言描述任务，Codex 就能打开 App、读取页面、点击、滚动、输入文字，并在侧边栏展示手机屏幕。

手机端不需要安装任何 runner，也不需要 Xcode 签名。iPhone Use 只使用本机两个通道：

| 通道 | 负责 | 实现 |
| --- | --- | --- |
| CoreDevice | 截图、坐标点击、滑动、键盘输入、Home / 音量键、解锁、启动 App | 独立解释器里的 `pymobiledevice3`，通过 JSON-lines 子进程协议调用 |
| AXe | 元素树、按选择器点击 | 本机 `axe` 二进制，走 CoreDevice 隧道读取辅助功能树 |

两者都只在本机运行，USB 通信由 `pymobiledevice3` 直接完成，没有端口转发、没有 HTTP 服务、没有手机端 App。控件定位失败时，工具会返回当前截图与坐标换算，模型据此改用坐标操作。

## 安装依赖

| 依赖 | 用途 |
| --- | --- |
| macOS 与 Codex 桌面端 / CLI | 安装本地插件、运行 MCP 服务与屏幕侧边栏 |
| USB 连接的真实 iPhone | 信任此 Mac，开启开发者模式，使用时保持解锁 |
| Python 3.9+ | 运行 MCP 服务；主进程只用标准库 |
| CoreDevice 独立解释器（`pymobiledevice3` + `Pillow`） | 截图、HID 手势、键盘、按键、解锁与启动 App |
| [AXe](https://github.com/cameroncooke/AXe) 二进制 | 元素树与按选择器点击 |
| Node.js 20.19+、22.12+ 或 24+，npm 10+ | 仅在本地构建屏幕 widget 时需要 |

首次准备（两条命令即可，之后不再需要）：

```bash
/opt/homebrew/bin/python3 -m venv ~/.local/share/iphone-use-coredevice/venv
~/.local/share/iphone-use-coredevice/venv/bin/pip install pymobiledevice3 Pillow

brew install cameroncooke/axe/axe   # 或从 AXe 仓库安装到 ~/.local/share/iphone-use-axe/axe
```

`pua_doctor` 会检查两者是否就位。默认路径是 `~/.local/share/iphone-use-coredevice/venv/bin/python` 与 `~/.local/share/iphone-use-axe/axe`，可用 `IPHONE_USE_COREDEVICE_PYTHON`、`IPHONE_USE_AXE` 覆盖。

## 用一段提示词让 Codex 安装

将下面这段话复制到本机 Codex：

```text
请帮我安装和配置 iPhone Use：
https://github.com/zhongerxin/iPhone-use

先读取仓库 README 和安装脚本，检查本机 Codex CLI、Python、Node.js、npm，
以及通过 USB 连接的 iPhone。把项目放到合适的本机目录，运行 sh scripts/install.sh
安装插件。

创建 CoreDevice 助手用的解释器，安装 pymobiledevice3 与 Pillow，并确认 AXe
二进制可用。如果当前聊天还没有加载新工具，明确告诉我重连或新开聊天后继续。
工具可用后读取 iphone-use-setup 技能，运行 pua_setup 发现设备并配置 UDID，
直到 pua_ready 返回 ready=true，然后打开手机屏幕。不要照搬作者的设备标识。

缺少依赖时说明具体缺项并帮助安装。设备信任、开发者模式或解锁需要我操作时，
告诉我明确步骤，等我完成再继续。
```

安装插件后，当前聊天可能需要重新连接才能加载工具，流程可在重连后继续。设备信任、开发者模式及手机解锁仍需本人在系统界面完成。

仓库目前为私有，需要有 GitHub 访问权限，或者使用已取得的源代码包。

## 安装插件

```sh
git clone https://github.com/zhongerxin/iPhone-use.git
cd iPhone-use
sh scripts/install.sh
```

脚本验证并暂存源码，通过 Codex CLI 注册本地 marketplace、安装插件和技能，并将安装缓存中的服务注册为标准 MCP `iphone_use`。插件与标准配置使用同一命名空间，避免重复工具注册。

源代码包解压后，也可以在包目录运行同一个安装脚本。安装完成后重连或新开 Codex 聊天。

## 连接并验证

在新聊天中启用 **iPhone Use**，输入：

> 用 iphone-use-setup 帮我配置通过 USB 连接的 iPhone，验证 READY，然后显示手机屏幕。

技能引导 Codex 运行 `pua_setup` 的 `discover` 与 `configure`，把设备 UDID 写进状态目录，再用 `pua_ready` 证明两条通道都能用：`proof.coredevice` 与 `proof.axe` 同时为 true 才返回 `ready=true`。锁屏时 `pua_ready` 返回 `state: "locked"` 而不是报错，先解锁再继续。

## 可以做什么

| 功能 | 示例 |
| --- | --- |
| App 导航与操作 | 打开目标 App，进入搜索、详情、设置或草稿页面 |
| 页面读取 | 获取紧凑控件树、文字、位置和实际截图 |
| 点击与手势 | 按控件或坐标点击，滑动，等待页面目标出现 |
| 文本输入 | 一次提供完整 ASCII 文字，可带换行与 Tab |
| 连续任务 | 将已知点击、输入、等待等步骤组成一次 batch，减少模型往返 |
| 可见失败恢复 | 控件被遮挡、不唯一或树不完整时，查看新截图并重新选择点击位置 |
| 实时手机屏幕 | iPhone 外壳、设备状态、操作光效与清楚可见的点击 / 拖动提示 |
| 连接恢复 | 预览可刷新重连；区分锁屏暂停与主动暂停 |

示例提示词：

```text
用 iPhone Use 打开备忘录，新建一条草稿，输入下面的内容，核对后保留。

打开目标 App，搜索这个名称，读取前几项结果并整理给我。

把这个页面上的列表从头翻到尾，记录每一项的标题。
```

输入与提交分开，工具不会替你按下发送键。模型需要核对关键页面、接收人、数量和最终结果。密码、验证码、Face ID 等认证交给用户完成，接管期间可以暂停预览。

屏幕预览在同一聊天中复用已有 widget。底部提供刷新、主屏幕和截图按钮；无图像时保留黑色屏幕的 iPhone 外壳，屏幕内仅显示对应状态图标，顶部显示连接或暂停状态。预览供用户观看，模型定位仍以工具返回的实际图像或控件为依据。

## 技术亮点

- **本机 USB 直连。** `pymobiledevice3` 在独立进程里持有隧道与 HID 会话，MCP 主进程只用标准库，通过 JSON-lines 通信；不开放端口，不装手机端服务。
- **两棵来源明确的树。** 元素树来自 AXe 的辅助功能快照，坐标与 HID 来自 CoreDevice；两者都失败时才提示检查 USB、信任与解锁。
- **紧凑观察。** 节点省略与文字重复的 name / value，只标注异常状态（`enabled:false`、`visible:false`、`in_viewport:false`）；截图默认缩放为模型友好的 JPEG，原图仅在 `IPHONE_USE_IMAGE=original` 时保留。
- **截图兜底。** 元素定位失败时返回截图与 `image.pixel_to_point`，按 iPhone 点坐标调用 `pua_tap` 即可。
- **明确失败语义。** 多进程共享操作锁；超时与 `uncertain` 结果要求先读实际状态，再决定是否重试，避免盲目重放点击或输入。
- **解锁有前置证明。** 只有 AXe 报告锁屏才会输入密码，解锁后必须用新的元素树证明成功。
- **实时预览与暂停恢复。** 屏幕流来自 CoreDevice 的视频流，不落盘；区分锁屏与主动暂停，刷新可主动重连。

这些优化主要减少重复请求与模型往返，完整任务速度仍取决于 App、USB 状态和模型响应。

## 工具概览

模型可用 16 个工具，另有 2 个仅供屏幕 widget 使用的工具。

| 工具 | 用途 |
| --- | --- |
| `pua_doctor`、`pua_setup`、`pua_ready` | 环境诊断、设备发现与配置、就绪证明 |
| `pua_observe`、`pua_find` | 控件树、截图与目标查询 |
| `pua_apps`、`pua_launch_app` | 查询 App 标识、读取安装证据和启动 App |
| `pua_tap`、`pua_swipe`、`pua_press_button` | 点击、滑动、主屏幕与音量键 |
| `pua_type_text`、`pua_wait` | ASCII 输入与有界等待 |
| `pua_unlock`、`pua_batch` | 锁屏解锁、组合动作 |
| `pua_screen`、`pua_metrics` | 预览开关与有界耗时统计 |

界面异常时先返回截图，再由模型判断下一步。遮挡、滚动无进展、输入不符或预期页面未出现也走截图兜底。已有截图直接复用，不自动继续盲滑或重放操作。

## 本机数据与升级

新安装默认使用 `~/.local/share/iphone-use/`，可通过 `IPHONE_USE_STATE_DIR` 指定外部目录；旧版 `WDA_STATE_DIR` 仍兼容。升级时若新目录不存在，会继续使用检测到的旧配置目录，保留设备配置。

运行目录权限为 700，私有配置和图像文件为 600。明确调用的截图有保留上限，预览流不落盘。设备数据、运行日志与依赖目录均不进入源代码发布包。

更新已克隆的仓库：

```sh
git pull --ff-only
sh scripts/install.sh
```

重新连接聊天以加载更新后的工具、技能和屏幕资源。若手机连接失败，先检查 USB、设备解锁、开发者模式与 `pua_doctor` 返回的具体错误。

## 开发与文档

```sh
npm ci --prefix ui --no-audit --no-fund
npm run build --prefix ui
sh scripts/check.sh
python3 scripts/package.py
```

构建结果是自包含屏幕 HTML，源代码包位于 `dist/iphone-use-<版本>-source.zip`。安装和普通使用不需要重新构建 UI。安装后的插件仍完整保留 widget 的源码、样式、构建脚本、配置、依赖锁文件和测试，便于本地维护。

- [屏幕预览与恢复](skills/iphone-use/references/screen.md)
- [App 标识参考](skills/iphone-use/references/apps.md)
- [安装与连接故障排查](skills/iphone-use-setup/references/troubleshooting.md)
- [更新记录](CHANGELOG.md)

## 依赖与致谢

本项目使用 [CoreDevice](https://github.com/doronz88/pymobiledevice3) 的原生隧道与 HID 能力，以及 [AXe](https://github.com/cameroncooke/AXe) 的辅助功能树读取。iPhone Use 在这些基础上提供 Codex 插件、MCP 工具、安装引导与屏幕 widget。

感谢 pymobiledevice3、AXe 及相关项目的维护者和贡献者，让真实 iPhone 的自动化操作成为可能。

MIT License。第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
