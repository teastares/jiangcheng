# 产线求解算例

按每天 16 分钟计算，入选布局取自程序搜索的候选。

| 产物及配额 | 稳态理论日产 | 共用上游 | 独立供料 | 入选 |
| --- | ---: | ---: | ---: | --- |
| [绿色晶能](green-crystal.md) | 192 | [52 × 17](green-crystal-shared.html) | [31 × 20](green-crystal-separate.html) | 独立供料 |
| [绿色晶能（200 kW 上限）](green-crystal-200kw.md) | 192 | [40 × 23](green-crystal-200kw-shared.html) | 未找到 | 共用上游 |
| [布](cloth.md) | 320 | [19 × 5](cloth-shared.html) | [14 × 6](cloth-separate.html) | 独立供料 |
| [初级工具](basic-tools.md) | 192 | [19 × 6](basic-tools-shared.html) | [14 × 4](basic-tools-separate.html) | 独立供料 |
| [加强木板](reinforced-wood.md) | 320 | [21 × 7](reinforced-wood-shared.html) | [14 × 8](reinforced-wood-separate.html) | 独立供料 |
| [加固铁板](reinforced-iron.md) | 320 | [17 × 5](reinforced-iron-shared.html) | [14 × 4](reinforced-iron-separate.html) | 独立供料 |
| [建造胶囊](capsules.md) | 384 | [37 × 8](capsules-shared.html) | [18 × 8](capsules-separate.html) | 独立供料 |

方案详情含离线 HTML、SVG、格位 JSON、逐口配额和建材。适用 Steam 的参数表同时生成导入文本。启用仿真时也记录空线启动后的产量。

[求解器使用与验证边界](../../docs/solver.md)
