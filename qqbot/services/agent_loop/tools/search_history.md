# 工具：`search_history`

## 功能

`search_history` 检索当前 scope 的历史事件。默认时间线只包含近期窗口，本工具
直接查询 `agent_events`，并使用与正常时间线相同的 XML 渲染格式返回结果。

## 参数

所有过滤条件均为可选，多个条件使用 AND 组合：

- `anchor_event_id`：仅返回严格早于该 ULID 事件 ID 的事件。
- `start_time`：ISO8601 起始时间，包含边界。
- `end_time`：ISO8601 结束时间，包含边界。
- `query`：对消息 `search_text` 执行 pg_trgm 模糊相似度匹配，不要求精确
  子串匹配。
- `limit`：可选整数，默认 20，归一到 1–50 范围。

查询始终受当前 `scope_key` 隔离：group scope 按 `group_id` 过滤，private
scope 按 `user_id` 过滤。参数中不存在跨 scope 的目标字段。

## 权限与作用域

`required_permission=GUEST`，`allowed_scopes` 不限，不要求机器人群角色。

## 返回

成功返回：

```json
{
  "matched": 1,
  "anchor_event_id": "01...",
  "items": [
    {
      "event_id": "01...",
      "occurred_at": "2026-07-30T12:00:00+08:00",
      "kind": "message",
      "render": "<msg>名字(QQ) #消息ID: 正文"
    }
  ],
  "warnings": []
}
```

`items` 按事件时间正序排列。时间字符串无法解析时，对应过滤条件会被跳过并
写入 `warnings`，调用仍可成功。没有匹配项时 `matched=0` 且 `items=[]`。

缺少或无法解析 `scope_key` 时返回 `invalid_arguments`。
