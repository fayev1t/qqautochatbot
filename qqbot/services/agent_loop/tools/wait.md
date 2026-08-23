# 工具：`wait`

## 功能

`wait` 在指定秒数后为当前 scope 安排一次唤醒。调用立即返回；计时器到期后，
系统写入 `runtime.wait_elapsed` 事件，在时间线中渲染为 `<system>wait_elapsed`
行并携带 `note` 原文，随后启动一个新 tick。

用于自我提醒与延迟执行其它动作，其中一个具体用途是给自己的回想改期：系统在
群里静默满阈值时会落 `<system>silence_elapsed` 并叫醒一次，本工具是在此之外按
当前处境自行约定的那一次。

## 参数

- `seconds`：必填整数，范围为 5–6000，表示唤醒前的等待秒数。
- `note`：必填字符串，说明约这一次唤醒是为了什么。去除首尾空白后不得为空，
  最多保留 500 个字符；计时器到期时原样写入 `wait_elapsed`。

## 返回

调用成功立即返回：

```json
{
  "scheduled": true,
  "seconds": 1800,
  "wake_at": "2026-08-03T15:31:00+08:00",
  "note": "看看那件事有没有下文"
}
```

## 失败

- `invalid_arguments`：`seconds` 非整数或越界（`seconds_not_int` /
  `seconds_out_of_range`）；`note` 非字符串或去空白后为空（`note_not_str` /
  `note_empty`）。

## 生命周期

计时器仅保存在当前进程内存中，不会在进程重启后恢复。已完成的工具调用及其
`wake_at` 仍保留在时间线中。多个调用会创建多个相互独立的计时器，运行时不做
合并或去重。
