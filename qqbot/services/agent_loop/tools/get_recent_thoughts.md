# 工具：`get_recent_thoughts`

## 功能

`get_recent_thoughts` 取回当前 scope 最近若干拍程序里写下的**注释文本**，按时间
正序返回。

这些注释是每一拍当时写下的判读。当拍完整源码已在 `<action>` 的 next_action 里；本工具
按多拍抽取注释，不含程序源码本身，也不含那些拍的工具结果。

无注释的拍不会出现在结果中，也不占 `limit` 名额。源码在当拍未通过静态预检的
拍整拍跳过。

## 参数

- `limit`：可选整数，1–30，缺省 20。最多返回多少拍。
- `within_hours`：可选整数，1–24，缺省 6。只看最近这么多小时内的拍。

## 返回

```json
{
  "returned": 2,
  "ticks": [
    {"at": "2026-08-03T14:58:12+08:00", "notes": "他们在聊周末的事\n先不插话"},
    {"at": "2026-08-03T15:02:41+08:00", "notes": "这句应该是问我的"}
  ]
}
```

单拍注释合并后超过 400 字会在尾部截断并加 `…`。

## 失败

- `invalid_arguments`：`limit` / `within_hours` 非整数或越界，`reason_code` 取
  `limit_not_int` / `limit_out_of_range` / `within_hours_not_int` /
  `within_hours_out_of_range`。

## 权限与作用域

无最低权限要求，所有 scope 可用。查询范围严格限于当前 scope 自己的决策记录。
