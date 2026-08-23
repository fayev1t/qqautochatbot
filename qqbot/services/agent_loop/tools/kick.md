# 工具：`kick`

## 功能

`kick` 将指定成员移出当前群，对应 OneBot V11 `set_group_kick`。
`reject_add_request=true` 时同时拒绝该成员后续的入群申请。

## 参数

```json
{
  "user_id": 12345,
  "reject_add_request": false
}
```

- `user_id`：必填整数，表示目标成员的 QQ 号。
- `reject_add_request`：可选布尔值，默认 `false`。为 `true` 时同时拒绝
  目标成员后续的入群申请。
- `group_id` 从当前 `scope_key` 注入，参数中不存在 `group_id`。
- `user_id` 不能等于机器人自身账号。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=ADMIN`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`。
- 机器人群角色必须严格高于目标成员：owner 可操作 admin/member，admin 仅可
  操作 member。目标角色可查询时会在调用 OneBot 前校验。

## 返回

成功返回：

```json
{
  "group_id": 100,
  "user_id": 12345,
  "reject_add_request": false,
  "applied": true
}
```

权限条件不满足时返回 `permission_denied_user_tier` 或
`permission_denied_bot_role`；目标是机器人自身时返回
`invalid_arguments`；OneBot 调用失败时返回 `upstream_action_failed`。

成功操作后，平台通常会产生
`<notice>group_decrease …被…移出` 行。其中
`operator_qq` 等于当前 `bot_qq` 时，该 notice 是本次调用的回执事件。
