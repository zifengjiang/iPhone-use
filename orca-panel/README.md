# iPhone Screen — Orca 侧边栏面板

在 Orca 右侧边栏看 USB 直连 iPhone 的实时画面，并直接点击/滑动/按硬件键。

## 安装

```bash
cd orca-panel
orca plugin install .          # 打印权限清单与指纹
orca plugin install . --approve <指纹>
orca plugin enable local.iphone-use-screen --approve <指纹>
orca plugin open local.iphone-use-screen
```

开发时用 `orca plugin dev load . --approve <指纹>`，改完 `dev load` 再跑一次即生效。

## 依赖

- 仓库内的 `server/coredevice_helper.py`（面板 worker 通过它取流与发手势）。
- `~/.local/share/iphone-use-coredevice/venv`（pymobiledevice3 + Pillow），可用 `IPHONE_USE_COREDEVICE_PYTHON` 覆盖。
- 设备：USB 直连；只接一台时自动选中，多台用 `IPHONE_USE_COREDEVICE_UDID` 指定。

## 工作方式

面板帧禁网（CSP `connect-src 'none'`），所有请求走 worker：worker 用
`coredevice_helper.py --stream` 取设备自身 HEVC 流（VncStreamServer 负责 RCTL 回执与解码），
转成 multipart JPEG，面板按序列号长轮询取最新帧。点击/滑动换算成 0–1 坐标交给 helper 的 HID 服务。

## 局限

- 键盘输入不在面板里（走 MCP 的 `pua_type_text`）。
- 只显示当前在前台的画面，App 切走时看到的是系统界面。
