---
name: iphone-use
description: 通过 PUA（Phone Use Agent）MCP 工具操作真实 iPhone；首次使用先取得 READY，锁屏先解锁，再按 observe → find → tap 的循环执行任务。默认乐观执行导航、点击和滚动，在下一步需要信息时顺带观察，只对关键结果显式验收；涵盖 App 查找、输入、批量动作、手机屏幕侧边栏与认证接管。
---

# 用 PUA 完成 iPhone 任务

PUA 由两个本机通道组成：CoreDevice 负责截图、坐标点击、滑动、键盘、Home / 音量键、解锁和启动 App；AXe 负责元素树和按选择器点击。两者都通过 USB 直接访问手机，手机端不需要安装任何服务。

先确定用户要交付的结果和完成条件。一次正常返回表示动作已按计划执行，继续准备下一步；不要为每次点击或翻页单独观察。最终关键操作、发送结果和关键文本需要实际证据。

## 取得 READY

本对话首次使用手机时调用 `pua_ready(screenshot=false)`；需要画面时再取截图。返回 `ready=true` 时复用其中的 `observation` 准备下一步，不立即重复 observe。

- `state="locked"`：手机锁屏。有用例密码就用 `pua_unlock`；没有则请用户在手机上解锁，再重跑 `pua_ready`。锁屏时不要猜测页面内容。
- `device_unavailable`：还没有固定设备。调用 `pua_setup(action="discover")` 找到 UDID，再 `configure`。
- `coredevice_unavailable` / `axe_unavailable`：本机工具缺失，按返回的 `next_steps` 补齐，详见 `iphone-use-setup`。
- 已有本对话 READY 且通道正常时直接继续，不重跑 setup 或 doctor。

READY 关联手机屏幕侧边栏。已有面板就复用；用户要求打开时调用一次 `pua_screen(action="open")`。底部的刷新 / 主屏幕 / 截图按钮由用户自己点，`pua_screen_frame`、`pua_screen_action` 不是模型的工具。

## 观察与定位

`pua_observe(mode="tree"|"screenshot"|"both")` 返回紧凑树：`type` 不带前缀（`Button`、`TextField`），`rect` 是 `[x, y, width, height]`（iPhone 点），没有 `enabled` / `visible` / `in_viewport` 表示正常。默认 `max_nodes=100`；`truncated=true` 时用 `pua_find` 精确查找，不要重复拉整树。

树里没有目标（自绘内容、系统弹窗、图像文字）时取截图。截图像素乘以 `image.pixel_to_point` 得到 iPhone 点坐标：

```
x = px * pixel_to_point[0]
y = py * pixel_to_point[1]
```

`pua_find(selector, limit)` 返回候选，每个候选带 `tap` 点；`pua_wait(selector, timeout_seconds)` 在超时前轮询树，超时返回 `postcondition_failed`。

selector 只支持 `label`、`label_contains`、`name`、`value`、`type`、`enabled`、`index`。从当前节点照抄，不要自造字段；标签含变化的时间或数字时用 `label_contains`。

## 操作

- `pua_tap`：优先用 selector。同一位置叠了多个匹配（Cell 与其中文字）时工具自动取最内层；多个分开的匹配返回 `ambiguous_target`，按候选的 `tap` 坐标点击或加 `index`。selector 表达不了时传 `x`、`y`。坐标点击后如果页面没变，先取截图确认遮挡，不要原样重试。
- `pua_swipe`：`direction` 是手指移动方向，`up` 通常看到下面的内容；可用 `region` 限定区域。只执行一次手势，是否移动看下一张截图。
- `pua_type_text`：只支持可打印 ASCII 与 Tab、换行，单次最多 500 字符，更长内容分几次输入（焦点保持在输入框内时逐次追加）。先点中输入框，再不带 selector 调用。`allow_newlines=true` 只在确实需要多行时使用；含非 ASCII 字符会被拒绝，需要改用其他输入方式并告知用户。
- `pua_press_button`：`home`、`volumeup`、`volumedown`。锁屏请用 `pua_unlock`。
- `pua_launch_app(bundle_id)`：已知 bundle ID 直接传；不确定时用 `pua_apps(query=...)`，`installed_verified=true` 才是本机安装证据。
- `expect` 只用于需要证明的关键一步：动作完成后工具轮询 AXe 树确认目标出现，否则在结果里返回 `postcondition_failed` 并附截图。普通步骤不要加。

## 输入与发送

工具不会替你发送消息。先点击输入框、确认焦点，再用 `pua_type_text` 写完整内容，然后取一次树或截图核对草稿，最后单独点击发送按钮。发送后核对结果页或消息记录。

密码、验证码、Face ID 由用户完成：先 `pua_screen(action="pause")` 清空画面，用宿主提问工具请用户操作，收到完成确认后 `pua_screen(action="resume")` 并重新观察。

## 批量与长页面

`pua_batch` 最多 20 步，支持 tap / swipe / type_text / launch_app / press_button / wait / observe。返回 `completed_steps` 与 `stop_reason`：`time_budget` 表示时间预算用尽，其他值是失败步骤的错误码。按 `stopped_at` 继续剩余步骤，不重放整个批次。

翻长列表时不要一次塞入多次滑动：先 `pua_swipe`，再取树或截图记录内容，判断是否到边界，然后决定下一步。工具不再提供滚动查找和自动采集，覆盖范围由你按实际读取的条目说明。

## 失败处理

| 结果 | 处理 |
| --- | --- |
| `phone_locked` | 按上文解锁；预览会自动暂停为 `device_locked`。 |
| `device_busy` | 另一个进程正在操作手机，稍后重试一次；不要并发点击。 |
| `coredevice_timeout` / `uncertain` | 手机可能已经执行，先观察实际状态，再决定是否重做。 |
| `axe_target_not_found`、`axe_ambiguous_target`、`axe_target_changed` | 结果附带截图；按当前画面用坐标操作或重新选取。 |
| `axe_disconnected`、`axe_service_unavailable`、`coredevice_failed` | 检查 USB、信任与解锁，重跑 `pua_ready`。 |
| `preview_paused` | 预览被暂停；`pua_screen(action="resume")` 或让用户点刷新。 |

自绘页面、浮层遮挡或树为空时以截图为准，不要反复更换 selector 写法。
