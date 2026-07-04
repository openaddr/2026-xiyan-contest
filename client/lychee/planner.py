"""任务规划器：任务估值 + 途经点选择 + 交付截止硬约束。

分数模型（任务书 7.2，已交付口径）：
  送达基础分 = min(240, 120 + floor(基础分 × 4/3))     -> 基础分 90 拿满
  皇榜任务分 = min(180, 基础分 + 里程碑(60:+15, 90:+35, 110:+50))
  用时分     = floor(原始用时分 × min(基础分, 90) / 90)
90 以下每 1 点基础分的综合边际收益约 3 分，90 以上衰减为 1~1.5 分，
所以策略档位为「保 90 冲 110+」：3 个 30 分任务锁死，之后净收益为正才接。
"""
import math

from . import protocol as P

# ---- 可调参数 ----
SAFETY_MARGIN = 60          # 交付截止安全余量（帧）
# RUSH 后余量分档（V3.26）：60 的余量吸收的是"未建模延误"（敌卡/窗口/
# 汇聚等待），进入 RUSH 后这些方差大头已经落定，继续按 60 算等于把
# 尾段顺路任务整档熔断——reports 局 vs2986 在 S12/S13 跳过两个 5 帧
# 读条的 15 分任务（T_021/T_023），终局实剩 38 帧，那 20 分恰好大于
# 18 分的败差。25 = 验核 6 + 交付 2 + 一次窗口/休整级意外的量级
RUSH_SAFETY_MARGIN = 25
FREEZE_RISK_BUDGET = 30     # 尾随冻结预算（V3.93）：领先的活对手可在我方
                            # 前路任意节点回手卡，一次冻结 = 首风化 30~45 帧
                            # 起步；纯农画像豁免，见 _rear_freeze_exposed。
                            # 强度扫描（30/45/60）：rf0/rf11 翻盘三档全稳
                            # (+700)；toller5 三档 +35/+19/+15——取 30 保住
                            # 2839 形态零劣化（camper5 的 45 档 +89 额外
                            # 收益为此舍弃）；镜像成本三档同在（机制固有）
RUSH_EARLIEST = 390         # 宫宴冲刺最早可能触发帧（任务书 6.5）
GATE_VERIFY_FRAMES = 6      # 宫门验核处理帧数
DELIVER_FRAMES = 2          # 到终点 + 交付
FRESH_VALUE_PER_FRAME = 0.11   # 每帧鲜度损耗折分 ≈ 0.055(官道) × 1.8(分/鲜度) + 阈值摊销
TIME_SCORE_PER_FRAME = 70.0 / 600.0   # 用时分斜率（任务系数拉满时）
RAW_TIME_SCORE_EST = 25     # 估算用的原始用时分（约 385 帧交付）
CONTEST_RISK_DISCOUNT = 0.5  # 对手比我们更近时的估值折扣
# 争夺宽限带（V3.22 实验，已证伪，保留旋钮与记录）：反事实统计
# （contest_truth，36 局 1900+ 样本）证明 0.5 作为概率大错——预测落后
# ≤4 帧硬抢真实胜率 100%（三形态无一例外）、5~10 帧 96~99%（opp_eta
# 是不含对手停留的裸 ETA + 对手多半无意图，双重偏差）。但 840 局扫描
# 证明它作为政策歪打正着：G=4 五形态逐位零变化（小差距折半从不翻转
# argmax）；G=10/20 全线崩坏（camper 42→38、toller 48→42、farmer
# 24→22）——去抢对手身边的任务赢面虽大，却把走廊到达拖后 6~10 帧，
# 悬崖帧价 25~35 分/帧远超任务边际值。0.5 的"悲观"实际在给估值体系
# 没显式定价的"贴身绕路外部性"买单。真要动它，先给走廊外部性建模
CONTEST_GRACE_FRAMES = 0     # 0 = 现行行为（任何落后都吃满折扣）
CONTEST_GRACE_DISCOUNT = 0.9
# 分段争夺折扣（V3.23）：反事实按博弈阶段切分（48 局 2000+ 样本）后
# 发现外部性完全集中在关前——关后所有 gap 桶硬抢真实胜率 97~100%
# （走廊已过，没有悬崖可输，输了只亏一趟路），对手已交付后更是规则上
# 不可能被抢（7.4 + 交付队伍跳过主动作）。0.5 只该活在关前竞速段
POST_CHOKE_CONTEST_DISCOUNT = 0.9   # 关后：P≈0.98 × 湿件谦逊
CONTEST_PHASE_ENABLED = True
# 前推偏置（V3.24，用户指令：前期节点降权、优先冲走廊、后面的资源更
# 重要）：任务价值 × 地图进度系数——progress = 1 - 该点到宫门裸帧 /
# 起点到宫门裸帧，系数 = FLOOR + (1-FLOOR)×progress。语料依据：2839
# 边冲边农（S07/S10/S11 沿途农同样的 150 分），我们 S03 停留 15 帧在
# 悬崖带射程（ETA≤125）之外，seed4/23 深链死局皆源于开局段落后。
# 只作用于任务：资源口径不动（冰链血泪资产、马匹 T06 经济）
FORWARD_BIAS_FLOOR = 1.0    # 手动全局档（1.0=关）。全局开被 1344 局扫描
                            # 证伪（camper 相位骰子），保留旋钮供实验
FORWARD_BIAS_CUT = 0.0      # >0 时改用阶跃：进度 < CUT 的节点吃 FLOOR，
                            # 之后完全不动（只压真正的开局簇，不扰动
                            # 走廊邻近农任务的时序）
FORWARD_BIAS_AUTO = 0.6     # 冲锋型对手在线识别命中时的地板（strategy.
                            # _fwd_rush_tick 按位置/行为置 forward_rush_opp，
                            # 不认对手 ID——地图会变对手会变，用户纠偏）
# 前段推进保护（V3.28）：reports vs2986/vs2738 同构败因不是某个任务本身
# 亏，而是 S03 原地/邻接任务 + 山线资源包把路线承诺拖进 S06/S08，
# 对手则跳过 S03，在 S07/S10 官道沿途补齐 150 分并早 55+ 帧交付。
# 对手只先走几帧时不该改道认怂，而是尾随主路；所以这里只拦低进度/
# 山线停留，不拦官道跟随。过了中段后硬件资源价值上调，补回前面放弃的
# 非关键经济。V3.32 审计：陪练电池无 2986 官道型代表曾实测净负关闭；
# V3.35 恢复山路轻门（平台 lose 批次最稳定负样本：已有基础分后被
# S06/S08 山路拖走）；V3.37 走廊跟随轻门（平台 15 局 6 败：对手已提交
# 走廊后仍为眼前任务反切另一条线——按 routeEdge/站位证据拦离站反切，
# 水路不作跟随证据）；V3.91 补齐 RoadFarmerBot 语料后复证全量门：
# 对官道农形态收益决定性（12 种子总 margin 5130→6305、交付
# r515-585→r475-490），亏损集中在 camper/rusher/toller 误开火 →
# 按"farmer 模式+零设卡+富点干等实锤"门控启用。多门并存：全量门
# 只对干等实锤纯农开火；各轻门覆盖其余形态（一律用 raw 竞争判定，
# 不吃形态门）
FRONT_TEMPO_ENABLED = True
FRONT_TEMPO_MOUNTAIN_RECOVERY = True
FRONT_TEMPO_PROGRESS_CUT = 0.22
FRONT_TEMPO_BASE_CAP = 90
FRONT_TEMPO_OPP_LEAD = 3
FRONT_TEMPO_BLOCK_ROUTE_TYPES = {P.MOUNTAIN}
FRONT_TEMPO_KEEP_LEAD_TRAIL = -30
FRONT_TEMPO_KEYPASS_BASE_CAP = 120
FRONT_TEMPO_KEYPASS_TASK_ROUTES = {P.ROAD}
# 富点干等实锤（V3.91）：2986 型在普通任务点纯等刷新波（零读条闲置），
# 2839/toller 教义"农不停步"、camper 只蹲关隘——普通节点累计闲置帧是
# 两者唯一的行为分水岭。strategy._profile_tick 逐帧累计（停留 ≥5 帧后
# 的无读条帧才计，滤过路噪声），过线才解锁 FRONT_TEMPO 尾随
FRONT_TEMPO_DWELL_MIN = 30
FRONT_TEMPO_CORRIDOR_FOLLOW = True
FRONT_TEMPO_CORRIDOR_BASE_CAP = 150
FRONT_TEMPO_CORRIDOR_ROUTES = {P.ROAD, P.MOUNTAIN}
FRONT_TEMPO_CORRIDOR_NODES = {
    "S04": P.WATER, "S05": P.WATER,
    "S06": P.MOUNTAIN, "S08": P.MOUNTAIN,
    "S07": P.ROAD, "S09": P.ROAD,
}
FRONT_TEMPO_HEAVY_MOUNTAIN_GUARD = True
FRONT_TEMPO_HEAVY_MOUNTAIN_BASE_CAP = 120
FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_BASE = 30
FRONT_TEMPO_HEAVY_MOUNTAIN_MAX_OPP_LEAD = 20
FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_LEG = 45
FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_DETOUR = 28
LATE_RESOURCE_PROGRESS = 0.62
LATE_RESOURCE_MULT = 1.25
ICE_AMMO_TARGET_BAD = 1
ICE_PRE_AMMO_VALUE_MULT = 0.72
BREAK_GUARD_EXPECT_DEFENSE = 6
BREAK_SHADOW_CHOKE_MULT = 0.35
BREAK_FUNNEL_TAX_MULT = 0.45
# 阻挡节点的寻路惩罚按真实处理代价估：会计入 ETA，不能虚高
OBSTACLE_PENALTY = 10       # 清障 6 帧读条 + 1 好果 / 强通税 8 帧
GUARD_PENALTY = 35          # 强通时间税 min(40, 10+防守值×5) 量级

# 对手阴影惩罚（V3.1 走廊竞争）：对手会先于我们到达的节点，资源会被扫空、
# 设卡会掐点出现（四局回放实锤：落后 60~80 帧走同一走廊 = 全程吃陷阱）。
SHADOW_CHOKE_PENALTY = 35   # 咽喉类节点（对手大概率回手设卡）
# 阴影×漏斗去重（V3.27）：V3.16 起漏斗模型已按到达时机给"咽喉被卡"
# 精确定价（死等+税×先验），V3.1 时代的阴影咽喉罚是它的粗粒度前身——
# 同一个恐惧在官道武关上收两遍钱（35 帧寻路罚 + 漏斗税），reports 局
# r99 岔路把官道冤枉成山线的决定性楔子。去重后 camper 世界仍有
# prior 1.0 的漏斗税全额保护；被漏斗定价覆盖的咽喉不再叠阴影罚
SHADOW_FUNNEL_DEDUP = True
SHADOW_NODE_PENALTY = 8     # 普通节点（资源被扫、任务被先手）
SHADOW_MARGIN = 5           # 到达时间差超过该值才算被抢先
# 阴影咽喉不含 GATE：宫门验核双方都要排队，实战对手从未卡宫门；
# 尾段(S11-S14)双走廊共用，过重的阴影只会虚压 slack 不改变路线
CHOKE_TYPES = {"KEY_PASS", "PASS", "MOUNTAIN_PASS"}
FORECAST_HORIZON = 120      # 预告天气只影响这个时间窗内的路段估价

# 资源提货目标价值（V3.2）：冰鉴 +10 鲜度 = 18 分，按 17 计（贴 100 上限有损耗）。
# 败局实锤：3 个冰鉴全在陆路，水路竞速交付快 25 帧却输 27 分鲜度。
# 冰鉴 19 = +10鲜度×1.8×0.94(贴上限损耗) + ~2.5(少跌破一个转坏阈值≈1.9分/篓)
RESOURCE_VALUES = {P.ICE_BOX: 19.0, P.FAST_HORSE: 4.0, P.SHORT_HORSE: 3.0}
CLAIM_FRAMES = 2            # 资源领取读条
# 对手到资源点的竞争折扣
SWEEP_DISCOUNT = 0.15       # 对手明显先到：大概率被扫空
CONTEST_DISCOUNT = 0.55     # 五五开：窗口期间双方同冻结，时间近乎免费

# V3.3 鲜度竞赛：
# 拒止倍率——资源在对手前进路线上时，抢到 = 我 +17 且对手 -18（双向摇摆）
DENIAL_FACTOR = 1.5
# 链式加成——目标点通往宫门路上的其他资源按半权计（赢下 S03 后 S07 顺路白拿）
CHAIN_WEIGHT = 0.5
# 不在对手路线上的资源，竞争折扣下限（对手专程绕过来的概率低；
# 六局回放里山冰 S06 在对手走官道时从未被碰，V3.10 由 0.7 上调）
OFFPATH_RACE_FLOOR = 0.85
# 路线鲜度定价——每帧鲜度损耗超出停靠基准(0.05)的部分折算成等效帧数：
# 山路 0.07 → ×1.16，支路 ×1.12，官道 ×1.04，水路 ×0.96（对败局：山路捡冰
# 的绕路成本被低估 15%，省下的冰又漏在路上）
_FV = FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME
ROUTE_FRESH_FACTOR = {
    rt: 1.0 + (decay - P.IDLE_FRESH_DECAY) * 1.8 / _FV
    for rt, decay in P.ROUTE_FRESH_DECAY.items()
}
# 天气对鲜度的区域加成（任务书 2.5）：暴雨命中水路 ×1.3；酷暑全图 ×1.5
WEATHER_FRESH_REGION = {("HEAVY_RAIN", P.WATER): 1.3}

# 回头迟滞（V3.8）：刚离开的节点作为目标首跳的附加帧数与窗口期
BACKTRACK_PENALTY = 25
BACKTRACK_WINDOW = 40

# 刷新流期望（V3.27，reports 局遗留观察项）：估值只给当前可见任务定价，
# 热点节点（S07 型，整局 5 波刷新）的未来任务流在走廊决策时一分不值，
# 而对手的 150 分正来自蹲波次。模型：节点刷新率 =（本局已观测刷新数 +
# 候选点静态先验份额）混合，乘"我们在该节点附近可吃到的窗口"（目标点
# 本身给驻留信用，沿途点给路过信用），按边际任务分与竞争折扣折算。
# 刻度诚实注记：本图两条走廊的原始刷新数几乎打平（山 8 vs 官 7），
# 本机制的价值在"该不该为等波次多留一手"而非扳走廊——扳走廊靠
# SHADOW_FUNNEL_DEDUP 去重
REFRESH_DWELL_CREDIT = 25   # 目标节点的驻留窗口信用（帧）
REFRESH_PASS_CREDIT = 10    # 沿途节点的路过窗口信用（帧）
REFRESH_CLAIM_PROB = 0.35   # 到点竞争 + 刷新时机不确定的折扣
REFRESH_VALUE_CAP = 30.0    # 单次估值的刷新流加成上限（一个任务）
REFRESH_RATE_FLOOR_ROUND = 120   # 观测分母下限（开局样本太少时防爆）

# 目标粘性（V3.18）：每帧 argmax 重规划在两个净值接近的目标间会震荡
# （回头迟滞只防物理折返，不防目标层面的反复横跳）。换目标要求新净值
# 超出当前承诺目标 15%——迟滞带宽小于任何一次真实的估值翻转（对手抢先
# 的竞争折扣 ×0.5、漏斗税差 ±30 帧都远大于 15%），只滤掉浮点级抖动
SWITCH_MARGIN = 1.15

# 竞速模式（V3.18，audit 缺口 1 的修复）：双方到下一关键关隘的裸 ETA 差
# 在竞争带内时，每晚 1 帧都在提高"输掉漏斗竞速"的概率——输 = 死等 +
# 满防税（45~80 帧）起步，尾部是 replay20/36 的 195 帧冻死。平时价
# 0.227 分/帧会批准所有账面为正的小绕路，把走廊先手一口口让出去。
# 倍率校准：漏斗的死等/税差已由 _funnel_delta 单独按时机定价，这个倍率
# 只补"领先的附加值"——设卡权、刷新先手、免陷阱（audit 缺口 1 里
# funnel 模型覆盖不到的部分），不能重复计满防税。2.5× 实测会把开局
# 冰链（败局13：快 25 帧输 27 分鲜度）整个砍掉，回退到 1.75×：
# 33 帧级的冰链绕路仍然放行（19 分 > 13.1），10 分以下+15 帧级的
# 边际小目标出局（10 < 15×0.397=6 的两倍附近开始被压）。
# 进入/退出条件全部公开可算：过完咽喉（前方无 KEY_PASS）或差距拉开自然退出
RACE_BAND = 25
RACE_FRAME_MULT = 1.75
# 悬崖带（V3.21）：竞速带内且关隘已近时，帧价从边际损耗切换为悬崖斜率。
# 立项证据：随机化 camper 四个结构性死局里 10/15/23 完全同构——S07 一停
# （两个 30 分顺路任务，~14 帧）把走廊进入从"同帧进边"变成"落后 18~20
# 帧"，赢局画像（+165 均值）翻成 -326~-586。漏斗竞速是悬崖函数：落后
# ≤4 帧（对手设卡读条）仍安全免疫，多 1 帧就是死等+满防税起步（6.2.1）。
# 死局实测斜率：胜负画像差 500~700 分摊在 ~20 帧的顺路停留上 ≈ 25~35
# 分/帧，取中值 30。刻度校验：顺路 30 分任务的边际值高达 93~99（跨里程
# 碑 + 任务系数抬用时分），4 帧读条 ≈ 23 分/帧——悬崖价必须高于它才咬
# 得动（首版取 10 被实测证伪：cliff=1 时任务照领，死局原样复现）。
# 带外一切照旧，资源目标口径（race_adjust=False，冰链血泪资产）不动。
RACE_CLIFF_ETA = 125        # 咽喉 ETA 在此内才算"近"（S07 决策点 ~119；
                            # 更远处未来方差主导，一帧不构成悬崖信息）
RACE_CLIFF_LEAD = 10        # 领先出安全垫（读条 4 + 余量）后不抢：带内
                            # 领先方顺路任务是把领先烧成落后的第一步，但
                            # 领先 >10 帧时 6 帧任务翻不了盘
RACE_CLIFF_TRAIL = 60       # 落后侧悬崖延伸：落后度量有锚点漂移 + t_o
                            # 裸 ETA 双重偏差（~55 帧级），且对手没起卡
                            # 前门就没关；真落到 60 外基本追不回，转农
RACE_CLIFF_OPP_FARM = 30    # 对手在途 taskScore ≥ 此值 → 它一路在农任务
                            # 不是在抢关，悬崖不成立（行为证据，与 V3.18
                            # 出牌频率画像同级；A/B 实测不加这道门 farmer
                            # 局 48/48→42/48、镜像均分 -53——弃经济抢一场
                            # 不存在的竞速。语料里的脚本抢关者到关前分数恒 0）
RACE_CLIFF_FRAME_VALUE = 30.0
# 边农边冲压力（V3.24/V3.31）：平台 2986/2738 不是纯 rusher，也不是
# 纯 farmer，而是官道高速农到 120/150 后继续贴宫门推进。该信号现在
# 收敛到 _opp_tempo_mode()，避免同一份在途任务分证据一边关闭悬崖、
# 一边打开前推偏置。
FARM_RUSH_TASK = 90
FARM_RUSH_GATE_ETA = 300
FARM_RUSH_GATE_MARGIN = 10
FARM_RUSH_PROGRESS_EPS = 4
FARM_RUSH_MOUNTAIN_PENALTY = 45

# 尾段任务底线：90->120 的综合边际约 45 分，强过领先 6~10 帧直接送。
# 只在过完第一道 KEY_PASS 后给近身/顺路任务开绿灯；关前补分会把走廊
# 先手烧掉（toller seed12 复盘），仍交给常规估值。
TASK_FLOOR_BASE = 120
TASK_FLOOR_MIN_BASE = 90
TASK_FLOOR_MAX_FRAMES = 16
TASK_FLOOR_BONUS = 80.0

# 悬崖带共点对峙豁免（V3.26，reports 局 vs2619 实锤）：双方同帧停靠在
# 同一任务点时，悬崖的前提（"我停它不停 → 我落后进漏斗"）不成立——
# 它也停下农，竞速对称；它不停，任务归我们是纯拒止（+我 −它双向摆幅）。
# vs2619：r167 双方同到 S07，桌上三个 30 分任务，悬崖价 30/帧把它们
# 全砍（4 帧读条 = 120 > 净值 90），对手留场连吃三个 90 分，而它整局
# 零设卡——我们抢赢的漏斗没有过路费，终局 -8（里程碑差 -45）。
# 豁免边界（V3.26.1 收紧，camper seed15 A/B 抓获）：只豁免脚下
# （pos == 锚点）+ 普通节点（关隘/宫门同桌 = 蹲点预备式）+ 对手已
# 停靠在同一节点（"将至 ETA≤10"版被开局汇聚窗口反噬：camper 分 0
# 未落卡时与我们同桌，一停即 -410 未交付）；见过对手设卡或画像为
# camper 时不豁免——"停一手被掐"死局全部来自会设卡的对手
CLIFF_MELEE_EXEMPT = True

# 漏斗定价（V3.16）：全图汇于关键关隘（武关 S10 类），谁后到谁挨卡。
# 对手先到时，我们过漏斗的真实代价随"到达时机"剧烈变化（replay57/60 实测）：
# - 赶在它到位前过完边：0（设卡必须人到，规则 6.2.1 免疫）
# - 到得太早（它还没到，但我们抢不完边）：死等它到位+设卡，再吃满防强通税
#   （replay36/56/57：山路正好撞进这个窗口，死等 21~52 帧 + 税 45+）
# - 它刚过就跟上：满防税 45 + 窗口摊销（replay60 水路：死等 0）
# - 来得很晚：卡已风化，税逐级递减甚至为 0
# 所有数字来自规则：读条 4 帧、KEY_PASS 满防首次风化 45 帧、之后每 30 帧 -1、
# 税 min(50, 15+5×防守)。唯一近似：对手 ETA 用裸帧（不含它的途中停留），
# 已在语料上验证方向正确（57 山路 vs 60 水路的实付代价排序一致）。
FUNNEL_GUARD_READ = 4
FUNNEL_FIRST_WEATHER = 45
FUNNEL_WEATHER_GAP = 30
FUNNEL_WINDOW_OVERHEAD = 8      # 强通 PASS 窗口 + 可能的休整摊销
FUNNEL_GUARD_PRIOR = 0.7        # 首卡出现前的先验（语料 6/11 局走廊领跑者卡漏斗，
                                # L4 系 demo 5/5；见过对手设卡后升为 1.0）
# farmer 先验下行（V3.26）：0.7 只升不降是单向棘轮——reports 三败局
# 对手全程农任务零设卡，我们仍按 0.7 给共用走廊计漏斗税，把自己推向
# 山线（刷新密度低 + S06→S08→S10 长边节奏税）。对手画像为 farmer
# （在途任务分 ≥60 且全场未见其设卡）时先验降档；它一旦落卡，
# _guard_seen 粘性升 1.0，本值即被覆盖，2839 防御不拆
FUNNEL_FARMER_PRIOR = 0.35
# 差值截断只作用于"税差"部分（税依赖对手是否真设卡，有模型不确定性）；
# "死等差"部分不截断——它是纯几何：到得早又抢不完边就必须等卡出生
# （规则 6.2.1 + 防冻结天条推导，与对手意愿无关）
FUNNEL_TAX_DELTA_CAP = 30.0
# 竞速不确定带：过边完成时刻与对手到位时刻差在 ±15 帧内时按线性概率折算
# ——开局双方等距（出口≈t_o）本质是五五开（S02 窗口决定），二值判定会让
# 模型坐在边界上被浮点抖动摆布，把开局所有小目标一刀切杀掉
FUNNEL_RACE_BAND = 15.0

# 破关悬赏（V3.12）：只在落后时追（任务书 6.3.3——攻破方总分需低于设卡方才计分，
# 领先时打了也白打）。只追一击必破的目标（好果坏果各至多 2 篓的单次攻坚上限），
# 车轮战式蹲点强拆留给"挡路时顺手打"的既有逻辑，不在这里专程绕路。
BOUNTY_GOOD_RESERVE = 5     # 与 strategy.PlannerStrategy.MIN_GOOD_RESERVE 保持一致
BOUNTY_MAX_DEFENSE = 2 * 2 + 2 * 3   # 好果2*2 + 坏果2*3 的单次攻坚上限
# rewardScore 直接读公开字段，不叠加"首次悬赏+20"的完成加成——该加成是否已经
# 计入 bountyScore 展示口径不确定，宁可低估，不要在悬赏值上叠加一个可能重复计算的假设


def milestone_bonus(base):
    if base >= 110:
        return 50
    if base >= 90:
        return 35
    if base >= 60:
        return 15
    return 0


def task_component_score(base, raw_time_score=RAW_TIME_SCORE_EST):
    """给定任务基础分累计，「送达 + 任务 + 用时」三项合计（已交付口径）。"""
    delivery = min(240, 120 + base * 4 // 3)
    tasks = min(180, base + milestone_bonus(base))
    time_score = math.floor(raw_time_score * min(base, 90) / 90)
    return delivery + tasks + time_score


def marginal_task_value(base, score, raw_time_score=RAW_TIME_SCORE_EST):
    """再完成一个 score 分任务的边际综合收益。"""
    return (task_component_score(base + score, raw_time_score)
            - task_component_score(base, raw_time_score))


class Plan:
    """kind: 'task' 做任务 / 'resource' 提资源 / 'deliver' 直奔交付线 / 'hold' 原地。"""

    __slots__ = ("kind", "task", "position", "detail", "slack", "resource")

    def __init__(self, kind, task=None, position=None, detail="", slack=0,
                 resource=None):
        self.kind = kind
        self.task = task          # 任务实例 dict
        self.position = position  # 执行任务/领取资源应停靠的节点
        self.detail = detail
        self.slack = slack        # 交付截止余量（帧），负数=已进入抢救模式
        self.resource = resource  # kind='resource' 时的资源类型

    def __repr__(self):
        tid = self.task.get("taskId") if self.task else None
        return (f"Plan({self.kind}, task={tid}, res={self.resource}, "
                f"pos={self.position}, slack={self.slack}, {self.detail})")


class TaskPlanner:
    def __init__(self, logger=None):
        self.log = logger
        # 可调常数镜像为实例属性（V3.18）：镜像自博弈 A/B 时按实例覆盖，
        # 模块全局会同时改到对局双方。语义与模块级默认值完全一致
        self.RACE_BAND = RACE_BAND
        self.RACE_FRAME_MULT = RACE_FRAME_MULT
        self.RACE_CLIFF_ENABLED = True
        self.RACE_CLIFF_ETA = RACE_CLIFF_ETA
        self.RACE_CLIFF_LEAD = RACE_CLIFF_LEAD
        self.RACE_CLIFF_TRAIL = RACE_CLIFF_TRAIL
        self.RACE_CLIFF_OPP_FARM = RACE_CLIFF_OPP_FARM
        self.RACE_CLIFF_FRAME_VALUE = RACE_CLIFF_FRAME_VALUE
        self.SWITCH_MARGIN = SWITCH_MARGIN
        self.FUNNEL_GUARD_PRIOR = FUNNEL_GUARD_PRIOR
        self.OFFPATH_RACE_FLOOR = OFFPATH_RACE_FLOOR
        self.CONTEST_RISK_DISCOUNT = CONTEST_RISK_DISCOUNT
        self.CONTEST_GRACE_FRAMES = CONTEST_GRACE_FRAMES
        self.CONTEST_GRACE_DISCOUNT = CONTEST_GRACE_DISCOUNT
        self.POST_CHOKE_CONTEST_DISCOUNT = POST_CHOKE_CONTEST_DISCOUNT
        self.CONTEST_PHASE_ENABLED = CONTEST_PHASE_ENABLED
        self.SAFETY_MARGIN = SAFETY_MARGIN
        self.RUSH_SAFETY_MARGIN = RUSH_SAFETY_MARGIN
        self.FREEZE_RISK_BUDGET = FREEZE_RISK_BUDGET
        self.CLIFF_MELEE_EXEMPT = CLIFF_MELEE_EXEMPT
        self.FUNNEL_FARMER_PRIOR = FUNNEL_FARMER_PRIOR
        self._choke_ahead_cache = (-1, False)
        self.FORWARD_BIAS_FLOOR = FORWARD_BIAS_FLOOR
        self.FORWARD_BIAS_CUT = FORWARD_BIAS_CUT
        self.FORWARD_BIAS_AUTO = FORWARD_BIAS_AUTO
        self.FRONT_TEMPO_ENABLED = FRONT_TEMPO_ENABLED
        self.FRONT_TEMPO_MOUNTAIN_RECOVERY = FRONT_TEMPO_MOUNTAIN_RECOVERY
        self.FRONT_TEMPO_PROGRESS_CUT = FRONT_TEMPO_PROGRESS_CUT
        self.FRONT_TEMPO_BASE_CAP = FRONT_TEMPO_BASE_CAP
        self.FRONT_TEMPO_OPP_LEAD = FRONT_TEMPO_OPP_LEAD
        self.FRONT_TEMPO_KEEP_LEAD_TRAIL = FRONT_TEMPO_KEEP_LEAD_TRAIL
        self.FRONT_TEMPO_KEYPASS_BASE_CAP = FRONT_TEMPO_KEYPASS_BASE_CAP
        self.FRONT_TEMPO_DWELL_MIN = FRONT_TEMPO_DWELL_MIN
        self.FRONT_TEMPO_CORRIDOR_FOLLOW = FRONT_TEMPO_CORRIDOR_FOLLOW
        self.FRONT_TEMPO_CORRIDOR_BASE_CAP = FRONT_TEMPO_CORRIDOR_BASE_CAP
        self.FRONT_TEMPO_HEAVY_MOUNTAIN_GUARD = FRONT_TEMPO_HEAVY_MOUNTAIN_GUARD
        self.LATE_RESOURCE_PROGRESS = LATE_RESOURCE_PROGRESS
        self.LATE_RESOURCE_MULT = LATE_RESOURCE_MULT
        self.ICE_AMMO_TARGET_BAD = ICE_AMMO_TARGET_BAD
        self.ICE_PRE_AMMO_VALUE_MULT = ICE_PRE_AMMO_VALUE_MULT
        self.BREAK_GUARD_EXPECT_DEFENSE = BREAK_GUARD_EXPECT_DEFENSE
        self.BREAK_SHADOW_CHOKE_MULT = BREAK_SHADOW_CHOKE_MULT
        self.BREAK_FUNNEL_TAX_MULT = BREAK_FUNNEL_TAX_MULT
        self.forward_rush_opp = False    # strategy 在线识别结论
        self._fwd_total = None
        self.SHADOW_CHOKE_PENALTY = SHADOW_CHOKE_PENALTY
        self.SHADOW_FUNNEL_DEDUP = SHADOW_FUNNEL_DEDUP
        self.REFRESH_CLAIM_PROB = REFRESH_CLAIM_PROB
        self.REFRESH_VALUE_CAP = REFRESH_VALUE_CAP
        self._task_seen = set()      # 已见 taskId（刷新观测）
        self._spawn_count = {}       # nodeId -> 本局已观测刷新数
        self._last_choke = None      # 最近一次漏斗 ctx 的咽喉（去重用，容一帧滞后）
        self.CHOKE_PASS_FALLBACK = True   # 潼关回退（V3.20），A/B 可关
        self.blacklist = {}   # taskId -> 解禁帧（吃到拒绝后临时拉黑）
        # 对手画像（V3.20，strategy 每帧写入）："camper" 时漏斗先验提前升 1.0
        # ——不必等它第一张卡落地。立项依据：camper 局扫描里 RACE_FRAME_MULT/
        # SWITCH_MARGIN/FUNNEL_GUARD_PRIOR 三参数拨动同一个漏斗前分叉，最优
        # 方向与镜像局相反 → 参数无全局最优，须按对手风格分档
        self.opp_profile = "unknown"
        self._shadow_cache = (-1, frozenset())  # (round, 被对手抢先的节点集)
        self._opp_path_cache = (-1, frozenset())  # (round, 对手前进路线节点集)
        self._guard_seen = False       # 对手本局设过卡（漏斗先验升为 1.0，粘性）
        self._opp_dwell_idle = 0       # 对手在普通节点的累计干等帧（V3.91，
                                       # strategy._profile_tick 写入）
        self._funnel_cache = (None, None)  # ((round, cur), (choke, t_o, prior, toll_direct))
        self._race_cache = (-1, False)     # (round, 竞速模式是否激活)
        self._cliff_cache = (-1, False)    # (round, 悬崖带是否激活)
        self._cliff_choke = None           # 悬崖带激活时的咽喉节点
        self._committed = None             # 目标粘性：当前承诺目标的键
        # 回头迟滞（V3.8）：刚离开的节点在窗口期内作为目标首跳要付额外代价。
        # replay25：走廊总价近似打平让 65 帧真实折返在绕路公式里"免费"，
        # S03→S02→S04 的回头使我们晚 70 帧到 S09，正好撞上对手设卡循环。
        self.back_node = None
        self.back_until = -1

    # ================= 对外入口 =================

    def plan(self, state):
        me = state.me
        self._observe_spawns(state)
        cur = self._anchor_node(state)
        if not cur:
            return Plan("hold", detail="no position")
        penalty = self._penalty_fn(state)
        ecost = self._edge_cost_fn(state)
        speed = state.my_speed()
        g = state.graph

        # 交付截止用真实时间成本；方案比较用价值成本（鲜度/阴影定价）。
        # 混用会自己吓自己：真实地图开局曾估出 slack=-26 直接熔断所有目标。
        to_gate_t, _ = g.shortest_path(cur, state.gate_node, speed,
                                       self._time_penalty_fn(state),
                                       self._time_edge_cost_fn(state))
        to_gate, _ = g.shortest_path(cur, state.gate_node, speed, penalty, ecost)
        gate_to_term, _ = g.shortest_path(state.gate_node, state.terminal_node, speed)
        eta_direct = to_gate_t + GATE_VERIFY_FRAMES + gate_to_term + DELIVER_FRAMES
        # 余量分档（V3.26）：RUSH 后未建模方差已落定，改用小余量放行
        # 尾段零绕路任务（可行性硬约束本身不变，仍用时间口径逐个检查）
        margin = self.RUSH_SAFETY_MARGIN \
            if state.phase == P.PHASE_RUSH else self.SAFETY_MARGIN
        # 尾随冻结预算（V3.93）：活着的对手领先我们穿共享走廊时，它先过
        # 我们要过的每个节点=可以边走边回手卡（V3.92 调查的镜像死形：
        # 官道农到 150、r450 到 S13，被宫前驿回手卡冻死最后一条边 120 帧
        # ——SAFETY_MARGIN 60 挡不住一次冻结）。纯农画像（≥60 分+全场
        # 未见卡）豁免；只加在 NORMAL 阶段的任务贪吃决策上，RUSH 25
        # 边际是 V3.26 拿回 vs2986 的战果不动
        if state.phase != P.PHASE_RUSH \
                and self._rear_freeze_exposed(state, cur):
            margin += self.FREEZE_RISK_BUDGET
        slack = state.duration_round - (state.round + eta_direct + margin)

        # 已验核后离开宫门需要重新验核（6 帧），V1 不再接任务，直奔交付
        if me.get("verified"):
            self._committed = None
            return Plan("deliver", detail="verified", slack=slack)
        if slack <= 0:
            self._committed = None
            return Plan("deliver", detail="deadline", slack=slack)

        base = me.get("taskScore", 0) or 0
        cands = {}   # key -> (净值, ("task"/"resource"/"bounty", task, pos, rtype))
        for t in state.claimable_tasks():
            if self.blacklist.get(t["taskId"], 0) > state.round:
                continue
            ev = self._evaluate(state, t, cur, base, to_gate, eta_direct,
                                slack, speed, penalty, ecost)
            if ev and ev[0] > 0:
                cands[("task", t["taskId"])] = (ev[0], ("task", t, ev[1], None))
            floor_ev = self._evaluate(state, t, cur, base, to_gate, eta_direct,
                                      slack, speed, penalty, ecost,
                                      task_floor=True)
            if floor_ev and floor_ev[0] > 0:
                cands[("task_floor", t["taskId"])] = (
                    floor_ev[0] + TASK_FLOOR_BONUS,
                    ("task", t, floor_ev[1], None))

        # 资源提货目标与任务同台竞价（冰鉴 17 分 vs 任务 45~99 分 vs 绕路成本）
        for node_id, rtype, net in self._resource_targets(
                state, cur, to_gate, slack, speed, penalty, ecost):
            cands[("resource", node_id, rtype)] = \
                (net, ("resource", None, node_id, rtype))

        # 悬赏目标：只在落后时同台竞价（追分专用，见 6.3.3）
        for node_id, net in self._bounty_targets(
                state, cur, to_gate, slack, speed, penalty, ecost):
            cands[("bounty", node_id)] = (net, ("bounty", None, node_id, None))

        best_key = self._sticky_choice(cands)
        if best_key:
            best_net, (kind, t, pos, rtype) = cands[best_key]
            return Plan(kind, t, pos, slack=slack, resource=rtype,
                        detail=f"net={best_net:.0f} base={base}")
        return Plan("deliver", detail=f"no worthy task, base={base}", slack=slack)

    def _sticky_choice(self, cands):
        """目标粘性（V3.18）：净值最高者胜出，但换目标要求 15% 的净值优势。

        当前承诺目标已失效（被抢/过期/净值转负）时无粘性，直接换 argmax；
        没有任何正净值候选时清空承诺（回到 deliver）。"""
        if not cands:
            self._committed = None
            return None
        best_key = max(cands, key=lambda k: cands[k][0])
        held = self._committed
        if held in cands and held != best_key \
                and cands[best_key][0] < cands[held][0] * self.SWITCH_MARGIN:
            best_key = held
        self._committed = best_key
        return best_key

    # ================= 漏斗定价（V3.16） =================

    def _funnel_ctx(self, state, cur, penalty=None, ecost=None):
        """本帧漏斗上下文：(choke, 对手到位帧 t_o, 先验, 直达路的漏斗代价)。

        选路用价值口径（车队真实会走的路——裸最短路在本图是山线，会把
        官道候选冤枉成"绕路"），计时统一用裸边帧与对手 ETA 同度量。
        对手已交付/退赛或不可达时无漏斗威胁，返回 None。按 (round, cur) 缓存。
        """
        key = (state.round, cur)
        if self._funnel_cache[0] == key:
            return self._funnel_cache[1]
        ctx = None
        opp = state.opp
        if opp and not opp.get("delivered") and not opp.get("retired"):
            if not self._guard_seen:
                for node in state.nodes.values():
                    gd = node.get("guard")
                    if gd and gd.get("active") and \
                            gd.get("ownerTeamId") not in (None, state.my_team):
                        self._guard_seen = True
                        break
            _, path = state.graph.shortest_path(cur, state.gate_node, 1000,
                                                penalty, ecost)
            ahead = (path or [])[1:]
            choke = next((n for n in ahead
                          if state.node(n).get("nodeType") == "KEY_PASS"), None)
            if choke is None and self.CHOKE_PASS_FALLBACK:
                # 潼关回退（V3.20）：前方没有 KEY_PASS 时，PASS 型关隘是下一个
                # 可蹲守的咽喉——过武关不等于出走廊。语料 2839 蹲潼关；随机化
                # 陪练 seed4（蹲 S11、延迟起卡）双座位 -497 未交付，蹲潼关对
                # 只认 KEY_PASS 的旧模型完全隐形。开局 S03 被前方的 S10 遮蔽，
                # 此回退只在过了武关后生效，不改开局行为
                choke = next((n for n in ahead
                              if state.node(n).get("nodeType") == "PASS"), None)
            if choke:
                oe = self._opp_eta(state, choke)
                if oe != math.inf:
                    t_o = state.round + oe
                    if self._guard_seen or self.opp_profile == "camper":
                        prior = 1.0
                    elif self.opp_profile == "farmer":
                        # 农任务型（V3.26）：分数在涨、全场没设过卡，
                        # 漏斗威胁按证据降档（落卡即被 _guard_seen 覆盖）
                        prior = self.FUNNEL_FARMER_PRIOR
                    else:
                        prior = self.FUNNEL_GUARD_PRIOR
                    toll_direct = self._funnel_toll(
                        state, choke, t_o, path, state.round)
                    ctx = (choke, t_o, prior, toll_direct)
        self._funnel_cache = (key, ctx)
        self._last_choke = ctx[0] if ctx else None
        return ctx

    @staticmethod
    def _raw_walk_frames(state, path):
        """沿 path 的裸帧耗时（基准速度，与对手 ETA 同度量）。"""
        g = state.graph
        t, prev = 0.0, path[0]
        for nb in path[1:]:
            edge = g.edge_between(prev, nb)
            t += g.edge_frames(edge, P.BASE_SPEED) if edge else 0.0
            prev = nb
        return t

    @staticmethod
    def _funnel_toll(state, choke, t_o, path, t_start):
        """沿裸最短路 path、t_start 出发，过 choke 的期望代价 (死等帧, 税帧)。

        时间口径与 t_o 一致：裸边帧（基准速度、不含途中停留），双方同一度量。
        """
        if not path or choke not in path[1:]:
            return 0.0, 0.0
        g = state.graph
        t = float(t_start)
        prev = path[0]
        for nb in path[1:]:
            edge = g.edge_between(prev, nb)
            ef = g.edge_frames(edge, P.BASE_SPEED) if edge else 0.0
            if nb == choke:
                exit_t = t + ef
                # 竞速概率：出口早于 t_o−带宽 = 稳赢免疫；晚于 +带宽 = 稳输全额
                p_lose = min(1.0, max(0.0, (exit_t - (t_o - FUNNEL_RACE_BAND))
                                      / (2 * FUNNEL_RACE_BAND)))
                if p_lose <= 0:
                    return 0.0, 0.0  # 赶在对手到位前过完边：规则免疫（6.2.1）
                guard_up = t_o + FUNNEL_GUARD_READ
                # 死等只在稳输（p=1）时计费：竞速带内的"死等"本质是 S02 窗口式
                # 五五开，对各候选近似对称、在差值里相消，带内计它只会放大
                # 到达估计噪声（±2 帧的出发差被斜坡放大成 ±5 帧期望费）
                dead = max(0.0, guard_up - t) if p_lose >= 1.0 else 0.0
                age = max(0.0, t + dead - guard_up)
                # 非 KEY_PASS 卡（潼关类 PASS 关隘）首次风化 30 帧（规则：
                # 满防 45 帧仅限 KEY_PASS，其余 30），衰减节奏相同
                first = (FUNNEL_FIRST_WEATHER
                         if state.node(choke).get("nodeType") == "KEY_PASS"
                         else FUNNEL_WEATHER_GAP)
                if age < first:
                    defense = 6
                else:
                    defense = max(0, 5 - int((age - first)
                                             // FUNNEL_WEATHER_GAP))
                if defense <= 0:
                    return dead, 0.0
                tax = min(50, 15 + 5 * defense)
                return dead, (tax + FUNNEL_WINDOW_OVERHEAD) * p_lose
            t += ef
            prev = nb
        return 0.0, 0.0

    def _funnel_delta(self, state, cur, pos, proc, penalty=None, ecost=None):
        """经 pos 绕路相对直达路的漏斗代价差（帧，可负=晚到躲税/躲死等）。

        死等差不截断（纯几何）；税差截断 ±FUNNEL_TAX_DELTA_CAP（依赖对手行为）。
        """
        ctx = self._funnel_ctx(state, cur, penalty, ecost)
        if not ctx:
            return 0.0
        choke, t_o, prior, toll_direct = ctx
        g = state.graph
        _, p1 = g.shortest_path(cur, pos, 1000, penalty, ecost)
        _, p2 = g.shortest_path(pos, state.gate_node, 1000, penalty, ecost)
        if not p1 or not p2:
            return 0.0
        if choke in p1[1:]:
            via = self._funnel_toll(state, choke, t_o, p1, state.round)
        else:
            via = self._funnel_toll(
                state, choke, t_o, p2,
                state.round + self._raw_walk_frames(state, p1) + proc)
        d_dead = via[0] - toll_direct[0]
        d_tax = max(-FUNNEL_TAX_DELTA_CAP,
                    min(FUNNEL_TAX_DELTA_CAP, via[1] - toll_direct[1]))
        if d_tax > 0 and self._can_break_expected_guard(state, choke):
            # 有足够坏果/富余好果时，前方卡点不是满额强通税，而是到点秒拆。
            # 仍保留死等差：若对手能在长边中途掐成卡，边上冻结风险不在这里消除。
            d_tax *= self.BREAK_FUNNEL_TAX_MULT
        return (d_dead + d_tax) * prior

    # ================= 刷新流期望（V3.27） =================

    def _observe_spawns(self, state):
        """记录本局任务刷新观测：新 taskId 首见即计入其节点。"""
        for t in state.tasks:
            tid = t.get("taskId")
            if not tid or tid in self._task_seen:
                continue
            self._task_seen.add(tid)
            n = t.get("nodeId")
            if n:
                self._spawn_count[n] = self._spawn_count.get(n, 0) + 1

    def _refresh_rate(self, state, node_id):
        """节点每帧刷新率：本局观测 + 候选点静态先验的平滑混合。"""
        obs_total = len(self._task_seen)
        if not obs_total:
            return 0.0
        # 静态份额：该节点在多少个任务模板的候选列表里
        mult = sum(1 for nodes in (state.task_candidates or {}).values()
                   if node_id in nodes)
        mult_total = sum(len(nodes)
                         for nodes in (state.task_candidates or {}).values())
        share = mult / mult_total if mult_total else 0.0
        w = (self._spawn_count.get(node_id, 0) + share) / (obs_total + 1.0)
        total_rate = obs_total / max(state.round, REFRESH_RATE_FLOOR_ROUND)
        return total_rate * w

    def _refresh_bonus(self, state, pos, back_path, base):
        """目标点及沿途热点的未来任务流折分（上限一个任务）。

        目标点给驻留窗口信用（做完任务多等一手波次是 loiter 既有行为），
        沿途点给路过信用。按当前 base 的边际任务分与竞争折扣折算。"""
        mv30 = marginal_task_value(base, 30)
        bonus = self._refresh_rate(state, pos) * REFRESH_DWELL_CREDIT             * mv30 * self.REFRESH_CLAIM_PROB
        for n in set(back_path or ()) - {pos}:
            bonus += self._refresh_rate(state, n) * REFRESH_PASS_CREDIT                 * mv30 * self.REFRESH_CLAIM_PROB
        return min(bonus, self.REFRESH_VALUE_CAP)

    # ================= 破关悬赏估值（V3.12） =================

    def _bounty_targets(self, state, cur, to_gate, slack, speed, penalty, ecost):
        """挂在敌方有效设卡上、落后时值得专程绕路攻破的悬赏：[(nodeId, 净收益)]。

        目标节点就是设卡节点本身；实际攻坚由 strategy._breakthrough 在到达其
        相邻节点时触发（攻坚破卡规则要求站在相邻节点，不进入目标节点，见 6.3.1），
        这里的寻路会自然在最后一跳撞见 enemy_guard() 并转交给突破逻辑，无需特殊处理。
        """
        if not state.is_behind():
            return []
        me = state.me
        good = me.get("goodFruit", 0) or 0
        bad = me.get("badFruit", 0) or 0
        max_gf = min(2, max(0, good - BOUNTY_GOOD_RESERVE))
        max_bf = min(2, bad)
        max_defense = max_gf * 2 + max_bf * 3
        if max_defense <= 0:
            return []
        g = state.graph
        out = []
        for b in state.enemy_bounties():
            node_id = b.get("nodeId")
            if node_id == cur:
                continue
            guard = state.enemy_guard(node_id)
            defense = guard.get("defense", 0) or 0
            if defense <= 0 or defense > max_defense:
                continue  # 打不穿就不专程绕路，避免多轮车轮战的不确定性
            f_to, path = g.shortest_path(cur, node_id, speed, penalty, ecost)
            if not path:
                continue
            f_back, back = g.shortest_path(node_id, state.gate_node, speed, penalty, ecost)
            if not back:
                continue
            detour = max(0, f_to + f_back - to_gate)
            if self._time_detour(state, cur, node_id) > slack:
                continue  # 硬约束用时间口径，不能耽误交付
            net = (b.get("rewardScore") or 0) - detour * self._frame_value(state, to_gate)
            if net > 0:
                out.append((node_id, net))
        return out

    # ================= 资源提货估值 =================

    def _resource_ctx(self, state, cur):
        """资源竞速上下文（按帧+锚点缓存）。

        返回 (race_discount(nodeId, our_raw_eta), stock_claimables(node),
              opp_path, my_raw)；竞速统一用裸帧，双方同一度量。
        """
        cache = getattr(self, "_res_ctx_cache", None)
        if cache and cache[0] == (state.round, cur):
            return cache[1]

        me_res = state.me.get("resources") or {}
        opp = state.opp
        opp_pos = (opp.get("nextNodeId") or opp.get("currentNodeId")) if opp else None
        opp_dist = state.graph.all_frames(opp_pos) if opp_pos and \
            not (opp.get("delivered") or opp.get("retired")) else {}
        my_raw = state.graph.all_frames(cur)
        opp_path = self._opp_path_nodes(state)

        def race_discount(node_id, our_raw_eta):
            oe = opp_dist.get(node_id)
            if oe is None:
                return 1.0
            if oe + SHADOW_MARGIN < our_raw_eta:
                d = SWEEP_DISCOUNT
            elif abs(oe - our_raw_eta) <= SHADOW_MARGIN:
                d = CONTEST_DISCOUNT
            else:
                return 1.0
            # 不在对手前进路线上的资源：它专程绕过来的概率低，折扣设下限
            if node_id not in opp_path:
                d = max(d, self.OFFPATH_RACE_FLOOR)
            return d

        def stock_claimables(node):
            stock = node.get("resourceStock") or {}
            for rtype, value in RESOURCE_VALUES.items():
                limit = 2 if rtype == P.ICE_BOX else 1
                if stock.get(rtype, 0) > 0 and me_res.get(rtype, 0) < limit:
                    yield rtype, value

        ctx = (race_discount, stock_claimables, opp_path, my_raw)
        self._res_ctx_cache = ((state.round, cur), ctx)
        return ctx

    def _resource_bundle(self, state, pos, onward_path, cur):
        """任务/资源计划的沿途资源捆绑价值 (加分, 附加帧数)。

        任务点上的可领资源全额计入（就地领取只多花 2 帧读条）；
        通往宫门沿途的按半权。replay21/22 教训：官道任务捆着双冰、
        水路任务只捆一匹马，分开估值让 30 分的鲜度捆绑被单任务净值掩盖。
        """
        race_discount, stock_claimables, opp_path, my_raw = \
            self._resource_ctx(state, cur)
        bonus, frames = 0.0, 0
        node = state.nodes.get(pos) or {}
        for rtype, value in stock_claimables(node):
            v = self._resource_phase_value(state, pos, value, rtype) \
                * race_discount(pos, my_raw.get(pos, 0))
            if pos in opp_path:
                v *= DENIAL_FACTOR
            bonus += v
            frames += CLAIM_FRAMES
        node_raw = state.graph.all_frames(pos) if onward_path else {}
        for nb in set(onward_path or ()) - {pos}:
            nb_node = state.nodes.get(nb) or {}
            for rtype, value in stock_claimables(nb_node):
                eta = my_raw.get(pos, 0) + frames + node_raw.get(nb, 0)
                v = CHAIN_WEIGHT \
                    * self._resource_phase_value(state, nb, value, rtype) \
                    * race_discount(nb, eta)
                if nb in opp_path:
                    v *= DENIAL_FACTOR
                bonus += v
        return bonus, frames

    def _resource_targets(self, state, cur, to_gate, slack, speed, penalty, ecost):
        """有库存且值得专程去领的资源点: [(nodeId, resourceType, 净收益)]。

        V3.3 估值 = 面值 × 竞争折扣 × 拒止倍率 + 链式加成 − 绕路成本：
        - 拒止：资源在对手前进路线上，抢到 = 我 +17 且对手 -18（双向摇摆）
        - 链式：目标点通往宫门路上的其他可领资源按半权计入
          （败局教训：S06 山冰面值 17 干净，却放走了官道 S03+S07 买一送一）
        """
        race_discount, stock_claimables, opp_path, my_raw = \
            self._resource_ctx(state, cur)
        g = state.graph
        out = []
        for node_id, node in state.nodes.items():
            for rtype, value in stock_claimables(node):
                f_to, path = g.shortest_path(cur, node_id, speed, penalty, ecost)
                if not path:
                    continue
                if self._front_tempo_resource_blocked(state, cur, node_id, path):
                    continue
                f_to += self._backtrack_tax(state, cur, node_id)
                f_back, back = g.shortest_path(node_id, state.gate_node, speed,
                                               penalty, ecost)
                if not back:
                    continue
                detour = max(0, f_to + f_back - to_gate) + CLAIM_FRAMES
                if self._time_detour(state, cur, node_id) + CLAIM_FRAMES > slack:
                    continue  # 硬约束用时间口径
                v = self._resource_phase_value(state, node_id, value, rtype) \
                    * race_discount(node_id, my_raw.get(node_id, 0))
                if node_id in opp_path:
                    v *= DENIAL_FACTOR      # 抢的是对手碗里的
                # 链式加成：拿下该点后，去宫门路上的其他资源顺路半价计
                chain = 0.0
                node_raw = g.all_frames(node_id)
                for nb in set(back[1:]):
                    nb_node = state.nodes.get(nb) or {}
                    for rt2, val2 in stock_claimables(nb_node):
                        eta2 = my_raw.get(node_id, 0) + CLAIM_FRAMES \
                            + node_raw.get(nb, 0)
                        d2 = race_discount(nb, eta2)
                        if nb in opp_path:
                            val2 = val2 * DENIAL_FACTOR
                        chain += CHAIN_WEIGHT \
                            * self._resource_phase_value(state, nb, val2, rt2) * d2
                # 资源不计漏斗差（V3.16）也不计竞速溢价（V3.18）：资源面值
                # ≤19，漏斗模型在竞速带附近的到达估计噪声（±2 帧出发差 →
                # ±40 帧期望费）会淹没它们；路线级灾难（36/56/57 山路口袋）
                # 全部由任务链驱动，任务侧计价足够
                net = v + chain - detour * self._frame_value(state, to_gate,
                                                             race_adjust=False)
                if net > 0:
                    out.append((node_id, rtype, net))
        return out

    # ================= 估值 =================

    def _evaluate(self, state, task, cur, base, to_gate, eta_direct,
                  slack, speed, penalty, ecost=None, task_floor=False):
        """返回 (净收益, 停靠节点)；不可行返回 None。"""
        if task_floor and not (
                TASK_FLOOR_MIN_BASE <= base < TASK_FLOOR_BASE
                and state.phase == P.PHASE_NORMAL
                and not self.race_cliff(state)
                and not self._key_pass_ahead(state, cur, penalty, ecost)):
            return None
        # 资源前置：如 T06 启动时要消耗 1 匹马；缺前置资源跑过去只会被拒
        # （requiredResourceTypes 语义为「任一满足」，如 快马/短程马 二选一）。
        # start.taskTemplates 可能为空，T06 按任务书 5.2 硬规则兜底。
        tpl = state.task_templates.get(task.get("taskTemplateId")) or {}
        required = tpl.get("requiredResourceTypes") or []
        if not required and task.get("taskTemplateId") == "T06":
            required = [P.FAST_HORSE, P.SHORT_HORSE]
        if required:
            res = state.me.get("resources") or {}
            if not any(res.get(rt, 0) > 0 for rt in required):
                return None
        # T04 目标障碍已被非 T04 方式清除 → 该任务永久失败（任务书 5.2），
        # 服务端仍标 active=true，提交只会吃 TASK_REQUIREMENT_NOT_MET
        # （replay20/36：S08 站着逐帧重试死任务 38/27 帧）
        if task.get("taskTemplateId") == "T04" \
                and not state.has_obstacle(task.get("nodeId")):
            return None

        g = state.graph
        pos, f_to = self._position_for(state, task, cur, speed, penalty, ecost)
        if pos is None:
            return None
        if self._front_tempo_task_blocked(state, task, cur, pos, speed,
                                          penalty, ecost, base):
            return None
        if self._keypass_tempo_task_blocked(state, task, cur, pos, speed,
                                            penalty, ecost, base):
            return None
        f_to += self._backtrack_tax(state, cur, pos)

        proc = task.get("processRound", 4) or 4
        if self._has_our_scout_mark(state, pos):
            proc = max(2, proc - 3)

        # 过期检查：赶到 + 读完条要在过期帧之前
        expire = task.get("expireRound") or 0
        if expire and state.round + f_to + proc > expire:
            return None

        # 绕路帧数 = (当前->任务点 + 任务点->宫门) - 当前->宫门直达
        f_back, back_path = g.shortest_path(pos, state.gate_node, speed,
                                            penalty, ecost)
        if not back_path:
            return None

        # （V3.21 校准注：曾试过更狠的悬崖带任务闸门——带内只放行严格
        # 关后的任务。被证伪：seed23 类深链死局（起卡延迟+晚动身+二卡）
        # 救不回来，反而把赢局分数拉低 ~120（跨里程碑任务在领先不宽裕的
        # 局里被误杀）。悬崖价 30/帧 + 顺路领取清空已是净收益最优点）
        detour = max(0, f_to + f_back - to_gate)
        total_frames = detour + proc
        # 硬约束用时间口径（可行性看时间，优劣看价值）
        time_frames = self._time_detour(state, cur, pos) + proc
        if time_frames > slack:
            return None
        if task_floor and time_frames > TASK_FLOOR_MAX_FRAMES:
            return None

        value = marginal_task_value(base, task.get("score", 0))
        # 前推偏置（V3.24）：前期节点的任务降权，优先把身位往走廊冲——
        # 同样的分数在沿途更靠前的波次里农回来（2839 打法），开局停留
        # 是深链死局的第一环
        value *= self._forward_factor(state, pos)
        # 对手风险：对手离任务点更近时打折；对手正在处理该任务则放弃。
        # 离路软化（V3.10.1）：任务点不在对手合理走廊上时，它专程绕来抢的
        # 概率低（与资源折扣对称）——曾把走官道的 2614 判定会来抢山地任务
        if self._opp_processing_task(state, task):
            return None
        opp = state.opp or {}
        opp_out = opp.get("delivered") or opp.get("retired")
        opp_eta = self._opp_eta(state, pos)
        if opp_eta < f_to and not opp_out:
            # 分段折扣（V3.23）：对手已交付/退赛不折（规则上不可能被抢，
            # 曾漏此检查白砍尾段任务）；关后 0.9（反事实 97~100%）；
            # 关前保持 0.5（含贴身绕路外部性的补偿定价，放宽已被证伪）
            gap = f_to - opp_eta
            if self.CONTEST_PHASE_ENABLED and not self._choke_ahead(state):
                d = self.POST_CHOKE_CONTEST_DISCOUNT
            else:
                d = (self.CONTEST_GRACE_DISCOUNT
                     if gap <= self.CONTEST_GRACE_FRAMES
                     else self.CONTEST_RISK_DISCOUNT)
            if pos not in self._opp_path_nodes(state):
                d = max(d, self.OFFPATH_RACE_FLOOR)
            value *= d

        # 资源捆绑（V3.6）：任务点及其通往宫门沿途的可领资源计入任务估值。
        # replay21/22 教训：T01@S03 捆着官道双冰、T08@S04 只捆一匹马，
        # 单任务净值 argmax 把 30+ 分的鲜度捆绑包挤出局。
        bundle, bframes = self._resource_bundle(state, pos, back_path, cur)
        if self._time_detour(state, cur, pos) + proc + bframes > slack:
            bundle, bframes = 0.0, 0  # 余量装不下捆绑就只按裸任务估

        fv = self._frame_value(state, eta_direct,
                               race_adjust=not task_floor)
        # 共点对峙豁免（V3.26，V3.26.1 收紧）：悬崖带内、任务就在脚下、
        # 对手停靠在同一节点 —— 双方同桌，悬崖前提（我停它不停）不成立，
        # 回落竞速帧价。vs2619 实锤：S07 三连任务被悬崖全砍，对手留场
        # 吃满 90。收紧记录（camper seed15 A/B 抓获）：首版用
        # "opp ETA ≤10 将至"即豁免，被开局汇聚窗口反噬——camper 分 0、
        # 未落卡、画像 unknown 时与我们同桌，一停即输走廊（-410 未交付，
        # 正是 seed10/15 死形）。现要求①普通节点（关隘同桌=对方蹲点
        # 预备式，不豁免）②对手已停靠（不是"将至"，收敛中让路照旧）
        if (self.CLIFF_MELEE_EXEMPT
                and fv >= self.RACE_CLIFF_FRAME_VALUE and pos == cur
                and not self._guard_seen and self.opp_profile != "camper"
                and state.node(pos).get("nodeType")
                not in ("KEY_PASS", "PASS", "GATE")
                and self._opp_farming_at(state, pos)):
            fv = (FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME) \
                * self.RACE_FRAME_MULT
        cost = (total_frames + bframes) * fv
        # 漏斗定价（V3.16/V3.31）：绕路改变到达关键关隘的时机，死等/
        # 满防税/躲税的差额计入净值。漏斗税是实际等待/通行帧，恒用基础
        # 帧价；悬崖帧价已包含输掉漏斗竞速的尾部后果，不能再乘一次 30。
        funnel = self._funnel_delta(state, cur, pos, proc + bframes,
                                    penalty, ecost) * _FV
        refresh = self._refresh_bonus(state, pos, back_path, base)
        net = value + bundle + refresh - cost - funnel
        return (net, pos) if net > 0 else None

    def _position_for(self, state, task, cur, speed, penalty, ecost=None):
        """任务执行停靠点与到达帧数。T04 清障可在障碍节点或相邻节点处理。"""
        g = state.graph
        node = task.get("nodeId")
        if task.get("taskTemplateId") == "T04" and state.has_obstacle(node):
            # 障碍节点进不去：在相邻节点中选最快到达的
            cands = [n for n, _ in g.neighbors(node)]
            if cur == node or cur in cands:
                return cur, 0
            best = None
            for c in cands:
                f, path = g.shortest_path(cur, c, speed, penalty, ecost)
                if path and (best is None or f < best[1]):
                    best = (c, f)
            return best if best else (None, None)
        f, path = g.shortest_path(cur, node, speed, penalty, ecost)
        if not path:
            return None, None
        return node, f

    def _key_pass_ahead(self, state, cur, penalty=None, ecost=None):
        """当前到宫门的规划路线上是否还有未过的 KEY_PASS。"""
        if not cur or not state.graph:
            return False
        _, path = state.graph.shortest_path(cur, state.gate_node, P.BASE_SPEED,
                                            penalty, ecost)
        return any(state.node(n).get("nodeType") == "KEY_PASS"
                   for n in (path or [])[1:])

    # ================= 竞速模式（V3.18） =================

    def race_mode(self, state):
        """漏斗竞速窗口：双方到下一关键关隘的裸 ETA 差在 ±RACE_BAND 帧内。

        规则依据 6.2.1：设卡必须人站在目标节点上——先过漏斗者对身后的卡
        免疫、对手的前路全在自己射程内；后到者吃死等+满防税。这个不对称
        只在竞争带内可争夺，带内每一帧都是胜负帧。
        进入/退出全由公开状态决定：过完咽喉（前方无 KEY_PASS）、对手已
        交付/退赛、或差距拉出竞争带即退出。双方用同一裸帧度量。
        """
        if self._race_cache[0] == state.round:
            return self._race_cache[1]
        active = False
        cur = self._anchor_node(state)
        if cur and state.graph:
            # 用价值口径选路找咽喉（与 plan() 同源，裸最短路会冤枉官道候选）
            ctx = self._funnel_ctx(state, cur, self._penalty_fn(state),
                                   self._edge_cost_fn(state))
            if ctx:
                choke, t_o, _, _ = ctx
                my_eta = state.graph.all_frames(cur).get(choke, math.inf)
                if my_eta != math.inf and \
                        abs(state.round + my_eta - t_o) <= self.RACE_BAND:
                    active = True
        self._race_cache = (state.round, active)
        return active

    def race_cliff(self, state):
        """悬崖带：咽喉已近、尚无敌卡、我们没有安全领先——带内一帧≈胜负帧。

        与 race_mode 解耦（V3.21 校准）：race_mode 的 ±25 对称带在尾侧
        太窄——落后度量在长边上有 +1/帧的锚点漂移、t_o 是不含对手停留的
        裸 ETA（双重偏差可达 ~55 帧），seed23 实测在真实可争的局面里
        "落后 26"被判出带，悬崖关闭后顺路任务复活、死局原样复现。
        规则语义上，只要对手还没在咽喉起卡，门就没关：早到 1 帧就少
        1 帧死等/税差，落后侧的悬崖一直延伸到 RACE_CLIFF_TRAIL。
        它一起卡，悬崖已定（转入漏斗定价/攻坚经济），立即退出。
        进入/退出全由公开状态决定，与对手意愿无关。
        """
        if self._cliff_cache[0] == state.round:
            return self._cliff_cache[1]
        # V3.31：不再用 taskScore>=30 这个裸证据直接当 farmer。先归一到
        # 互斥节奏状态：纯 farmer 和 farm-rusher 都不吃完整悬崖价；后者
        # 走自己的前推/局部设卡/短等响应。曾试把 farm-rusher 重新拉进
        # 全局悬崖，toller seed3 立刻从 +18 回归到 -169。
        active = False
        if self.RACE_CLIFF_ENABLED \
                and self._opp_tempo_mode(state) not in ("farmer", "farm-rusher"):
            cur = self._anchor_node(state)
            ctx = self._funnel_ctx(state, cur, self._penalty_fn(state),
                                   self._edge_cost_fn(state)) if cur else None
            if ctx:
                choke, t_o, _, _ = ctx
                g = state.node(choke).get("guard")
                guarded = bool(g and g.get("ownerTeamId")
                               and g.get("ownerTeamId") != state.my_team
                               and g.get("active", g.get("defense", 0) > 0))
                my_eta = state.graph.all_frames(cur).get(choke, math.inf)
                if not guarded and my_eta != math.inf \
                        and my_eta <= self.RACE_CLIFF_ETA:
                    lead = t_o - (state.round + my_eta)   # >0 = 我们先到
                    if -self.RACE_CLIFF_TRAIL <= lead <= self.RACE_CLIFF_LEAD:
                        active = True
                        self._cliff_choke = choke
        if not active:
            self._cliff_choke = None
        self._cliff_cache = (state.round, active)
        return active

    def _map_total(self, state):
        """起点到宫门的裸帧全程（缓存），进度/深度度量的分母。"""
        if self._fwd_total is None:
            d, p = state.graph.shortest_path(
                state.start_node, state.gate_node, P.BASE_SPEED) \
                if state.start_node else (None, None)
            self._fwd_total = d if p else 0
        return self._fwd_total

    def _map_progress(self, state, node_id):
        """节点到宫门的裸帧进度：起点 0，宫门 1。"""
        total = self._map_total(state)
        if not total:
            return 1.0
        remain, path = state.graph.shortest_path(
            node_id, state.gate_node, P.BASE_SPEED)
        if not path:
            return 1.0
        return max(0.0, min(1.0, 1.0 - remain / total))

    def _front_tempo_active(self, state, cur, base=None):
        """开局簇推进保护：对手只领先几帧时，先尾随主路再补经济。"""
        if not self.FRONT_TEMPO_ENABLED or state.phase != P.PHASE_NORMAL:
            return False
        base = state.me.get("taskScore", 0) if base is None else base
        if (base or 0) >= self.FRONT_TEMPO_BASE_CAP:
            return False
        opp = state.opp or {}
        if opp.get("delivered") or opp.get("retired"):
            return False
        # V3.91 形态门控：只对已实锤的零设卡富点干等型纯农保速尾随。
        # 四轮收紧史：unknown 放行 → camper 亏损全回来（smoke 五红灯）；
        # farm-rusher 放行 → toller0 翻负；仅 farmer+未见卡 → toller0
        # 仍翻负（2839 落卡前与 2986 同貌，信息论墙）。最终分水岭 =
        # 富点干等实锤（普通节点累计闲置 ≥ DWELL_MIN；"农不停步"的
        # toller 与只蹲关隘的 camper 永不过线）
        if self._opp_tempo_mode(state) != "farmer" or self._guard_seen \
                or self._opp_dwell_idle < self.FRONT_TEMPO_DWELL_MIN:
            return False
        if self._map_progress(state, cur) >= self.FRONT_TEMPO_PROGRESS_CUT:
            return False
        if not self._opp_on_my_forward_path(state, cur):
            return False
        return self._opp_gate_lead(state, cur) >= self.FRONT_TEMPO_OPP_LEAD

    def _opp_gate_lead(self, state, cur):
        """对手相对我方到宫门的裸帧领先；正数=对手更快到宫门。"""
        if not cur or not state.graph:
            return 0.0
        my_gate = state.graph.all_frames(cur).get(state.gate_node, math.inf)
        opp_gate = self._opp_eta(state, state.gate_node)
        if my_gate == math.inf or opp_gate == math.inf:
            return 0.0
        return my_gate - opp_gate

    def _rear_freeze_exposed(self, state, cur):
        """尾随冻结暴露：活着的对手领先我方且共享前路（V3.93）。

        它先经过我们将要经过的节点，随时可以站桩回手卡；一旦踏边被掐，
        冻结时长（首风化 30~45 帧+衰减）远超基础安全边际。豁免：已实锤
        的零设卡纯农（strategy 画像 farmer 要求 ≥60 分且全场未见卡——
        这个决策发生在中后盘，画像已定型，不撞开局信息墙）。
        """
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        if self.opp_profile == "farmer" and not self._guard_seen:
            return False
        if not self._opp_on_my_forward_path(state, cur):
            return False
        return self._opp_gate_lead(state, cur) > 0

    def _opp_on_my_forward_path(self, state, cur):
        """对手当前位置/下一站是否在我方当前前路上；水路绕行不触发尾随。"""
        opp = state.opp or {}
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_pos or not cur or not state.graph:
            return False
        _, path = state.graph.shortest_path(cur, state.gate_node, P.BASE_SPEED)
        return opp_pos in (path or [])[1:]

    def _front_tempo_blocks_path(self, state, path):
        """前期不为单点收益切入山线；官道/水路硬件链仍按原口径争。"""
        if len(path or ()) < 2:
            return False
        for a, b in zip(path, path[1:]):
            edge = state.graph.edge_between(a, b)
            if edge and edge.get("routeType") in FRONT_TEMPO_BLOCK_ROUTE_TYPES:
                return True
        return False

    def _front_tempo_contested_raw(self, state, cur):
        """前半程身位仍在同屏竞争（raw 身位判定，不吃形态门）。

        山路轻门（V3.35）用本口径：它的目标形态含 rusher/farm-rusher，
        不能被全量门的"干等实锤纯农"门挡住。
        """
        opp = state.opp or {}
        if opp.get("delivered") or opp.get("retired"):
            return False
        if self._map_progress(state, cur) >= self.LATE_RESOURCE_PROGRESS:
            return False
        if self._opp_on_my_forward_path(state, cur):
            return True
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if opp_pos == cur:
            return True
        return self._opp_gate_lead(state, cur) >= self.FRONT_TEMPO_KEEP_LEAD_TRAIL

    def _front_tempo_contested(self, state, cur):
        """形态门控版身位竞争：全量 FRONT_TEMPO 链路（关前节奏闸）用。"""
        if self._opp_tempo_mode(state) != "farmer" or self._guard_seen \
                or self._opp_dwell_idle < self.FRONT_TEMPO_DWELL_MIN:
            return False     # 同 _front_tempo_active 的形态门控（V3.91）
        return self._front_tempo_contested_raw(state, cur)

    def _task_route_bucket(self, state, task, pos):
        bucket = task.get("routeBucket") or task.get("routeType")
        if bucket:
            return bucket
        node_id = task.get("nodeId") or pos
        if node_id and pos and node_id != pos:
            edge = state.graph.edge_between(pos, node_id)
            if edge:
                return edge.get("routeType")
        return None

    def _opp_committed_corridor(self, state):
        """对手前段已用移动/站位暴露的走廊；未知时不猜。"""
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        def node_corridor(node_id):
            if node_id not in FRONT_TEMPO_CORRIDOR_NODES:
                return None
            if not self._key_pass_ahead(state, node_id):
                return None
            rt = FRONT_TEMPO_CORRIDOR_NODES[node_id]
            return rt if rt in FRONT_TEMPO_CORRIDOR_ROUTES else None
        edge_id = opp.get("routeEdgeId")
        if edge_id and edge_id in state.graph.edges:
            rt = state.graph.edges[edge_id].get("routeType")
            if rt in FRONT_TEMPO_CORRIDOR_ROUTES:
                return rt
            return node_corridor(opp.get("nextNodeId")) \
                or node_corridor(opp.get("currentNodeId"))
        return node_corridor(opp.get("currentNodeId"))

    def _candidate_corridor(self, state, task, cur, pos, speed, penalty,
                            ecost):
        bucket = self._task_route_bucket(state, task, pos)
        if bucket in FRONT_TEMPO_CORRIDOR_ROUTES:
            return bucket
        _, path = state.graph.shortest_path(cur, pos, speed, penalty, ecost)
        path = path or ()
        for a, b in zip(path, path[1:]):
            edge = state.graph.edge_between(a, b)
            rt = edge.get("routeType") if edge else None
            if rt in FRONT_TEMPO_CORRIDOR_ROUTES:
                return rt
        return None

    def _front_tempo_corridor_follow_blocked(self, state, task, cur, pos,
                                             speed, penalty, ecost, base):
        """首关前跟随对手已提交走廊，别在岔路口把路权农掉。"""
        if not self.FRONT_TEMPO_CORRIDOR_FOLLOW:
            return False
        if pos == cur:
            return False
        if state.phase != P.PHASE_NORMAL or self.opp_profile == "camper":
            return False
        base = state.me.get("taskScore", 0) if base is None else base
        if (base or 0) >= self.FRONT_TEMPO_CORRIDOR_BASE_CAP:
            return False
        if not self._key_pass_ahead(state, cur, penalty, ecost):
            return False
        if not self._front_tempo_contested_raw(state, cur):
            return False     # raw 身位判定：轻门不吃全量门的形态门（V3.94）
        opp_route = self._opp_committed_corridor(state)
        if opp_route is None:
            return False
        cand_route = self._candidate_corridor(state, task, cur, pos, speed,
                                              penalty, ecost)
        if cand_route is None:
            return False
        return cand_route != opp_route

    def _front_tempo_heavy_mountain_target_blocked(self, state, cur, target,
                                                   speed, penalty, ecost,
                                                   base):
        """新图早段重山线入口闸门：别被 S06 任务/资源把主路权拽走。"""
        if not self.FRONT_TEMPO_HEAVY_MOUNTAIN_GUARD:
            return False
        if not target or target == cur:
            return False
        if state.phase != P.PHASE_NORMAL:
            return False
        if self.opp_profile == "camper":
            return False
        base = state.me.get("taskScore", 0) if base is None else base
        if (base or 0) < FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_BASE:
            return False
        if (base or 0) >= FRONT_TEMPO_HEAVY_MOUNTAIN_BASE_CAP:
            return False
        # 这轮平台负样本集中在 S03 入口被 S06 T04/冰鉴吸走；对手已明显
        # 抢到前方时，山线可能是反 camper 的救援路线，不在这里泛拦。
        if cur not in ("S02", "S03") or target != "S06":
            return False
        if self._opp_gate_lead(state, cur) > FRONT_TEMPO_HEAVY_MOUNTAIN_MAX_OPP_LEAD:
            return False
        if FRONT_TEMPO_CORRIDOR_NODES.get(cur) == P.MOUNTAIN:
            return False
        if FRONT_TEMPO_CORRIDOR_NODES.get(target) != P.MOUNTAIN:
            return False
        if self._opp_committed_corridor(state) == P.MOUNTAIN:
            return False
        if not self._key_pass_ahead(state, cur, penalty, ecost):
            return False

        direct, direct_path = state.graph.shortest_path(
            cur, state.gate_node, speed, penalty, ecost)
        to_target, target_path = state.graph.shortest_path(
            cur, target, speed, penalty, ecost)
        from_target, onward_path = state.graph.shortest_path(
            target, state.gate_node, speed, penalty, ecost)
        if not direct_path or not target_path or not onward_path:
            return False
        detour = to_target + from_target - direct
        return (to_target >= FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_LEG
                or detour >= FRONT_TEMPO_HEAVY_MOUNTAIN_MIN_DETOUR)

    def _front_tempo_heavy_mountain_task_blocked(self, state, task, cur, pos,
                                                 speed, penalty, ecost, base):
        """T04 可在相邻节点处理，但会解锁重山线；按目标节点而非停靠点定价。"""
        target = task.get("nodeId") or pos
        if self._task_route_bucket(state, task, pos) != P.MOUNTAIN:
            return False
        return self._front_tempo_heavy_mountain_target_blocked(
            state, cur, target, speed, penalty, ecost, base)

    def _keypass_tempo_task_blocked(self, state, task, cur, pos, speed,
                                    penalty, ecost, base):
        """首个关键关前的节奏闸门：120 分够进 S10，非官道先让。"""
        if not self.FRONT_TEMPO_ENABLED or state.phase != P.PHASE_NORMAL:
            return False
        if not self._front_tempo_contested(state, cur):
            return False
        if not self._key_pass_ahead(state, cur, penalty, ecost):
            return False
        bucket = self._task_route_bucket(state, task, pos)
        if bucket and bucket not in FRONT_TEMPO_KEYPASS_TASK_ROUTES:
            return True
        return (base or 0) >= self.FRONT_TEMPO_KEYPASS_BASE_CAP

    def _front_tempo_task_blocked(self, state, task, cur, pos, speed,
                                  penalty, ecost, base):
        """早段路线承诺闸门：低进度/山线任务先让，等 S07/S10 波次补分。"""
        if self._front_tempo_heavy_mountain_task_blocked(
                state, task, cur, pos, speed, penalty, ecost, base):
            return True
        if self._front_tempo_corridor_follow_blocked(
                state, task, cur, pos, speed, penalty, ecost, base):
            return True
        if not self._front_tempo_active(state, cur, base):
            return self._front_tempo_mountain_recovery_blocked(
                state, task, cur, pos, speed, penalty, ecost, base)
        _, path = state.graph.shortest_path(cur, pos, speed, penalty, ecost)
        if self._front_tempo_blocks_path(state, path):
            return True
        task_node = task.get("nodeId") or pos
        progress = min(self._map_progress(state, pos),
                       self._map_progress(state, task_node))
        return progress < self.FRONT_TEMPO_PROGRESS_CUT

    def _front_tempo_mountain_recovery_blocked(self, state, task, cur, pos,
                                               speed, penalty, ecost, base):
        """平台保速轻门：默认只拦前段竞争态的山线任务/山线路径。

        V3.32 关闭全量 FRONT_TEMPO 后，本地哨兵更稳，但平台 lose 批次
        重新暴露 2986/2931 型问题：已有基础分时被 S06/S08 山路任务拖走，
        对手先入 S10/S11 后建立卡点。这里不恢复资源截停，只把最稳定的
        山线岔路负样本收回来。
        """
        if not self.FRONT_TEMPO_MOUNTAIN_RECOVERY:
            return False
        # V3.91：不再因全量门开启而自闭——全量门被"干等实锤纯农"收窄
        # 后，轻门负责其余形态的山线拖走负样本；身位判定用 raw 口径
        if state.phase != P.PHASE_NORMAL:
            return False
        base = state.me.get("taskScore", 0) if base is None else base
        if (base or 0) < 30 or (base or 0) >= self.FRONT_TEMPO_BASE_CAP:
            return False
        opp = state.opp or {}
        if (opp.get("taskScore") or 0) < self.RACE_CLIFF_OPP_FARM \
                and self._opp_tempo_mode(state) not in ("rusher", "farm-rusher"):
            return False
        if not self._front_tempo_contested_raw(state, cur):
            return False
        if not self._key_pass_ahead(state, cur, penalty, ecost):
            return False
        bucket = self._task_route_bucket(state, task, pos)
        if bucket == P.MOUNTAIN:
            return True
        _, path = state.graph.shortest_path(cur, pos, speed, penalty, ecost)
        return self._front_tempo_blocks_path(state, path)

    def _front_tempo_resource_blocked(self, state, cur, node_id, path):
        """早段不专程钻支线拿资源；贴着对手主路的硬件资源仍可争。"""
        base = state.me.get("taskScore", 0) or 0
        if base >= 30 and self._front_tempo_heavy_mountain_target_blocked(
                state, cur, node_id, P.BASE_SPEED, self._penalty_fn(state),
                self._edge_cost_fn(state), base):
            return True
        if not self._front_tempo_active(state, cur):
            return False
        if node_id == cur:
            return False
        if self._front_tempo_blocks_path(state, path):
            return True
        if self._map_progress(state, node_id) < self.FRONT_TEMPO_PROGRESS_CUT \
                and node_id not in self._opp_path_nodes(state):
            return True
        return False

    def _break_capacity(self, state):
        """当前可立即投入的攻坚值：坏果优先，好果保留交付底仓。"""
        me = state.me
        good = me.get("goodFruit", 0) or 0
        bad = me.get("badFruit", 0) or 0
        return min(2, max(0, good - BOUNTY_GOOD_RESERVE)) * 2 \
            + min(2, bad) * 3

    def _expected_guard_defense(self, state, node_id):
        if state.node(node_id).get("nodeType") in CHOKE_TYPES:
            return self.BREAK_GUARD_EXPECT_DEFENSE
        return 4

    def _can_break_expected_guard(self, state, node_id=None):
        defense = self.BREAK_GUARD_EXPECT_DEFENSE if node_id is None \
            else self._expected_guard_defense(state, node_id)
        return self._break_capacity(state) >= defense

    def _resource_phase_value(self, state, node_id, value, rtype=None):
        """中后段硬件资源加权：前段保推进，后段补鲜度/速度。"""
        if rtype == P.ICE_BOX \
                and (state.me.get("badFruit", 0) or 0) < self.ICE_AMMO_TARGET_BAD \
                and state.phase == P.PHASE_NORMAL \
                and self._key_pass_ahead(state, node_id):
            value *= self.ICE_PRE_AMMO_VALUE_MULT
        if self._map_progress(state, node_id) >= self.LATE_RESOURCE_PROGRESS:
            return value * self.LATE_RESOURCE_MULT
        return value

    def _opp_tempo_mode(self, state):
        """互斥对手节奏状态：camper/farmer/rusher/farm-rusher/unknown。

        这里不替代 strategy 的粘性画像，只给 planner 内部所有节奏机制
        一个共同入口，避免同一份 taskScore 证据被不同模块解释成相反结论。
        """
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return "unknown"
        if self.opp_profile == "camper":
            return "camper"
        if self.forward_rush_opp:
            return "farm-rusher" if (opp.get("taskScore") or 0) else "rusher"
        if self.farm_rusher_pressure(state):
            return "farm-rusher"
        if (opp.get("taskScore") or 0) >= self.RACE_CLIFF_OPP_FARM:
            return "farmer"
        return "unknown"

    def _forward_factor(self, state, node_id):
        """地图进度系数：起点附近 → FLOOR，宫门方向 → 1.0（裸帧度量）。"""
        floor = self.FORWARD_BIAS_FLOOR
        if self._opp_tempo_mode(state) in ("rusher", "farm-rusher"):
            floor = min(floor, self.FORWARD_BIAS_AUTO)
        if floor >= 1.0:
            return 1.0
        progress = self._map_progress(state, node_id)
        if self.FORWARD_BIAS_CUT > 0:
            return floor if progress < self.FORWARD_BIAS_CUT else 1.0
        return floor + (1.0 - floor) * progress

    def _choke_ahead(self, state):
        """前方是否还有咽喉（漏斗 ctx 存在）——争夺折扣的阶段判定。"""
        if self._choke_ahead_cache[0] == state.round:
            return self._choke_ahead_cache[1]
        cur = self._anchor_node(state)
        ctx = self._funnel_ctx(state, cur, self._penalty_fn(state),
                               self._edge_cost_fn(state)) if cur else None
        val = bool(ctx)
        self._choke_ahead_cache = (state.round, val)
        return val

    # ================= 帧价值与辅助 =================

    def _frame_value(self, state, eta_direct, race_adjust=True):
        """一帧的机会成本 = 鲜度损耗 + 用时分斜率；竞速窗口内按倍率上调。

        用时分按交付帧计算，而绕路 1 帧交付就晚 1 帧 —— 无论现在离
        宫宴冲刺多远，这 0.117 分/帧都是实打实的（平台三局我们交付
        都在 537+，比对手晚 40~60 帧，鲜度+用时合计输 30 分）。

        竞速期（V3.18）：0.227 的平局真空价只对无争夺场景成立。竞争带内
        改价格而不是逐个加闸门，让规划器自己砍掉边际小绕路。
        race_adjust=False 供资源目标用——与"资源不计漏斗差"（V3.16）同一
        理由：冰链是 17~19 分级的交付硬件、语料两次败局（13/14）实锤不可
        放弃，竞速溢价会把开局唯一冰的争夺整个砍掉；路线级节奏灾难由任务
        链驱动，任务侧计价足够。
        """
        v = FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME
        if race_adjust and self.race_mode(state):
            v *= self.RACE_FRAME_MULT
            if self.race_cliff(state):
                v = max(v, self.RACE_CLIFF_FRAME_VALUE)
        return v

    def farm_rusher_pressure(self, state, cur=None):
        """对手是“边农边冲”而非纯 farmer：高任务分且宫门 ETA 已形成压力。"""
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired") or not state.graph:
            return False
        if (opp.get("taskScore") or 0) < FARM_RUSH_TASK:
            return False
        cur = cur or self._anchor_node(state)
        if not cur:
            return False
        my_gate = state.graph.all_frames(cur).get(state.gate_node, math.inf)
        opp_gate = self._opp_eta(state, state.gate_node)
        if my_gate == math.inf or opp_gate == math.inf:
            return False
        if opp_gate > FARM_RUSH_GATE_ETA:
            return False
        if opp_gate > my_gate + FARM_RUSH_GATE_MARGIN:
            return False
        # 移动中的对手必须朝宫门净推进；否则很可能只是 farmer 追任务路过。
        if opp.get("routeEdgeId") and opp.get("currentNodeId") and opp.get("nextNodeId"):
            dist = state.graph.all_frames(opp["currentNodeId"])
            cur_gate = dist.get(state.gate_node, math.inf)
            next_gate = state.graph.all_frames(opp["nextNodeId"]).get(
                state.gate_node, math.inf)
            if next_gate + FARM_RUSH_PROGRESS_EPS >= cur_gate:
                return False
        return True

    @staticmethod
    def _opp_farming_at(state, node_id):
        """对手停靠在该节点且正在读任务条（共点对峙豁免的"同桌"判定）。

        V3.26.2 再收紧（seed15 A/B 二次抓获）："仅停靠"仍误伤——camper
        沿途停 2 帧领资源也算停靠，豁免在开局共点开火即输走廊。农任务
        读条（currentProcess 带 taskId）才是"它坐下吃这桌菜"的实锤；
        它正在吃的那一个由 _opp_processing_task 排除，我们接其余的。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        if opp.get("routeEdgeId") or opp.get("currentNodeId") != node_id:
            return False
        proc = opp.get("currentProcess") or {}
        return bool(proc.get("taskId"))

    @staticmethod
    def _anchor_node(state):
        """规划起点：停靠节点，或移动中的当前目标节点。"""
        me = state.me
        if me.get("routeEdgeId"):
            return me.get("nextNodeId") or me.get("currentNodeId")
        return me.get("currentNodeId")

    def _time_penalty_fn(self, state):
        """真实时间惩罚：阻挡处理代价 + 固定处理站读条。

        只含真的会花掉的帧数，用于交付截止 slack —— 不能混入价值定价！
        （V3.4 教训：鲜度因子+阴影混进 ETA 后，真实地图开局估 542 帧、
        slack=-26，第 2 帧就进抢救模式，资源/任务全部熔断。）
        """
        gate, term = state.gate_node, state.terminal_node

        def penalty(nid):
            p = 0
            if state.enemy_guard(nid):
                p += GUARD_PENALTY
            if state.has_obstacle(nid):
                p += OBSTACLE_PENALTY
            node = state.node(nid)
            # 固定处理站必须处理完才能离站（登船7/水路换运6/入关5/宫前5）
            # 宫门验核帧数由 plan() 单独计，终点无处理，都不重复算
            if nid not in (gate, term):
                proc_type = node.get("processType")
                if proc_type and proc_type != "VERIFY":
                    p += node.get("processRound", 0) or 0
            return p
        return penalty

    def _penalty_fn(self, state):
        """价值惩罚 = 真实时间 + 对手阴影（用于方案比较/寻路选边）。"""
        shadow = self._shadow_nodes(state)
        time_penalty = self._time_penalty_fn(state)

        # 阴影×漏斗去重（V3.27）：漏斗模型已按时机精确定价的那个咽喉，
        # 不再叠粗粒度阴影罚（用上一次 ctx 的咽喉，容一帧滞后——
        # _funnel_ctx 自身要调本函数，实时取会成环）
        dedup = self._last_choke if self.SHADOW_FUNNEL_DEDUP else None

        def penalty(nid):
            p = time_penalty(nid)
            if nid in shadow and nid != dedup:
                node = state.node(nid)
                if node.get("nodeType") in CHOKE_TYPES:
                    shadow_pen = self.SHADOW_CHOKE_PENALTY
                    if self._can_break_expected_guard(state, nid):
                        shadow_pen = max(SHADOW_NODE_PENALTY,
                                         shadow_pen * self.BREAK_SHADOW_CHOKE_MULT)
                    p += shadow_pen
                else:
                    p += SHADOW_NODE_PENALTY
            return p
        return penalty

    def _shadow_nodes(self, state):
        """对手前进路线上、且会先于我们到达的节点集（按帧缓存）。"""
        if self._shadow_cache[0] == state.round:
            return self._shadow_cache[1]
        shadow = set()
        opp = state.opp
        me = state.me
        if opp and not opp.get("delivered") and not opp.get("retired") and state.graph:
            opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
            my_pos = me.get("nextNodeId") or me.get("currentNodeId")
            if opp_pos and my_pos and opp_pos != my_pos:
                opp_dist = state.graph.all_frames(opp_pos)
                my_dist = state.graph.all_frames(my_pos)
                _, opp_path = state.graph.shortest_path(opp_pos, state.gate_node)
                for n in opp_path[1:]:  # 对手脚下的点不算
                    if n == state.terminal_node:
                        continue
                    if opp_dist.get(n, math.inf) + SHADOW_MARGIN \
                            < my_dist.get(n, math.inf):
                        shadow.add(n)
        self._shadow_cache = (state.round, frozenset(shadow))
        return self._shadow_cache[1]

    def _opp_path_nodes(self, state):
        """对手去宫门的「合理走廊」节点并集（按帧缓存）。

        对手不一定走它的最短路（实测 demo 的 Dijkstra 最优是水路、实际走
        官道），单一路径预测会让拒止判定系统性失灵。取其所有总长不超过
        最优 ×1.25 的首跳分支路径的并集，作为它可能经过的节点集。
        """
        if self._opp_path_cache[0] == state.round:
            return self._opp_path_cache[1]
        nodes = set()
        opp = state.opp
        g = state.graph
        if opp and not opp.get("delivered") and not opp.get("retired") and g:
            opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
            if opp_pos:
                best, path = g.shortest_path(opp_pos, state.gate_node)
                nodes = set(path)  # 含其脚下/正在前往的点（到站就会顺手领）
                if best < math.inf:
                    for nb, e in g.neighbors(opp_pos):
                        f_nb = g.edge_frames(e)
                        d, p2 = g.shortest_path(nb, state.gate_node)
                        if p2 and f_nb + d <= best * 1.25:
                            nodes.update(p2)
                            nodes.add(nb)
        self._opp_path_cache = (state.round, frozenset(nodes))
        return self._opp_path_cache[1]

    def _time_edge_cost_fn(self, state):
        """真实时间边成本：只含天气移动税（生效全额，近期预告半额）。"""
        weather = state.weather or {}
        active = weather.get("active") or []
        forecast = weather.get("forecast") or []

        def edge_cost(edge, base_frames):
            rt = edge.get("routeType")
            wmult = 1.0
            for w in active:
                tax = P.WEATHER_MOVE_TAX.get((w.get("type"), rt))
                if tax:
                    wmult = max(wmult, tax / 1000.0)
            for w in forecast:
                tax = P.WEATHER_MOVE_TAX.get((w.get("type"), rt))
                if tax and (w.get("startRound", 10 ** 9) - state.round) \
                        <= FORECAST_HORIZON:
                    wmult = max(wmult, 1.0 + (tax / 1000.0 - 1.0) * 0.5)
            return base_frames * wmult
        return edge_cost

    def _edge_cost_fn(self, state):
        """价值边成本 = 时间成本 × 路线鲜度定价（用于方案比较/寻路选边）。

        鲜度定价与天气耦合：暴雨中的水路 0.045×1.3 > 0.05，
        "水路更保鲜"在雨中反转；酷暑全图等比放大差距。
        """
        time_cost = self._time_edge_cost_fn(state)
        farm_pressure = self.farm_rusher_pressure(state)
        active_types = {w.get("type") for w in (state.weather or {}).get("active") or []}

        def edge_cost(edge, base_frames):
            rt = edge.get("routeType")
            decay = P.ROUTE_FRESH_DECAY.get(rt, P.IDLE_FRESH_DECAY)
            region = 1.0
            for wt in active_types:
                region = max(region, WEATHER_FRESH_REGION.get((wt, rt), 1.0))
            scale = 1.5 if P.HOT in active_types else 1.0
            mult = 1.0 + (decay * region - P.IDLE_FRESH_DECAY) * scale * 1.8 / _FV
            cost = time_cost(edge, base_frames) * mult
            if farm_pressure and rt == P.MOUNTAIN:
                cost += FARM_RUSH_MOUNTAIN_PENALTY
            return cost
        return edge_cost

    @staticmethod
    def _has_our_scout_mark(state, node_id):
        for m in state.node(node_id).get("scouted") or []:
            if m.get("teamId") == state.my_team and m.get("remainingTriggers", 1) > 0:
                return True
        return False

    def _opp_eta(self, state, node_id):
        """对手到目标节点的帧数，含路线边上的剩余进度。

        V3.7 修复：对手在边上时曾把 nextNodeId 当作已到达（ETA=0），
        导致主动设卡的时机判断（教科书场景：我在武关、它在半路）永远不成立。
        """
        opp = state.opp
        edge_remain = 0.0
        if opp.get("routeEdgeId") and opp.get("nextNodeId"):
            total = opp.get("edgeTotalMs") or 0
            done = opp.get("edgeProgressMs") or 0
            edge_remain = max(0, total - done) / 1000.0  # 对手速度按基准估
            opp_node = opp.get("nextNodeId")
        else:
            opp_node = opp.get("currentNodeId")
        if not opp_node:
            return math.inf
        f, path = state.graph.shortest_path(opp_node, node_id)
        return edge_remain + f if path else math.inf

    @staticmethod
    def _opp_processing_task(state, task):
        proc = state.opp.get("currentProcess") or {}
        return proc.get("taskId") == task.get("taskId")

    def _time_detour(self, state, cur, pos):
        """真实时间口径的绕路帧数（用于交付截止的硬约束）。

        V3.10.1 修正：硬约束曾拿价值帧（含鲜度因子/障碍/阴影的膨胀值）
        去比时间口径的 slack —— 山冰线被虚高的 90 价值帧卡在 83 slack 外，
        而真实时间绕路只有 ~50 帧。可行性看时间，优劣看价值。
        """
        key = (state.round, cur)
        cache = getattr(self, "_time_detour_cache", None)
        if not cache or cache[0] != key:
            pen_t = self._time_penalty_fn(state)
            ec_t = self._time_edge_cost_fn(state)
            tg_t, _ = state.graph.shortest_path(cur, state.gate_node, 1000,
                                                pen_t, ec_t)
            self._time_detour_cache = (key, pen_t, ec_t, tg_t)
            cache = self._time_detour_cache
        _, pen_t, ec_t, tg_t = cache
        f_to, p1 = state.graph.shortest_path(cur, pos, 1000, pen_t, ec_t)
        f_back, p2 = state.graph.shortest_path(pos, state.gate_node, 1000,
                                               pen_t, ec_t)
        if not p1 or not p2:
            return math.inf
        return max(0, f_to + f_back - tg_t)

    def _backtrack_tax(self, state, cur, target):
        """目标需要经由刚离开的节点回头时的附加帧数（迟滞）。"""
        if not self.back_node or state.round >= self.back_until or target == cur:
            return 0
        if self.back_node == target:
            return BACKTRACK_PENALTY
        nxt = state.graph.next_hop(cur, target)
        return BACKTRACK_PENALTY if nxt == self.back_node else 0

    # ================= 马匹预留 =================

    def horses_reserved(self, state):
        """需要为「消耗马匹类任务」(T06 争马换乘) 保留的马匹数。

        平台实测教训：r91 骑掉唯一的短程马只省 ~2 帧（≈0.5 分），r300
        站在 S09 面对刷出来的 T06 却做不了（RESOURCE_NOT_ENOUGH ×12），
        S09/S04 两个 T06 全过期，任务分卡在 60。
        T06 实例是中途刷新的，所以除了看当前任务列表，还要看地图配置
        里有没有 T06 候选点（有就迟早会刷）。
        """
        if state.phase == P.PHASE_RUSH:
            return 0  # 冲刺阶段任务停刷，马全部用来赶路
        if self.race_mode(state):
            # 竞速窗口（V3.18）：马是走廊竞速武器不是存款——T06 是"可能刷
            # 出来的 ≤45 边际分"，漏斗先手是"确定的 45~80 帧税差 + 设卡权"。
            # 速度手段全留给"输了以后"（旧行为：疾行令只在 slack<0 才发）
            # 正是 audit 缺口 1 的资源侧表现
            return 0
        base = state.me.get("taskScore", 0) or 0
        if base >= 110:
            return 0  # 里程碑拿满，边际收益剩 45/个，速度更值钱

        def needs_horse(template_id):
            if template_id == "T06":   # 任务书 5.2：T06 消耗快马/短程马，规则固定
                return True
            tpl = state.task_templates.get(template_id) or {}
            return any(rt in (P.FAST_HORSE, P.SHORT_HORSE)
                       for rt in tpl.get("requiredResourceTypes") or [])

        # 当前任务列表里有可做的马匹任务
        for t in state.claimable_tasks():
            if self.blacklist.get(t["taskId"], 0) > state.round:
                continue
            if needs_horse(t.get("taskTemplateId")):
                return 1
        # 地图会刷马匹任务且比赛还早（实例随时可能出现）
        if state.round < 350 and any(needs_horse(tid)
                                     for tid in state.task_candidates):
            return 1
        return 0

    # ================= 反馈 =================

    def blacklist_task(self, task_id, until_round):
        self.blacklist[task_id] = until_round
