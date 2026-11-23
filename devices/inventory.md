# 设备清单（真实 / 模拟）

> 本文件由 `scripts/gen_inventory.py` 从 `devices/devices.json` 生成，不要手改。
> 重新生成：`python scripts/gen_inventory.py`；校验：`python scripts/gen_inventory.py --check`

- 受控实体总数：**17**
- 真实设备：**0**
- 模拟实体：**17**
- 其中带危险标签的实体：**6**

危险标签与 `provenance` 只在安全评审后修改：风险判定读取的是这份清单，不是设备自报的属性，所以设备无法把自己降级成“安全”。

| entity_id | 名称 | 域 | 真实/模拟 | 初始状态 | 危险标签 | 命中的危险类别 |
| --- | --- | --- | --- | --- | --- | --- |
| light.living_room_main | 客厅大灯 | light | 模拟 | off | - | - |
| light.living_room_strip | 客厅灯带 | light | 模拟 | off | - | - |
| light.bedroom_main | 卧室主灯 | light | 模拟 | on | - | - |
| light.study_desk | 书房台灯 | light | 模拟 | off | - | - |
| switch.heater_socket | 客厅电暖器插座 | switch | 模拟 | off | danger:high_power | high_power_socket_off(confirm)；high_power_socket_on(confirm) |
| switch.kettle_socket | 厨房电热水壶插座 | switch | 模拟 | off | danger:high_power | high_power_socket_off(confirm)；high_power_socket_on(confirm) |
| switch.humidifier | 卧室加湿器 | switch | 模拟 | off | - | - |
| switch.gas_valve | 厨房燃气电磁阀 | switch | 模拟 | off | danger:gas | gas_supply_close(confirm)；gas_supply_open(deny) |
| switch.main_breaker | 入户总电源空开 | switch | 模拟 | on | danger:main_power | main_power_off(confirm)；main_power_on(confirm) |
| lock.front_door | 入户门锁 | lock | 模拟 | locked | danger:security | door_unlock(confirm) |
| alarm_control_panel.home | 家庭安防主机 | alarm_control_panel | 模拟 | armed_away | danger:security | alarm_disarm(confirm) |
| cover.living_room_curtain | 客厅窗帘 | cover | 模拟 | closed | - | - |
| climate.living_room_ac | 客厅空调 | climate | 模拟 | off | - | - |
| climate.bedroom_ac | 卧室空调 | climate | 模拟 | off | - | - |
| fan.bedroom_fan | 卧室风扇 | fan | 模拟 | off | - | - |
| vacuum.robot | 扫地机器人 | vacuum | 模拟 | idle | - | - |
| media_player.living_room_tv | 客厅电视 | media_player | 模拟 | on | - | - |

## 说明

- `真实` = 通过本地 Home Assistant 控制的物理设备（Wi-Fi/LAN 本地控制）；`模拟` = HA 模拟实体或本项目的内置模拟后端，不接任何真实负载。
- 门锁、燃气电磁阀、总电源空开、大功率插座、安防主机一律标注 `danger:*`，并在演示时保持为模拟实体（方案 R6：危险动作只在虚拟实体上演示）。
- 若 `真实设备` 为 0，则本轮**没有真实设备参与**，README 不得暗示有。
