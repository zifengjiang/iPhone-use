# 连接与工具故障排查

按顺序排查：本机工具 → 系统是否看到手机 → iPhone Use 能否打开隧道 → 界面树是否可用。每一步都有明确证据，不要跳过直接重装。

## 1. 本机工具

```bash
~/.local/share/iphone-use-coredevice/venv/bin/python -c "import pymobiledevice3, PIL; print(pymobiledevice3.__version__)"
~/.local/share/iphone-use-axe/axe --version   # 或 which axe
```

任一条失败就重建：

```bash
/opt/homebrew/bin/python3 -m venv ~/.local/share/iphone-use-coredevice/venv
~/.local/share/iphone-use-coredevice/venv/bin/pip install pymobiledevice3 Pillow
```

`pua_doctor` 的 `coredevice` / `axe` 两段就是这两项检查。路径不同时设置 `IPHONE_USE_COREDEVICE_PYTHON` 与 `IPHONE_USE_AXE`，注意 MCP 进程的环境变量来自 Codex 配置，而不是你交互式 shell 的配置。

## 2. 系统是否看到手机

```bash
xcrun devicectl list devices
```

- 列表里没有目标手机：USB / 数据线 / 端口 / 信任问题，先处理这些。
- 有手机但 `connection_state` 不是 connected：重新插拔，或在手机上确认“信任此电脑”。
- 有手机、能连但 `pua_doctor` 的 `probe` 失败：通常是手机锁屏或开发者模式未开。

## 3. 常见错误码

| 错误码 | 含义 | 处理 |
| --- | --- | --- |
| `device_unavailable` | 没有固定 UDID | `pua_setup(action="discover")`，再 `configure` |
| `no_such_device` | configure 的 UDID 不在本次 discover 结果里 | 重新 discover，复制当前 udid |
| `coredevice_unavailable` | 解释器或依赖缺失 | 见第 1 节 |
| `coredevice_failed` | 隧道、HID 或截图失败 | 换线换口、确认解锁与信任；重跑 `pua_ready` |
| `coredevice_timeout` | 单次操作超时，`uncertain=true` | 动作可能已执行，先观察实际状态再决定 |
| `axe_unavailable` | AXe 不存在或不可执行 | 安装 AXe 或设置 `IPHONE_USE_AXE` |
| `axe_disconnected` / `axe_service_unavailable` | AXe 服务未就绪 | 重新插拔，重跑 `pua_ready`；连续失败再看 Console 中的 AXe 日志 |
| `axe_developer_mode` | 开发者模式未开 | 手机上开启并重启 |
| `axe_disk_image` | 开发者镜像不可用 | 用 Xcode 连接一次让系统准备镜像 |
| `axe_tree_unavailable` | 当前页面没有可读树 | 取截图，按坐标操作；锁屏界面常见 |
| `phone_locked` | 手机锁屏 | 解锁，或确认用例密码后 `pua_unlock` |
| `device_busy` | 另一个进程持有操作锁 | 结束并发进程，或稍后重试一次 |
| `preview_paused` | 预览被暂停 | `pua_screen(action="resume")`，或让用户点刷新 |
| `clipboard_unavailable` | 截图写入剪贴板失败 | 检查 `osascript` 权限；不影响其他工具 |

## 4. 锁屏与解锁

`pua_ready` 在锁屏时返回 `ready=false, state="locked"`，不是故障。只有 AXe 报告锁屏时 `pua_unlock` 才会输入密码，解锁后必须用新的元素树证明成功。

没有锁屏密码的测试机不要调用 `pua_unlock`：它在已解锁但被误判锁屏时会点按并滑动屏幕。真机排查时优先让用户手动解锁。

## 5. 预览

预览来自 CoreDevice 视频流，不落盘。`frame_available=false` 只表示首帧未到，不代表控制通道失败。刷新按钮会重启流并探测一次 `pua_ready`；锁屏会暂停为 `device_locked`，认证接管会暂停为 `authentication`。

## 6. 状态目录

`~/.local/share/iphone-use/`（或 `IPHONE_USE_STATE_DIR`）里关注：

- `config.json`：已固定的 UDID 与型号。
- `operation.lock`：操作锁；进程被杀后文件残留不影响下次加锁。
- `coredevice-helper.log`：helper 的 stderr，隧道与截图错误在这里。
- `artifacts/`：截图，保留上限 100 张。

怀疑配置损坏时可以改 `config.json` 里的 `udid`，或删掉后重新 `pua_setup configure`；不需要删除整个目录。
