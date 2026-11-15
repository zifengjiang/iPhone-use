---
name: iphone-use-setup
description: 安装与连接 iPhone Use 的 CoreDevice + AXe 本机通道，发现并固定 USB iPhone，证明 READY，并排查 USB、信任、开发者模式、锁屏与工具缺失。用于首次安装、更换手机、连接失败或 READY 不通过。
---

# 安装与连接

iPhone Use 只需要两个本机组件，手机端不安装任何东西，也不需要 Xcode 签名：

| 组件 | 作用 | 默认位置 |
| --- | --- | --- |
| CoreDevice 解释器（`pymobiledevice3` + `Pillow`） | 截图、HID、键盘、按键、解锁、启动 App | `~/.local/share/iphone-use-coredevice/venv/bin/python` |
| AXe 二进制 | 元素树、按选择器点击 | `~/.local/share/iphone-use-axe/axe`，或 PATH 上的 `axe` |

```bash
/opt/homebrew/bin/python3 -m venv ~/.local/share/iphone-use-coredevice/venv
~/.local/share/iphone-use-coredevice/venv/bin/pip install pymobiledevice3 Pillow
brew install cameroncooke/axe/axe
```

路径不同就用 `IPHONE_USE_COREDEVICE_PYTHON`、`IPHONE_USE_AXE` 指定。`pua_doctor` 会报告两者是否可用，缺项会写进 `next_steps`。

## 连接手机

1. USB 连接，手机上点“信任此电脑”，并保持解锁。
2. 设置 → 隐私与安全性 → 开发者模式，打开后重启手机。
3. 调用 `pua_doctor`，确认 `coredevice.available` 与 `axe.available` 都为 true。

`pua_setup`：

- `action="discover"`：列出 USB 上的 iPhone（udid、型号、iOS 版本、连接状态）。可能较慢，几秒内返回。
- `action="configure", udid=...`：把 UDID 写入状态目录并立即重定向通道。UDID 必须来自当前的 discover 结果，否则返回 `no_such_device`。
- 其他 action 或省略：返回诊断（选择、工具、USB 探测与 `next_steps`）。

配置完成后调用 `pua_ready`。`ready=true` 且 `proof.coredevice`、`proof.axe` 都为 true 才算接通；此时可以把后续任务交给 `iphone-use` 技能。

换手机时重新 discover + configure。`pua_ready(device="<udid>")` 也可以临时切换，但不会写配置。

## 常见阻塞

| 现象 | 处理 |
| --- | --- |
| `coredevice_unavailable` | 解释器路径不对或没装依赖；按 `next_steps` 重建 venv。 |
| `axe_unavailable` | 没装 AXe 或不在 PATH；设置 `IPHONE_USE_AXE`。 |
| `coredevice_failed` / 找不到设备 | 换数据线或端口；确认已信任本机、已开开发者模式、手机已解锁。 |
| `device_unavailable` | 还没固定 UDID；运行 discover 再 configure。 |
| `ready=false, state="locked"` | 手机锁屏。有用例密码用 `pua_unlock`，否则请用户解锁后重跑 `pua_ready`。 |
| `axe_disconnected` / `axe_service_unavailable` | AXe 服务没起来：重新插拔 USB，重跑 `pua_ready`。 |
| `axe_developer_mode` / `axe_disk_image` | 开发者模式或开发者镜像不可用；按提示在手机上开启或让 Xcode 准备镜像。 |
| `phone_locked` | 与锁屏相同；预览会暂停为 `device_locked`。 |
| `device_busy` | 另一个进程正在操作手机；先结束它或稍后重试。 |

USB 反复掉线时先换线、换直连端口，避免 Hub。仍失败时用 `xcrun devicectl list devices` 确认系统是否能看到设备：看不到是 USB / 信任问题，看得到但 `pua_ready` 失败则是本机工具问题。

## 状态目录与升级

默认 `~/.local/share/iphone-use/`，可用 `IPHONE_USE_STATE_DIR` 指定；旧的 `WDA_STATE_DIR` 仍兼容。目录权限 700，配置文件 600。升级插件后重连聊天；`sh scripts/install.sh` 会重建安装缓存，状态目录与已固定的 UDID 保持不变。

其余任务操作细节见 `iphone-use` 技能；预览与认证接管见 `iphone-use/references/screen.md` 与 `authentication.md`。
