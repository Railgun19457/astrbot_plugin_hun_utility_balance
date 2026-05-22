# HNU 水电费查询

AstrBot 插件：查询海南大学水电费余额，并按配置定时检测低余额提醒。

## 功能

- 指令查询：`/水电查询`
- LLM 工具：`query_hnu_utility_balance`，用于让模型主动查询水电费余额（使用 `FunctionTool` 注册）
- 自动检测：按配置的检测频率定时查询
- 低余额提醒：使用 `template_list` 配置多个提醒规则

## 配置

- `openid`：海南大学水电费小程序的 openId，可参考 [hnu-utility-balance](https://github.com/Railgun19457/hnu-utility-balance) 的脚本获取。
- `enable_auto_check`：是否启用自动检测。
- `check_interval_minutes`：检测频率，单位分钟。
- `query_display_items`：`/水电查询` 返回显示项，可独立控制 `姓名`、`学校`、`楼栋`、`房间`、`热水余额`、`照明`、`空调`、`水表`、`检测提示` 是否显示。
- `reminders`：提醒列表，每条提醒包含：
	- `fee_type`：检测费用种类，支持 `照明`、`空调`、`水表`、`热水`。
	- `threshold`：提醒阈值（元）。余额小于或等于该值时触发提醒。
	- `session_umo`：提醒会话 UMO，可在目标会话发送 `/sid` 获取。

## 说明

发送 `/水电查询` 时会立即从接口获取最新余额数据，同时把下一次自动检测时间更新为当前时间之后的一个检测周期。

LLM 工具会使用配置中的 `openid` 查询余额，返回内容同样受 `query_display_items` 控制。工具以 `FunctionTool` 类形式注册，便于后续扩展更多工具。

自动提醒对同一条规则会做简单去重：余额持续低于阈值时只提醒一次；恢复到阈值以上后再次低于阈值会重新提醒。
