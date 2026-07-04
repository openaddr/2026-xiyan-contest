# 探索线合并指南（exp/* → 主线，2026-07-04 收盘）

给主线 agent / 用户：探索线今天三轮的成果清单、合并顺序与验收口径。
探索线基座 = 主线 `feat/v3.36-edge-bounty-rescue`（已含 3.34/3.35/3.36），
所以 **直接 merge `exp/v3.93-freeze-slack` 即可拿到全部三轮**，无需逐个。

## 成果清单（按分支）

| 分支 | 内容 | 平台价值 |
|---|---|---|
| exp/v3.91-roadfarmer-corpus | ① RoadFarmerBot 陪练（2986 官道农复刻，reports 三败局形态终于有语料）② FRONT_TEMPO 门控复活（干等实锤门）③ 与主线 v3.35 山路轻门并存合流 | 高——补齐当前平台 meta 的回归电池 |
| exp/v3.92-opening-tempo | 纯证伪档案（零代码）：开局信息墙四次撞击记录、钝规则 -3096 死形 | 防未来 agent 重蹈覆辙 |
| exp/v3.93-freeze-slack | 尾随冻结预算（30 帧，纯农豁免） | 高——roadfarmer 12/12、双翻 -67/-52→+700 |

## 合并时注意

1. **版本号**：探索线用 3.9x 号段（BUILD_VERSION="3.93-exp-freeze-slack"）。
   合入主线时按主线当前序列改号（version.py 三个条目 3.91/3.92/3.93 的
   注释同步改），代码内注释标记（V3.91/V3.93 字样）可保留或批量替换。
2. **conflict 热点**：`planner.py`（FRONT_TEMPO 常数区 + `_front_tempo_*`
   函数族 + plan() 的 margin 计算）、`version.py`、`selftest.py`（前段
   尾随钉子组被重写过）。若主线 3.37+ 又动了这些区域，冲突解法参考
   exp/v3.91 的合流提交 93bbb11（山路轻门用 `_front_tempo_contested_raw`，
   全量门用形态门控版——两门并存的关键）。
3. **smoke.py**：探索线加了 roadfarmer 0/5/8/11 哨兵（0/11 是硬哨兵，
   回归即红灯）。合并后 KNOWN_LOSS 只应剩 camper19。
4. **合并后验收**（快速口径）：`python3 selftest.py`（302 项）→
   `python3 smoke.py`（26 局全绿）→ 若动过 FRONT_TEMPO/margin 相关，
   加跑 roadfarmer 12 种子（期望总 margin ≈7981、12/12 全胜）。

## 已知代价（决策时已权衡，合并方知情即可）

- 镜像配对净 -255（集中 seed2/-257）：冻结预算在自博弈里放大相位差
  （尾随方早动身、领跑方兑现更大）。对外部对手无此效应。
- camper 4/23 深链未交付仍在（V3.21 起的历史观察项，本线未动）。

## 探索线未决（留给后续轮次）

按价值排序见记忆/version.py：镜像 seed2 暴露判定细分 → 冻结预算
期望制 → 记分牌意识 → farmer 双定义统一 → 根因 B。
