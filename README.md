# 江城创业记蓝图设计

根据指定《江城创业记》存档的科技、配方和工坊条件，为目标物品设计可建造、可供料、可运行的产线。

## 当前状态

Windows 蓝图生成器从用户选定的 Steam 存档只读提取科技、节点、配方和建筑的解锁状态，按已解锁设备、物流吞吐和选填的电力与净空上限搜索布局。离线 HTML 汇总建材、建造胶囊、各进货口日配额、实际带级和设施方向。工作区还保留命令行求解器、算例，以及既有单置换机和双置换机绿瓶蓝图。

[从 GitHub Releases 下载最新版 Windows x64 蓝图生成器](https://github.com/teastares/jiangcheng/releases/latest)

## 文档入口

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 项目协作约定、资料维护和样本处理 |
| [Windows 蓝图生成器](docs/windows-gui.md) | EXE、存档读取、77 个产物预设、配置与 HTML 导出 |
| [蓝图设计指南](docs/blueprint-design.md) | 按存档核对解锁，计算机器、供料、能耗和物流，编制摆放图 |
| [设施四向尺寸表](docs/device-footprints.md) | 安装版各设施的逻辑格、定位格与实际碰撞宽高 |
| [工坊设备功能与支持范围](docs/device-capabilities.md) | 232 项安装版设备的功能分类、解锁和自动规划范围 |
| [产线求解器](docs/solver.md) | 安装、输入格式、排布搜索、空线仿真与验证范围 |
| [求解算例](blueprints/solved/README.md) | 多种产物的共用与独立方案、SVG、建材和日配额 |
| [绿瓶全链蓝图](docs/green-crystal-blueprint.md) | 24 × 22 格的紧凑导入文本、SVG 摆放图、建造清单和 16 分钟日进货量 |
| [双置换机绿瓶蓝图](blueprints/green-crystal-raw-12pm.html) | 可离线打开并导出文本的 HTML，内含摆放图、三个进货口的配额、建材及接电说明 |
| [双绿瓶带级对照](blueprints/optimized/green-crystal-logistics/README.md) | 比较低级混用和仅三级，下载校正朝向后的 200 kW 蓝图 |
| [强力石砖布局优化](blueprints/optimized/powerful-stone-brick/README.md) | 复核地下带、进货、工位和出货动线，提供新版蓝图 |
| [双绿瓶历史布局](blueprints/optimized/README.md) | 对比旧自动、手工与四向重布结果 |
| [研究记录](docs/research.md) | 资料来源、探查范围、已确认规则、资料冲突和待验证问题 |
| [变更记录](changelog/README.md) | 项目发生的变更及实际验证结果 |

## 开始一次设计

提供目标物品与指定存档，可选择原文件或备份，也可提供对应的天机庙节点、已研习科技和 `F11 → 配方` 截图。平台、游戏版本、目标产量、可用地块和允许外供的材料会影响布局。有可用地块截图时，可以按实际障碍、入口和现有线路排布。

产线方案包含建筑和配方、输入输出流量、摆放与朝向、物流与能源接法，以及开工所需的前置条件。交付导入文本时依据同平台、同版本的游戏读写逻辑或分享样本核对格式，并通过游戏内测试记录实际导入和运行结果。 [蓝图设计流程](docs/blueprint-design.md#设计流程)
