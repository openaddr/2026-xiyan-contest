# 荔枝争运战 客户端（Python，基于官方 py-cli-26 适配）

零第三方依赖（仅标准库），满足比赛离线运行要求。

## 目录结构

```
client/
├── start.sh              # 平台启动入口: ./start.sh <playerId> <host> <port>
├── main.py               # 入口：CLI 解析（兼容两种风格）+ 连接重试 + 启动会话
├── selftest.py           # 离线自测（framing + 状态解析 + 策略场景）
├── ../package.sh|.bat    # 打包提交用 ZIP（脚本在仓库根目录）
├── lychee_basic_client/  # 官方基础客户端（gitee openaddr/py-cli-26 原样引入，勿改）
│   ├── framing.py        #   5位长度前缀分帧（半包/粘包处理）
│   ├── messages.py       #   registration/ready/心跳 消息构造
│   ├── session.py        #   ClientSession 基础消息流程
│   └── config.py         #   Config 数据类
├── tests/                # 官方单元测试（python3 -m unittest discover -s tests）
└── lychee/               # 我们的扩展层
    ├── session.py        # StrategySession(官方 ClientSession)：接入状态与策略
    ├── protocol.py       # 全量 action 构造器、规则常量（耗时系数/牌克制表…）
    ├── state.py          # GameState: start/inquire 解析与便捷访问
    ├── world.py          # 地图图结构 + Dijkstra 寻路 + 到站帧数估算
    ├── strategy.py       # Strategy 接口 + BaselineStrategy 基线
    └── log.py            # 日志（logs/client_<id>.log + stderr）
```

## 与官方基础客户端的关系

- `lychee_basic_client/` 原样引入，便于官方更新时直接 diff 合并。
- `lychee/session.py` 的 `StrategySession` 继承官方 `ClientSession`，
  只覆盖 start/inquire/over/error 四个钩子；分帧、消息流程全部复用官方实现。
- 官方基础实现收到 `error` 会直接退出；本客户端按协议第 11 章语义改为
  记录后继续（error 只表示该包未进入结算，如 ACTION_TOO_LATE）。

## 快速使用

```bash
# 离线自测（依赖仓库根目录的 start消息.json / inquire消息.json）
python3 selftest.py
# 官方单元测试
python3 -m unittest discover -s tests

# 连接调测服务端 —— 两种传参风格等价
./start.sh 1001 127.0.0.1 30000                 # 平台位置参数（任务书 10.2）
python3 main.py --host 127.0.0.1 --port 30000 --player-id 1001   # 官方命名参数

# 打包提交（脚本在仓库根目录，跨平台，核心逻辑在 ../package.py）
../package.sh    # macOS / Linux
..\package.bat   # Windows（双击或命令行）
# 生成 仓库根/dist/gameclient.zip：start.sh 在 ZIP 根目录，
# 自动强制 LF 换行 + 写入 0755 可执行位（Windows 原生压缩两者都会丢）
```

Windows 调测包接入：把本目录拷到调测包，把 `调测\client\start.bat` 里的
启动命令换成 `py -3 main.py %1 %2 %3 %4`。

## 分层与迭代点

- **lychee_basic_client / main / session**：通信骨架，一般不用再动。
  决策超 300ms 会告警（服务端默认每帧只等 500ms）。
- **state**：新增字段解析加在这里；动作是否生效要结合 `events[]` + 下一帧状态。
- **world**：寻路成本目前只算帧数；后续可加入鲜度损耗、天气预告、清障残留税的综合成本。
- **strategy**：主要迭代层。基线已覆盖主线（赶路→固定处理→RUSH 验核→交付）+
  机会性冰鉴 + 相邻清障 + 免费强行出牌。TODO：
  - 任务规划（顺路刷任务到基础分累计 ≥90，解锁满额送达分）
  - 窗口博弈（按成本与克制关系选牌）
  - 攻坚/强制通行/设卡对抗
  - 小分队（探路减读条 / 远程清障）
  - 终局急策时机（疾行/护果/破关三选一）

## 注意事项（易踩坑）

- 每帧必须回 action，没动作也要发 `actions: []`（连续 60 帧缺动作直接退赛）。
- `action.round` 必须等于本次 `inquire.round`。
- 不要写死任何节点/路线/阵营，一切以 `start` 下发为准（换图会变）。
- 交付后除 WAIT/空动作外的主动动作每次扣 5 分。
