"""策略层。

Strategy 是接口；BaselineStrategy 是能完整跑通「赶路 → 站点处理 → 验核 → 交付」
主线的基线实现；PlannerStrategy 在其上接入任务规划器（保 90 冲 110+）、
小分队探路和窗口出牌博弈。
- 每帧返回 actions[]（最多 1 主车队动作 + 1 小分队动作 + 1 窗口出牌 + 1 急策）；
- 用 events[]/actionResults[] 反馈修正本地状态（如站点处理是否完成）。
"""
import math
import random

from . import protocol as P
from .planner import (TaskPlanner, FUNNEL_FIRST_WEATHER,
                      FUNNEL_WEATHER_GAP, RUSH_EARLIEST,
                      marginal_task_value)

# （V3.25 撤下按 playerId 的对手手册：地图会变、对手会变，ID 定制是
# 过拟合——用户纠偏。前推偏置的激活改为对手位置/行为在线识别，见
# PlannerStrategy._fwd_rush_tick）


class Strategy:
    def on_start(self, state):
        """收到 start 后调用一次。"""

    def decide(self, state):
        """每帧调用，返回 actions 列表（可为空）。"""
        return []


class BaselineStrategy(Strategy):
    # 绕行惩罚：被阻挡节点在寻路中的附加帧数（可绕就绕，绕不开仍会走过去再处理）
    BLOCK_PENALTY = 120
    # 鲜度低于该值且手里有冰鉴时使用（阈值 70 之下每 10 点掉 1 篓好果）
    USE_ICE_BELOW = 72

    def __init__(self, logger=None):
        self.log = logger
        self._last_stationary_node = None   # 上一次停靠的节点
        self._processed_here = False        # 当前停靠节点的固定处理是否已完成/无需处理

    # ================= 主入口 =================

    def decide(self, state):
        self._absorb_feedback(state)
        actions = []

        # 1) 窗口出牌（每帧最多 1 张，多窗口时其余自动弃权）
        contests = state.my_open_contests()
        if contests:
            c = contests[0]
            actions.append(P.a_window_card(c["contestId"], self.pick_card(state, c)))

        # 2) 主车队动作
        main = self.main_action(state)
        if main:
            actions.append(main)

        return actions

    # ================= 反馈吸收 =================

    def _absorb_feedback(self, state):
        me = state.me
        cur = me.get("currentNodeId")
        stationary = not me.get("routeEdgeId")

        # 到达新节点：该站处理状态重置
        if stationary and cur != self._last_stationary_node:
            self._last_stationary_node = cur
            self._processed_here = False

        for action, code in state.my_rejections():
            if code == P.E_PROCESS_REQUIRED:        # 想走但没处理完 -> 先处理
                self._processed_here = False
            elif code == P.E_PROCESS_NOT_AVAILABLE:  # 此站无处理流程
                self._processed_here = True
            if self.log:
                self.log.info("r%d rejected: %s %s", state.round, action, code)

        for e in state.my_events("PROCESS_COMPLETE", "VERIFY_GATE_COMPLETE"):
            self._processed_here = True

    # ================= 主车队决策 =================

    def main_action(self, state):
        me = state.me
        if not me or me.get("retired"):
            return None

        st = me.get("state")
        # 处理/验核/休整/窗口/强制通行中：提交动作不生效，等就行
        if st in P.BUSY_STATES:
            return None
        if me.get("delivered"):
            return None  # 交付后除 WAIT 外都算违规，空动作最安全

        cur = me.get("currentNodeId")
        moving = bool(me.get("routeEdgeId"))
        if moving:
            return None  # 系统等待会继续前进；改道策略留给后续版本

        gate, terminal = state.gate_node, state.terminal_node
        verified = me.get("verified")

        # --- 终点交付 ---
        if cur == terminal:
            if verified and me.get("goodFruit", 0) > 0 and me.get("freshness", 0) > 0:
                return P.a_deliver()
            return P.a_wait()

        # --- 宫门验核（只在 RUSH 阶段开放） ---
        if cur == gate and not verified:
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()  # 宫宴冲刺未触发，等待

        # --- 固定处理站点：先完成处理才能离站 ---
        node = state.node(cur)
        needs_process = (node.get("processType") and node.get("processType") != "VERIFY"
                         and node.get("processRound", 0) > 0)
        if needs_process and not self._processed_here:
            return P.a_process()

        # --- 机会性保鲜 ---
        res = me.get("resources") or {}
        if me.get("freshness", 100) < self.USE_ICE_BELOW and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)
        stock = node.get("resourceStock") or {}
        if stock.get(P.ICE_BOX, 0) > 0 and res.get(P.ICE_BOX, 0) == 0:
            return P.a_claim_resource(cur, P.ICE_BOX)

        # --- 赶路：未验核去宫门，已验核去终点 ---
        target = terminal if verified else gate
        nxt = self._route_next_hop(state, cur, target)
        if nxt is None:
            return P.a_wait()

        # 下一站被挡且绕不开：先尝试清障（好果够时），否则等待
        if state.has_obstacle(nxt):
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        if state.enemy_guard(nxt):
            return P.a_wait()  # 攻坚/强制通行留给后续版本

        return P.a_move(nxt)

    def _route_next_hop(self, state, cur, target):
        def penalty(nid):
            return self.BLOCK_PENALTY if state.is_blocked(nid) else 0

        return state.graph.next_hop(cur, target, state.my_speed(), penalty)

    # ================= 窗口出牌 =================

    def pick_card(self, state, contest):
        """基线：有免费强行就打，否则弃权。子类可覆盖做克制博弈。"""
        if state.has_move_buff():
            return P.CARD_QIANG_XING  # 有马类/疾行令增益时强行免消耗
        return P.CARD_ABSTAIN


class PlannerStrategy(BaselineStrategy):
    """V1：任务规划（保 90 冲 110+）+ 小分队探路 + 窗口出牌升级。"""

    SCOUT_RESEND_GAP = 25       # 同一目标探路重发间隔（防止在途期间重复派人）
    SCOUT_MAX_ETA = 40          # 只探 40 帧内能赶到的目标（标记寿命 45 帧）
    # 走廊人手预留（V3.15）：过验核前非削弱派遣必须留下的人手底仓。
    # 4 = 2 次削弱 = 防 6 削到 4（好果 2 篓即可拆）再留一次余量；replay20/56
    # 实锤：人手在探路/边上削弱里买穿后，走廊第二张卡只能干等风化
    SQUAD_CORRIDOR_RESERVE = 4
    GATE_SCOUT_FROM = 355       # 宫门验核最早 ~390 帧，此前派的标记必然过期
    IDLE_TASK_MAX_PROC = 6      # 空转帧只顺手吃短任务，长读条仍交给规划器
    IDLE_TASK_TRAP_WAIT = 6     # farmer 防陷阱等待的可兑现空窗
    HORSE_CLAIM_MIN_SAVE = 3    # 顺路拿马至少要覆盖 2 帧领取读条再多赚 1 帧
    # 顺路领取清单：默认只拿确定收益高的冰鉴/马。文书类出牌池收益不稳定，
    # 2 帧读条在小分差局会反噬；情报仍由 _should_claim_intel_en_route 动态加入。
    CLAIM_EN_ROUTE = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE)
    # 竞速模式下的收缩清单（V3.18）：只领交付硬件与速度资源
    RACE_CLAIM_ONLY = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE)
    CLAIM_LIMIT = {P.ICE_BOX: 2}    # 冰鉴多多益善（+10 鲜度 ≈ 18 分），其余各 1
    USE_ICE_BELOW = 86          # 首关前先留 1 个坏果弹药，低到 86 左右再补冰
    USE_ICE_LATE_BELOW = 91     # 过首关/冲刺末端不再故意养坏果，恢复保鲜阈值
    ICE_AMMO_TARGET_BAD = 1     # 防 6 卡：1 坏果 + 2 好果即可一击破
    ICE_PRE_AMMO_CRITICAL = 80  # 极端低鲜度兜底，避免为等坏果把鲜度打穿

    MIN_GOOD_RESERVE = 5        # 攻坚投入好果时保留的底仓（交付要求好果 > 0）
    WEAKEN_RESEND_GAP = 12      # 同一设卡的削弱重发间隔（落地延迟 ~3-5 帧）
    EDGE_WEAKEN_RESERVE = 4     # 边冻结常规保留：两次削弱弹药
    EDGE_WEAKEN_RESCUE_AVAIL = 2    # 已卡死在关键口边上时，允许动用最后一组
    EDGE_WEAKEN_RESCUE_DEFENSE = 5  # 第一刀/风化后再救，不碰满防新卡
    EDGE_WEAKEN_RESCUE_ROUND = 220  # 中盘以后被卡死时，最后一组人手也要救命

    # ---- 主动设卡（V3）----
    # 咽喉类节点 + 宫前驿（V3.28：2839 的二卡就落在 S13 PALACE_STATION，
    # r450 掐 RUSH 起点收尾段 35~70 帧税——普通节点免底价，这张卡对
    # 领跑者近乎免费；此前类型门把它整个排除在我们的武器库外）
    GUARD_NODE_TYPES = {"KEY_PASS", "PASS", "MOUNTAIN_PASS", "GATE",
                        "PALACE_STATION"}
    GUARD_MIN_OPP_ETA = 8       # 对手至少 8 帧后才到（4 帧读条 + 生效余量）
    GUARD_MAX_OPP_ETA = 150     # 太远则风化/悬赏先到，白设
    # V3.12：80 → 65。V3.7 修了 ETA 度量后 4 局仍 0 次设卡——replay31 领跑局
    # 仿真：S10/S11 咽喉停靠帧 slack 分布 70~84，全被 80 拦掉（SAFETY_MARGIN
    # 60 已内含一道保险，等效要求 140 帧真余量）。65 落在实测分布之下、
    # 仍留 4 帧读条的 16 倍缓冲。
    GUARD_SLACK_MIN = 65        # 自己交付余量充足才花这 4 帧
    GUARD_ROUTE_TOLERANCE = 15  # 判断该节点是否在对手高效路线上的容差（帧）
    GUARD_REAR_ROUTE_TOLERANCE = 3  # 尾段单边卡不押近似五五开的分叉
    GUARD_RETRY_GAP = 40        # 同一节点设卡重试间隔
    GUARD_REGUARD_WINDOW = 18   # 我方卡刚被打穿且仍占点时，允许补卡反打
    GUARD_SQUAD_RICH_SKIP = 4   # farmer/跟随者攒着远程削弱弹药时，不喂好果
    GUARD_REGUARD_SQUAD_SKIP = 2
    # 关隘热设卡（V3.18）：刚赢下漏斗竞速、对手正被汇过来（ETA ≤60）时，
    # 4 帧读条 + 1~3 好果换对手 45+ 帧死等/满防税，是竞速胜利的兑现动作
    # ——65 的常规闸门在这个场景下把"过关隘必设卡"整档拦掉（对手 2614
    # 的同款打法：r314 立卡后边卡边农）。热窗口降到 25，仍留 6 倍读条缓冲
    GUARD_SLACK_HOT = 25
    GUARD_HOT_OPP_ETA = 60      # 对手到本关隘的 ETA 在此内算"热"
    GUARD_REAR_OPP_ETA = 130    # 普通汇入点回手卡窗口（路径必经时）
    GUARD_REAR_SLACK = 45       # 普通汇入点成本低，但仍要保交付余量
    GUARD_REAR_RUSH_SLACK = 20  # RUSH 起点二卡：只在仍有读条余量时兑现
    GUARD_REAR_TYPES = {"PALACE_STATION"}  # 普通点默认只放宫前驿这类尾段关键点
    # 追分合流卡（V3.33）：0 vs 60 这类前段任务分落后局，若我们抢先到
    # S09 式普通合流点且对手 8~18 帧后必经，4 帧读条换对手漏斗前停顿/
    # 攻坚税，是追分而非保守交付动作，不能套普通反手卡 45 slack。
    GUARD_CATCHUP_OPP_ETA = 18
    GUARD_CATCHUP_SLACK = 5
    GUARD_CATCHUP_TASK_GAP = 60
    GUARD_CATCHUP_MY_TASK_MAX = 30
    # 高分合流拒止卡（vs2931）：r224 我们先到 S09，对手 26 帧后必经且
    # 任务分 90:60 领先。旧 catchup 门只认 0:60，导致白白放它进站拿 T013。
    GUARD_SCORE_LEAD_OPP_ETA = 45
    GUARD_SCORE_LEAD_SLACK = 25
    GUARD_SCORE_LEAD_TASK_MIN = 90
    GUARD_SCORE_LEAD_TASK_GAP = 30
    # 确定必经拒止卡（V3.41）：比 3.40 再激进半档。普通合流点若
    # 已确认在对手高效路径上、对手 45 帧内到达，且对手至少小幅任务分
    # 领先，就把 4 帧读条兑现成通行税，而不是等到 90:60 这种强信号。
    GUARD_DENIAL_OPP_ETA = 45
    GUARD_DENIAL_SLACK = 20
    GUARD_DENIAL_TASK_MIN = 60
    GUARD_DENIAL_TASK_GAP = 15
    GUARD_BOUNTY_EXPOSE_ETA = 30  # 30 帧后破卡会生成/结算悬赏，领先局避开送分卡
    GUARD_MIN_ARRIVAL_DEFENSE = 3
    GUARD_REAR_MIN_ARRIVAL_DEFENSE = 4
    GUARD_PARTING_SLACK = -40   # S10 所有权/临别卡按硬截止算，允许吃掉安全垫

    # ---- 防中边陷阱（V3.5，V3.12 删证据门）----
    # 设卡必须站在节点上：对手占着/将先到我们的下一跳时，上边就可能被掐点
    # 冻结（实测连环两次：S10 花 6 人手解冻，S11 无人手可用冻到终场未交付）。
    # 它离开该节点后就永远无法在那里设卡 —— 等它走，留卡就站在节点上攻坚拆。
    # 任务书 8.2：移动中只能 WAIT/续走/用马，不能回头——中边冻结在规则上
    # 无解，预防是唯一手段。V3.9 曾加"设卡前科"证据门防误伤，但对手的第一
    # 张卡必然没有前科（replay36: r295 几何+地形全中被前科门放行，冻 195 帧
    # 零交付）；地形门已把误伤压到每局 ≤1 次咽喉等待（≤30 帧 ≈ 6.6 分），
    # 对比冻结 180+ 帧 / 零交付 500 分级，陷阱概率 ≥5% 即回本 → 删证据门。
    TRAP_GUARD_FRAMES = 4       # 设卡读条帧数（对手到点后需要的成卡时间）
    TRAP_ORDINARY_WAIT = 45     # 普通节点驻扎等待预算（V3.22）：农夫型
                                # 久驻不狙击，等满即硬闯；45 ≈ 它一次任务
                                # 波次间隔的量级，也 < 被掐的冻结代价
    # farmer 咽喉有界等待（V3.29，replay93 抓获）：定价层已按 farmer
    # 先验 0.35 判官道便宜，保命层却不读画像无上限死等——replay93 在
    # S09 对着"蹲武关农波次、整局零设卡"的 2738 站了 109 帧，r598 才
    # 交付（离收盘 2 帧），差点把 716 分等成未交付。教义修正：无上限
    # 等待自身在钟表面前就是灾难级风险。三重门（画像 farmer + 全场未
    # 见卡 + 它此刻停靠在读任务条）全中时，等待封顶后走边——它每张
    # 任务读条 4 帧内规则上无法起手设卡，走边窗口有真实掩护；它若真
    # 变脸落卡，_guard_seen 立刻关死本豁免，一局至多上当一次。
    # camper / 见过卡 / 非农读条对手照旧无上限等待（V3.15 教义不动）
    TRAP_FARMER_WAIT = 25
    # replay95：farmer 还在赶往咽喉时不会命中上面的 camped 读条门，
    # S09+S10 合计白等 36 帧，同分输用时。高任务分、零设卡 farmer 的
    # 收敛等待也要有预算，但比 camped 门更谨慎，只作短观察窗。
    TRAP_FARMER_CONVERGE_WAIT = 12
    TRAP_FARMER_CONVERGE_TASK = 90
    TRAP_CONVERGE_ORDINARY = False  # 实验开关：收敛分支是否也防普通节点
                                # （无界版被电池证伪 camper 34/48；有界
                                # 变体的配对对照见 trap-gate 实验脚本）
    TRAP_ORDINARY_CONVERGE_ETA = 24  # 普通节点长边收敛窄门：对手快先到才让行
    TRAP_ORDINARY_CONVERGE_EDGE = 25 # 我方边足够长，对手 4 帧起卡追得上才等
    TRAP_CAMPED_ORDINARY = True     # 驻扎分支防普通节点（V3.22 主开关，
                                # 语料=2839 第 4 局 S09 掐踏边）
    TRAP_WAIT_MAX = 30          # 陷阱等待的日志告警阈值（V3.15 起不再硬闯：
                                # replay56 上限到点硬闯 71 帧长边被 r314 掐点冻死）
    # 注意：不设"截止吃紧就赌一把"的例外 —— slack 越紧冻结越致命
    # （等待成本 10~30 帧 vs 冻结成本 180+ 帧），对峙上限已兜底防赖
    # 陷阱等待的租买止损（V3.18）：V3.15 删对峙上限后等待无上界，蹲点者
    # 停靠在我们下一跳农任务 = 零成本冻结我们的推进（replay36 里 2614 还
    # 花了设卡成本，懂这套逻辑的对手连卡都不用设）。修法不是回退硬闯
    # （replay56 教训不动），而是 ski-rental：等待帧数一旦超过换走廊的
    # 绕路差价就改道，总代价 ≤ 事后最优的 2 倍；绕不开的真漏斗口照旧等待
    TRAP_AVOID_PENALTY = 900    # 改道承诺期间被避节点的寻路附加帧数
    TRAP_AVOID_WINDOW = 120     # 改道承诺的有效窗口（帧），对手离开即提前解除
    TRAP_DEADLINE_ESCAPE_SLACK = -20
    TRAP_DEADLINE_ESCAPE_WAIT = 45

    # ---- 尾段蹲刷（V3.10）----
    # 任务刷新跟在车队身后：领跑者吃冰，跟随者吃刷新（29/30/31 三局对手
    # 全靠尾段刷新农到 180 任务分，我们前 200 帧后零任务）。余量充足且
    # 里程碑未拿满时，站在任务候选点上等刷新，比冲刺快 40 帧（≈8分）值钱。
    LOITER_MIN_SLACK = 110      # 蹲刷要求的最小交付余量（帧）
    LOITER_BASE_CAP = 110       # 任务基础分达到该值后不再蹲（末档里程碑已到手）
    LOITER_BUDGET = 50          # 整局蹲刷总预算（帧），有限下注
    # S10 水路收租（V3.51/V3.52）：120 后先占武关就是一阶胜负条件；
    # 站住截任务波次，或等对手踏边后由临别/身体设卡冻结兑现。
    S10_TOLL_BASE = 120
    S10_TOLL_DENY_BASE = 150
    S10_TOLL_MIN_SLACK = -40
    S10_TOLL_MAX_OPP_ETA = 170
    S10_TOLL_ROUTE_TOLERANCE = 20
    S10_TOLL_BUDGET = 220
    S10_TOLL_TASK_MAX_PROC = 5

    # ---- 情报（INTEL，V3.12）----
    INTEL_DISTANCE_LIMIT = 15   # 任务书 3.3.4：目标距离超过 15 时使用被拒

    # ---- 小分队远程清障（V3.12）----
    SQUAD_CLEAR_RESEND_GAP = 18   # 落地延迟上限 15 帧 + 余量，防重复派人

    # ---- 小分队增援（V3.12 / V3.41）----
    REINFORCE_DEFENSE_FLOOR = 4    # 自家设卡防守值跌破该值才续
    REINFORCE_RESEND_GAP = 30      # 同一节点续防重试间隔

    def __init__(self, logger=None):
        super().__init__(logger)
        self.planner = TaskPlanner(logger)
        self._scout_sent = {}   # nodeId -> 派出帧
        self._rush_tactic_tried = False  # 护果令只尝试一次，被拒也不无限重试
        self._weaken_sent = {}  # nodeId -> 派出帧（削弱敌卡）
        self._guard_first_seen = {}  # nodeId -> 首见该敌卡的帧（临别卡宽限计时）
        self._guard_leave_probe = {} # nodeId -> 强通前离场预判开始帧
        self._weaken_target = None       # 本帧主车队让 squad_action 去削弱的目标
        # 上次强制通行到达节点。6.3.2 重复限制的准确语义（V3.18 修正）：
        # "主车队停在该节点时不能再次提交强制通行，离开后又回到该节点时仍不能提交"
        # ——禁的是【从】该节点再次发起，不是再次通行【进入】该节点。
        # 旧判断 target != _last_forced_node 方向拦反：站在记录节点上对邻站发
        # 强通会被 FORCED_PASS_REPEAT 拒掉且逐帧重试卡死（武关→潼关双咽喉局
        # 必然踩中）；而隔了一次强通后再次强通进同一节点其实合法却被自己禁了
        self._last_forced_node = None
        self._squad_spent = 0            # 本地人手账本（服务端字段缺失时兜底）
        self._guard_sent = {}            # nodeId -> 设卡提交帧（防重试风暴）
        self._own_guard_seen = {}        # nodeId -> (active, defense)，用于识别被打穿
        self._own_guard_broken = {}      # nodeId -> 最近一次我方卡失效帧
        self._trap_wait = (None, 0)      # (等待的目标节点, 连续等待帧数)
        self._trap_avoid = (None, -1)    # (租买改道要绕开的节点, 承诺到期帧)
        self._opp_card_hist = {}         # 对手本局出牌频次（WINDOW_CARD_REVEAL）
        self._window_draw_pressure = {}  # (target, type) -> (累计平局压力, 最近帧)
        self._window_suppress_until = {} # (target, type) -> 服务器重复平局抑制到期帧
        self._rng = None                 # (matchId, playerId) 派生种子，回放可复现
        self._opp_stationary = (None, 0)  # (对手停靠节点, 起始帧)——驻扎判定
        self._fwd_rush = False           # 冲锋型对手识别结论（粘性）
        self._opp_min_gate_eta = float("inf")   # 对手宫门 ETA 历史最小值
        self._opp_retreated = False      # 对手曾回头（ETA 显著回升过）
        self._loiter_spent = 0           # 尾段蹲刷已用帧数（预算制）
        self._s10_toll_hold_spent = 0    # S10 终局驻守已用帧数（预算制）
        self._last_main_action = None    # 上一帧提交的主车队动作（拒绝反馈的 join 键）
        self._clear_sent = {}            # nodeId -> 小分队清障派出帧（防重试风暴）
        self._reinforce_sent = {}        # nodeId -> 小分队增援派出帧（防重试风暴）
        self._opp_profile = "unknown"    # 对手画像（V3.20）：unknown/camper，粘性
        self._prof_idle_choke = 0        # 对手在 KEY_PASS 闲置驻扎的累计帧数
        self._opp_ordinary_guard_seen = False

    # ---------- 每帧入口 ----------

    def decide(self, state):
        self._absorb_feedback(state)

        # 交付后除 WAIT/重复交付外任何主动动作每次扣 5 分（7.4）：
        # 窗口牌、小分队都不许再发；被动进 PASS 窗口按弃权处理不扣分
        if state.me.get("delivered") or state.me.get("retired"):
            return []

        actions = []
        plan = self.planner.plan(state)
        if self.log and state.round % 20 == 0:
            self.log.debug("plan: %r", plan)

        contests = state.my_open_contests()
        if contests:
            c = self._priority_contest(state, contests, plan)
            actions.append(P.a_window_card(c["contestId"], self.pick_card(state, c)))

        main = self.main_action(state, plan)
        if main:
            actions.append(main)

        squad = self.squad_action(state, plan)
        if squad:
            actions.append(squad)

        # 服务端行为：移动中提交只含小分队/窗口动作的包会暂停本帧推进
        # （镜像调测第 2 帧实测），补显式 MOVE 当前目标保持前进。
        # 例外：目标节点被敌卡冻结时进度本来就不走，MOVE 只会被拒，不补。
        me = state.me
        if (actions and me.get("state") == P.ST_MOVING and me.get("nextNodeId")
                and not state.enemy_guard(me["nextNodeId"])
                and not any(a["action"] in P.MAIN_ACTION_TYPES for a in actions)):
            actions.append(P.a_move(me["nextNodeId"]))
        self._last_main_action = next(
            (a for a in actions if a["action"] in P.MAIN_ACTION_TYPES), None)
        return actions

    # ---------- 反馈：任务被拒时临时拉黑 ----------

    def _absorb_feedback(self, state):
        prev_station = self._last_stationary_node
        super()._absorb_feedback(state)
        # 回头迟滞：到达新节点时，刚离开的节点进入迟滞窗口
        if prev_station and self._last_stationary_node != prev_station \
                and not state.me.get("routeEdgeId"):
            self.planner.back_node = prev_station
            self.planner.back_until = state.round + 40
        self._weaken_target = None

        # 冲锋型对手在线识别（V3.25，按位置/行为，不认 ID）：在途任务分
        # ≥30（蹲点型到关前恒 0）+ 走廊推进从未回头（农任务型会游走回撤）
        # + 未被画像为蹲点型 → 前推偏置激活（粘性；画像若后到蹲点型则
        # 撤销）。全局常开被 1344 局扫描证伪（camper 相位骰子），冲锋型
        # 专用收益 vs toller margin +32→+248
        self._fwd_rush_tick(state)

        # 敌卡消失（被拆/风化/失效）即重置宽限计时：同节点再立新卡重新起算
        for node_id in list(self._guard_first_seen):
            if not state.enemy_guard(node_id):
                del self._guard_first_seen[node_id]
                self._guard_leave_probe.pop(node_id, None)
        # 对手驻扎追踪（V3.19）：停靠在同一节点的起始帧，供"坐地户免宽限"
        # 与画像分类器用（原始口径：含做任务帧）
        opp = state.opp
        if opp and not opp.get("routeEdgeId") and opp.get("currentNodeId"):
            if self._opp_stationary[0] != opp["currentNodeId"]:
                self._opp_stationary = (opp["currentNodeId"], state.round)
        else:
            self._opp_stationary = (None, state.round)

        # 富点干等观测（V3.91）逐帧跑——不能挂在 _profile_tick 里，
        # 画像定型后那条链就停了，而干等证据恰恰在定型之后才累积
        if opp:
            self._dwell_tick(state, opp)

        # 对手画像（V3.20）：早期识别蹲点型，漏斗先验不等首卡提前升 1.0
        if self.PROFILE_ENABLED and self._opp_profile == "unknown" \
                and state.round <= self.PROFILE_WINDOW:
            self._profile_tick(state)
        self.planner.opp_profile = \
            self._opp_profile if self.PROFILE_ENABLED else "unknown"

        # 首见帧在吸收时全量记录（V3.18）：曾只在 _breakthrough 里 setdefault，
        # 走到卡前才起算宽限——存在已久的老卡（真蹲点）也被当"临别新卡"
        # 白等 8 帧。吸收时记录后，宽限只留给真正刚立的卡
        for node_id in state.nodes:
            if state.enemy_guard(node_id):
                self._guard_first_seen.setdefault(node_id, state.round)
                if state.node(node_id).get("nodeType") not in self.GUARD_NODE_TYPES:
                    self._opp_ordinary_guard_seen = True
        self._track_own_guard_breaks(state)

        # 租买改道承诺提前解除：对手离开被避节点（或已交付/退赛）后该走廊
        # 已干净，不再为一个不存在的威胁多绕路
        avoid, until = self._trap_avoid
        if avoid:
            opp = state.opp
            gone = (not opp or opp.get("delivered") or opp.get("retired")
                    or (opp.get("currentNodeId") != avoid
                        and opp.get("nextNodeId") != avoid))
            if state.round >= until or gone:
                self._trap_avoid = (None, -1)

        # 对手出牌画像（V3.18）：WINDOW_CARD_REVEAL 全公开，本局频率替代
        # "对可负担集均匀出牌"的先验（pick_card 拉普拉斯平滑加权）
        stale = [k for k, (_, r) in self._window_draw_pressure.items()
                 if state.round - r > self.WINDOW_DRAW_PRESSURE_DECAY]
        for k in stale:
            del self._window_draw_pressure[k]
            self._window_suppress_until.pop(k, None)
        for k, until in list(self._window_suppress_until.items()):
            if state.round > until:
                del self._window_suppress_until[k]
        for e in state.events:
            self._record_window_draw_pressure(state, e)
            if e.get("type") != "WINDOW_CARD_REVEAL":
                continue
            p = e.get("payload") or {}
            if p.get("playerId") == state.opp_id:
                card = p.get("card") or p.get("cardType")
                if card:
                    self._opp_card_hist[card] = \
                        self._opp_card_hist.get(card, 0) + 1
        for e in state.my_events("FORCED_PASS_END"):
            p = e.get("payload") or {}
            node = p.get("nodeId") or p.get("targetNodeId")
            if node:
                self._last_forced_node = node  # 规则：该节点不能再次强制通行
        last = self._last_main_action or {}
        for action, code in state.my_rejections():
            # 平台 ACTION_REJECTED 载荷缺 action 字段（replay20/36 全为 None，
            # 拉黑分支因此从未命中过）：用上一帧实际提交的主动作补齐
            act = action or last.get("action")
            if act == "CLAIM_TASK" and code in (
                    "TASK_REQUIREMENT_NOT_MET", "TASK_PROTECTED", "OBJECT_BUSY",
                    "TASK_EXPIRED", "TASK_NOT_FOUND", "WINDOW_DRAW_RETRY_LIMIT"):
                tid = last.get("taskId")
                proc = state.me.get("currentProcess") or {}
                tid = tid or proc.get("taskId")
                # 拒绝发生在上一帧，没有可靠 taskId 时拉黑当前计划任务
                if not tid:
                    plan = self.planner.plan(state)
                    tid = (plan.task or {}).get("taskId")
                if tid:
                    self.planner.blacklist_task(tid, state.round + 40)
                    if self.log:
                        self.log.info("blacklist task %s until r%d (%s)",
                                      tid, state.round + 40, code)

    @staticmethod
    def _window_pressure_key(payload):
        target = payload.get("targetNodeId") or payload.get("nodeId")
        ctype = payload.get("contestType")
        if not target or not ctype:
            return None
        return (target, ctype)

    WINDOW_DRAW_PRESSURE_DECAY = 90
    WINDOW_DRAW_BREAK_AFTER = 2
    WINDOW_SUPPRESS_FALLBACK = 15

    def _record_window_draw_pressure(self, state, event):
        etype = event.get("type")
        p = event.get("payload") or {}
        key = self._window_pressure_key(p)
        if not key:
            return
        if etype in ("WINDOW_CONTEST_DRAW", "WINDOW_CONTEST_REPEAT_SUPPRESSED"):
            count, _ = self._window_draw_pressure.get(key, (0, state.round))
            self._window_draw_pressure[key] = (count + 1, state.round)
            if etype == "WINDOW_CONTEST_REPEAT_SUPPRESSED":
                until = self._window_suppress_until_round(p)
                if until is None:
                    until = state.round + self.WINDOW_SUPPRESS_FALLBACK
                self._window_suppress_until[key] = max(
                    until, self._window_suppress_until.get(key, -1))
        elif etype in ("DOCK_CONTEST_WIN", "TASK_CONTEST_WIN",
                       "RESOURCE_CONTEST_WIN", "GATE_CONTEST_WIN",
                       "OBSTACLE_CONTEST_WIN"):
            self._window_draw_pressure.pop(key, None)
            self._window_suppress_until.pop(key, None)

    @staticmethod
    def _window_suppress_until_round(payload):
        for key in ("suppressUntilRound", "suppressUntil", "suppressedUntil",
                    "blockedUntilRound", "blockedUntil", "retryUntilRound",
                    "retryAfterRound", "untilRound"):
            val = payload.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return None
        return None

    # ---------- 对手画像（V3.20） ----------
    # 蹲点型的行为签名：在关隘型节点（KEY_PASS/PASS）上"闲着"——不在处理、不在
    # 被我们的卡挡着，就是站着（等资源起卡/农任务间隙/纯蹲）。镜像对手
    # 不会这么做：它路过关隘要么在走、要么在做任务（有 currentProcess）、
    # 要么在我们的卡前等风化（有我方邻卡）。这三类全部排除后按帧累计，
    # 达阈值即分类 camper（粘性，不回退）。
    # 价值：真 2614 式"先农一会儿再起卡"的坐地户，在它第一张卡落地前
    # 就把 FUNNEL_GUARD_PRIOR 提前升到 1.0——这正是 camper 局扫描里
    # prior 0.91 变体 +13 分/-2 死局的收益窗口（首卡后 _guard_seen 已覆盖）。
    PROFILE_ENABLED = True
    PROFILE_CAMP_IDLE = 15     # 关隘闲置累计帧阈值（过客路过≤4 帧，读条 4 帧）
    PROFILE_WINDOW = 400       # 分类窗口。走廊长边 30~60 帧，蹲点者到达武关
                               # 本身就要 ~250 帧（竞技场实测），窗口太小会在
                               # 它刚落座时关死采集。误报风险有界：首卡之后
                               # prior 已被 _guard_seen 定死，画像不再增量起效；
                               # 400 之后的关隘等待多为尾段战术对峙，不采
    # farmer 分类（V3.26）：在途任务分 ≥60（两个以上任务，不是顺手一个）
    # 且全场未见其任何设卡 → 农任务型，漏斗先验降档（planner.FUNNEL_
    # FARMER_PRIOR）。reports 三败局对手全是这个形态：农到 120~150、
    # 零设卡，我们却按 0.7 先验交漏斗保险费。误判为 farmer 的下行风险
    # 有界：它一落卡 _guard_seen 粘性升 1.0 覆盖本档；中边陷阱等待等
    # 灾难级防御不读画像，保持全额（保险只降"定价"，不降"保命"）。
    # 注意与 camper 的判定顺序：farmer 靠分数、camper 靠关隘闲置——
    # "先农满 60 再蹲关"的混合体会被先判成 farmer，其后的蹲守由陷阱
    # 等待兜底、落卡由 _guard_seen 兜底，不裸奔
    PROFILE_FARM_SCORE = 60

    FWD_RUSH_TASK_MIN = 30      # 在途任务分证据线（蹲点型到关前恒 0）
    FWD_RETREAT_TOL = 12        # 宫门 ETA 回升超过此值 = 它回头过（农任务
                                # 型游走特征；容差吸收边进度量化噪声）
    FWD_RUSH_DEPTH = 0.6        # 触发还要求对手已深入（宫门 ETA ≤ 60% 全
                                # 程）：冲锋型攒够任务分时必然已在走廊深处
                                # （2839 任务 30 时在 S07），农任务型攒分时
                                # 还在浅区（FarmerBot r82 在 S04）——不加
                                # 这道闸它会被误触发在最有害的开局窗口

    def _fwd_rush_tick(self, state):
        """冲锋型对手在线识别 → planner.forward_rush_opp（前推偏置开关）。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            self.planner.forward_rush_opp = self._fwd_rush
            return
        eta = self.planner._opp_eta(state, state.gate_node)
        # 对手逼近宫门后冻结回头追踪：它过宫门奔终点时宫门 ETA 会回升
        # ~27 帧（终局伪影，实测把 r181 起的正确识别在 r452 误撤销）
        if eta != float("inf") \
                and self._opp_min_gate_eta > self.FWD_RETREAT_TOL:
            if eta > self._opp_min_gate_eta + self.FWD_RETREAT_TOL:
                self._opp_retreated = True
            self._opp_min_gate_eta = min(self._opp_min_gate_eta, eta)
        total = self.planner._map_total(state)
        deep = total and eta != float("inf") \
            and eta <= self.FWD_RUSH_DEPTH * total
        if self._opp_profile == "camper" or self._opp_retreated:
            self._fwd_rush = False       # 蹲点画像/回头随时撤销
        elif not self._fwd_rush and deep \
                and (opp.get("taskScore", 0) or 0) >= self.FWD_RUSH_TASK_MIN:
            self._fwd_rush = True
            if self.log:
                self.log.info("opp identified as forward-rusher at r%d",
                              state.round)
        self.planner.forward_rush_opp = self._fwd_rush

    def _dwell_tick(self, state, opp):
        """富点干等观测（V3.91）：普通节点上的累计无读条闲置帧。

        2986 型官道农会在任务富点纯等刷新波；2839/toller"农不停步"、
        camper 只蹲关隘（节点类型天然排除）。停留 ≥5 帧后的闲置才计，
        滤掉路过/领取的帧噪声；被我方卡拦住的等风化不算（那是受害者
        不是农夫）。累计过线解锁 planner 的 FRONT_TEMPO 尾随。
        """
        if opp.get("delivered") or opp.get("retired") \
                or opp.get("currentProcess"):
            return
        node_id, since = self._opp_stationary
        if not node_id or state.round - since < 5:
            return
        if state.node(node_id).get("nodeType") \
                in ("KEY_PASS", "PASS", "GATE", "PALACE_STATION"):
            return
        for nid in [node_id] + [n for n, _ in state.graph.neighbors(node_id)]:
            g = state.node(nid).get("guard")
            if g and g.get("ownerTeamId") == state.my_team \
                    and g.get("active", g.get("defense", 0) > 0):
                return
        self.planner._opp_dwell_idle += 1

    def _profile_tick(self, state):
        opp = state.opp
        if not opp:
            return
        # farmer 分类的关隘排除（V3.26.1，电池抓获）：延迟 camper 变体
        # "先在武关农满 60 再落卡"会被误判 farmer（随机化 camper 12/48
        # 误判、seed15 从赢局退回未交付）。可分离信号：真农夫的农发生在
        # S07 驿站类普通节点（reports 三局全程如此），在关隘上农到 60 的
        # 对手下一步大概率就是回手卡——它站在关隘上时不分类，等它离开
        # 关隘再看（真农夫有的是普通节点帧可采）
        opp_pos = opp.get("currentNodeId")
        at_choke = (opp_pos and not opp.get("routeEdgeId")
                    and state.node(opp_pos).get("nodeType")
                    in ("KEY_PASS", "PASS"))
        if (opp.get("taskScore") or 0) >= self.PROFILE_FARM_SCORE \
                and not self.planner._guard_seen and not at_choke:
            self._opp_profile = "farmer"
            if self.log:
                self.log.info("opp profiled as FARMER at r%d (taskScore=%d)",
                              state.round, opp.get("taskScore") or 0)
            return
        node_id, _ = self._opp_stationary
        if not node_id:
            return
        # 关隘型节点：KEY_PASS（S10 武关）+ PASS（S03/S11）——蹲潼关与
        # 蹲武关是同一威胁形态（replay36 死局的 ~225 帧通行费来自双关卡）
        if state.node(node_id).get("nodeType") not in ("KEY_PASS", "PASS"):
            return
        if opp.get("currentProcess"):
            return          # 农任务/读条中不算闲置（镜像在关隘做任务不误伤）
        # 该点或邻点有我方有效卡 → 它是被卡住在等风化，不是蹲点
        for nid in [node_id] + [n for n, _ in state.graph.neighbors(node_id)]:
            g = state.node(nid).get("guard")
            if g and g.get("ownerTeamId") == state.my_team \
                    and g.get("active", g.get("defense", 0) > 0):
                return
        self._prof_idle_choke += 1
        if self._prof_idle_choke >= self.PROFILE_CAMP_IDLE:
            self._opp_profile = "camper"
            if self.log:
                self.log.info("opp profiled as CAMPER at r%d (idle %d @ %s)",
                              state.round, self._prof_idle_choke, node_id)

    # ---------- 主车队 ----------

    def _should_use_ice(self, state, plan=None):
        me = state.me
        res = me.get("resources") or {}
        if res.get(P.ICE_BOX, 0) <= 0:
            return False
        fresh = me.get("freshness", 100) or 0
        if fresh <= 0:
            return False
        cur = me.get("currentNodeId")
        if not cur:
            return False
        bad = me.get("badFruit", 0) or 0
        key_ahead = self.planner._key_pass_ahead(state, cur)
        late = (cur == state.terminal_node or me.get("verified")
                or state.phase == P.PHASE_RUSH or not key_ahead)
        if key_ahead and not late and bad < self.ICE_AMMO_TARGET_BAD:
            return fresh < self.ICE_PRE_AMMO_CRITICAL
        threshold = self.USE_ICE_LATE_BELOW if late else self.USE_ICE_BELOW
        return fresh < threshold

    def main_action(self, state, plan=None):
        me = state.me
        if not me or me.get("retired") or me.get("delivered"):
            return None
        if me.get("state") in P.BUSY_STATES:
            return None
        if me.get("routeEdgeId"):
            # 路线边冻结检测：目标节点有敌方有效设卡时服务端会冻住移动进度
            # （平台实测：demo 掐着我们上边的时机设卡，冻了 180 帧导致未交付）。
            # 边上主车队不能攻坚/强通（状态限制），但小分队动作不受限 -> 持续削弱。
            nxt = me.get("nextNodeId")
            if nxt and state.enemy_guard(nxt):
                # 削弱纪律（V3.12）：
                # - 卡主还站在卡节点上时不削——它 ≤3 好果就能原地补满防 6，
                #   6 人手换一次清零的交换比恒亏（replay36: r315-317 连发削光
                #   防 6，对手 r330 原地补卡，白冻到 r525）
                # - 复用 WEAKEN_RESEND_GAP：削弱落地要 3-5 帧，连发只重复扣人手
                # - 常规留 2 人手保底：第二张卡才是杀招（replay20: S10 烧光
                #   人手后 S11 再冻 180 帧到终场未交付）
                # - 平台 20579/20587 反例：第一刀后还剩 3 人，S10 防守已降
                #   到 4/5，却继续等风化 120+ 帧，直接未交付。已在关键口边上
                #   被冻死时，保底弹药应该转成救命第二刀。
                avail = self._squad_avail(state)
                last_weaken = self._weaken_sent.get(nxt, -999)
                guard = state.enemy_guard(nxt) or {}
                node_type = state.node(nxt).get("nodeType")
                rescue_weaken = (
                    last_weaken > -999
                    and node_type not in ("START", "FINISH")
                    and (guard.get("defense", 0) or 0) <= self.EDGE_WEAKEN_RESCUE_DEFENSE
                    and state.round >= self.EDGE_WEAKEN_RESCUE_ROUND
                    and avail >= self.EDGE_WEAKEN_RESCUE_AVAIL
                    and (not plan or plan.slack <= self.GUARD_SLACK_MIN)
                )
                if (state.phase != P.PHASE_RUSH
                        and (avail >= self.EDGE_WEAKEN_RESERVE or rescue_weaken)
                        and not self._opp_at_node(state, nxt)
                        and state.round - last_weaken >= self.WEAKEN_RESEND_GAP):
                    self._weaken_target = nxt  # squad_action 本帧发 SQUAD_WEAKEN
                return None
            # 移动中只能用马类资源或疾行令：没有移动增益就顺手上马（不耽误本帧推进）。
            # （V3.12 删停滞看门狗改道：8.2 移动中不能改道，该分支从未生效）
            # 马匹经济：T06 类任务要消耗整匹马，留足预留量才骑（详见 planner）
            res = me.get("resources") or {}
            if not state.has_move_buff():
                # 终局急策三选一（V3.12）：截止吃紧的追分局，速度比护果令/破关令更值钱
                # ——疾行令 15 帧内+30%速度，是唯一能在 MOVING 中提交的急策，不必等到
                # 停靠再选（任务书 8.2：MOVING 状态允许马类资源和疾行令）。
                # 成本前置：疾行令花 2 好果（6.5），交付还要求好果 >0——好果不足时
                # 提交只会被业务拒绝，而 _rush_tactic_tried 已置位不再重试，
                # 等于把整局唯一的急策名额白白锁死，所以 <3 篓不发
                if (state.phase == P.PHASE_RUSH and plan is not None and plan.slack < 0
                        and (me.get("rushTacticUsedCount") or 0) == 0
                        and not self._rush_tactic_tried
                        and me.get("goodFruit", 0) >= 3):
                    self._rush_tactic_tried = True
                    return P.a_rush_speed()
                reserve = self.planner.horses_reserved(state)
                total = res.get(P.FAST_HORSE, 0) + res.get(P.SHORT_HORSE, 0)
                if total > reserve:
                    for horse in (P.FAST_HORSE, P.SHORT_HORSE):
                        if res.get(horse, 0) > 0:
                            return P.a_use_resource(horse)
            return None  # 让系统继续推进

        cur = me.get("currentNodeId")
        gate, terminal = state.gate_node, state.terminal_node
        verified = me.get("verified")
        plan = plan or self.planner.plan(state)

        # 冰鉴不再抢在第一颗坏果前吃：坏果是关隘攻坚弹药，零坏果会把
        # 防 6 卡从"一击破"变成削弱/强通。拿到 1 个坏果后再补鲜度。
        if self._should_use_ice(state, plan):
            return P.a_use_resource(P.ICE_BOX)
        res = me.get("resources") or {}

        # 终点交付
        if cur == terminal:
            if verified and me.get("goodFruit", 0) > 0 and me.get("freshness", 0) > 0:
                return P.a_deliver()
            return P.a_wait()

        # 宫门验核（仅 RUSH）；验核前先在三选一终局急策里挑一个用（V3.12：不再
        # 硬编码护果令——坏果 >=2 时破关令绑验核近乎免费（验核 6→3 帧），
        # 优先于要烧鲜度损耗才见效的护果令；都不划算才回落护果令）
        if cur == gate and not verified and plan.kind == "deliver":
            if state.phase == P.PHASE_RUSH:
                # 情报预热（V3.16）：花 1 帧上情报，验核 6→3，净省 2 帧——
                # replay57 验核 r592 完成、差 ~8 帧未交付，这 2 帧就是胜负帧。
                # 宫门探路在实战永远发不出去（RUSH 禁派 + 标记 45 帧寿命卡死
                # 提前派的窗口），情报是唯一能给验核减帧的手段
                warm = self._intel_prewarm(state, cur, 6)
                if warm:
                    return warm
                if (me.get("rushTacticUsedCount") or 0) == 0 and not self._rush_tactic_tried:
                    self._rush_tactic_tried = True
                    bad = me.get("badFruit", 0) or 0
                    good = me.get("goodFruit", 0) or 0
                    if bad >= 2 or good > self.MIN_GOOD_RESERVE:
                        return P.a_verify_gate(break_order=True)
                    if me.get("freshness", 0) < 100:
                        return P.a_rush_protect()
                return P.a_verify_gate()
            # 等 RUSH 的长空转正是情报最值钱的地方：355 帧后标宫门，验核 6→3
            return self._idle_upgrade(state, plan)

        s10_toll_hold = self._s10_toll_hold_active(state, plan, cur)

        # 固定处理站点必须先处理完才能离站。
        # 首次仍打窗口争先手；一旦 S02 DOCK 出现 DRAW/重复抑制，就退回
        # 奇偶错峰，避免镜像对手把整局锁死在码头窗口。
        node = state.node(cur)
        needs_process = (node.get("processType") and node.get("processType") != "VERIFY"
                         and node.get("processRound", 0) > 0)
        if needs_process and not self._processed_here:
            if self._opp_processing_here(state, cur):
                return self._idle_upgrade(state, plan)  # 排队等对手处理完，顺手用情报
            if self._yield_process_after_draw(state, cur):
                return self._idle_upgrade(state, plan)
            # 情报预热（V3.16）：读条 ≥4 帧的站先花 1 帧上情报（-3 净省 2）
            warm = self._intel_prewarm(state, cur, node.get("processRound", 0))
            if warm:
                return warm
            return P.a_process()

        # 主动设卡：同路且我们先到时，先兑现设卡权再做脚下经济。
        guard = self._guard_opportunity(state, cur, plan)
        if guard:
            return guard

        # 水路领先局的 S10 终局模式：任务分已封顶时，脚下刷新任务对我方
        # 可能是 0 分，但能截掉对手最后一档任务组件；无任务就驻守等待
        # 对手进入可冻结窗口，不顺手领低价值资源打断。
        if s10_toll_hold:
            deny = self._s10_toll_denial_task(state, cur)
            if deny:
                return P.a_claim_task(deny["taskId"])
            self._s10_toll_hold_spent += 1
            return P.a_wait()

        # 任务：已在执行位置就开始读条（任务是独占对象，不让行，靠出牌博弈）。
        # V3.8 顺序修正：先抢会被偷的稀缺资源再做任务 —— replay25 我们在 S03
        # 读任务条时，落后 5 帧的对手把冰从眼皮底下领走（r92），任务不会跑、
        # 库存资源会。对手赶得上偷时，资源优先。
        if plan.kind == "task" and cur == plan.position:
            steal_risk = self._contested_claim_first(state, cur, plan)
            if steal_risk:
                return steal_risk
            return P.a_claim_task(plan.task["taskId"])

        # 资源提货目标：到位就领（V3.2 冰鉴猎手；同帧撞车交给窗口博弈）
        if plan.kind == "resource" and cur == plan.position:
            return P.a_claim_resource(cur, plan.resource)

        # S10/S11 已经是后段任务泉；低分直送前，脚下 4~6 帧任务比情报/
        # 官凭这类顺路资源更该先兑现（2026-07-05 60:150 任务分败局）。
        rescue = self._same_node_low_score_task(state, plan, cur)
        if rescue and cur in ("S10", "S11") \
                and (me.get("taskScore", 0) or 0) < 150:
            return P.a_claim_task(rescue["taskId"])

        # 顺路领取（余量闸门 15：领取只花 2 帧读条，换 +18 分几乎恒值；
        # 阴影惩罚会压低 slack，这里的闸门只挡真正的临门一脚）
        if plan.kind in ("task", "resource") or plan.slack > 15:
            stock = node.get("resourceStock") or {}
            # 竞速期（V3.18）只领交付硬件（冰=鲜度）和速度（马）：文书/
            # 情报各 2 帧读条在竞争带内是胜负帧，赢下漏斗后有的是空转帧补
            claim_list = self.RACE_CLAIM_ONLY \
                if self.planner.race_mode(state) else self.CLAIM_EN_ROUTE
            if stock.get(P.INTEL, 0) > 0 and res.get(P.INTEL, 0) <= 0 \
                    and self._should_claim_intel_en_route(state, plan, cur):
                claim_list = tuple(claim_list) + (P.INTEL,)
            if self.planner._front_tempo_active(
                    state, cur, me.get("taskScore", 0)):
                claim_list = tuple(rt for rt in claim_list
                                   if rt in self.RACE_CLAIM_ONLY)
            if plan.kind == "deliver" and (
                    state.round >= RUSH_EARLIEST
                    or self.planner.farm_rusher_pressure(state, cur)):
                claim_list = tuple(rt for rt in claim_list
                                   if rt in self.RACE_CLAIM_ONLY)
            if self.planner.race_cliff(state):
                # 悬崖带内只保留冰鉴：S07→宫门后常有 RUSH 等待窗，冰鉴
                # 的确定鲜度收益可覆盖 2 帧读条；文书/情报/马仍交给规划目标。
                claim_list = tuple(rt for rt in claim_list if rt == P.ICE_BOX)
            for rt in claim_list:
                limit = self.CLAIM_LIMIT.get(rt, 1)
                if stock.get(rt, 0) > 0 and res.get(rt, 0) < limit:
                    if rt in (P.FAST_HORSE, P.SHORT_HORSE) \
                            and not self._claim_horse_en_route_worthwhile(
                                state, plan, cur, rt):
                        continue
                    if self._yield_for_contention(state):
                        return P.a_wait()  # 错峰一帧再领，资源窗口不值得打
                    return P.a_claim_resource(cur, rt)

        rescue = self._same_node_low_score_task(state, plan, cur)
        if rescue:
            return P.a_claim_task(rescue["taskId"])

        # 尾段蹲刷（V3.10）：没有值得做的目标且余量充足时，站在任务候选点
        # 上等刷新 —— 刷出的任务下一帧就会被 plan 接住（同点零绕路必中）
        if plan.kind == "deliver" and self._should_loiter(state, plan, cur):
            return self._idle_upgrade(state, plan)

        # 悬赏目标已被清算/我们已到位（极罕见：到达同帧悬赏刚好失效），交给下一帧重新规划
        if plan.kind == "bounty" and cur == plan.position:
            return P.a_wait()

        # 赶路：任务点 / 资源点 / 悬赏目标 / 宫门 / 终点
        # 悬赏目标就是敌方设卡节点本身，寻路会在最后一跳撞见 enemy_guard() 并
        # 自动转交 _breakthrough（攻坚破卡规则要求站在相邻节点，不进入目标节点）
        target = plan.position if plan.kind in ("task", "resource", "bounty") \
            else (terminal if verified else gate)
        if target == cur:
            return P.a_wait()
        nxt = self._route_next_hop(state, cur, target)
        if nxt is None:
            return self._idle_upgrade(state, plan)
        if state.has_obstacle(nxt) and not state.enemy_guard(nxt):
            if self._should_wait_for_squad_clear(state, plan, nxt):
                return P.a_wait()
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        if state.enemy_guard(nxt):
            return self._breakthrough(state, nxt, plan)
        if self._opp_setting_guard(state, nxt):
            # 对手正在下一跳读条设卡：此时上边会在半路被冻结（边上不能攻坚），
            # 等 1~4 帧卡成型后站在节点上攻坚拆掉再走，代价小一个数量级
            return self._idle_upgrade(state, plan)
        if self._mid_edge_trap_risk(state, cur, nxt, plan):
            if self._early_road_follow_release(state, cur, nxt):
                return P.a_move(nxt)
            # 防中边陷阱：等对手离开我们的下一跳再上边。等待不是无价的——
            # 等够绕路差价后租买止损改道（V3.18），绕不开的真漏斗口继续等
            alt = self._trap_reroute(state, cur, nxt, target)
            if alt:
                return P.a_move(alt)
            idle_wait = self.IDLE_TASK_TRAP_WAIT if (
                self._opp_profile == "farmer" and not self.planner._guard_seen
            ) else 0
            return self._idle_upgrade(state, plan, min_wait=idle_wait)
        return P.a_move(nxt)

    def _early_road_follow_release(self, state, cur, nxt):
        """S02/S03 官道明牌尾随不被陷阱层改回水路。

        replay99：对手已踏上 S02->S03 后，继续把它当"下一跳埋伏"会触发
        租买改道，绕回 S04/S05 零冰水路。开局官道尾随的主要风险不是卡，
        而是被水路断冰；未见卡时直接追。
        """
        if state.phase != P.PHASE_NORMAL or self.planner._guard_seen:
            return False
        if cur == "S02" and nxt == "S03":
            pass
        elif cur == "S03" and nxt == "S07":
            pass
        else:
            return False
        if (state.me.get("taskScore", 0) or 0) >= 60:
            return False
        return self.planner._opp_committed_corridor(state) == P.ROAD

    # ---------- 主动设卡（V3）----------
    # demo 用这招连赢我们四局：在咽喉节点身后设卡，对手要么烧果攻坚、
    # 要么吃 15+5×防守值 帧的强通税、要么等风化。成本仅 4 帧读条 + 0~3 好果。

    def _guard_opportunity(self, state, cur, plan):
        me, opp = state.me, state.opp
        # V3.28 删 RUSH 自禁：任务书 6.5 冲刺后只禁"新提交小分队动作"，
        # SET_GUARD 是主车队动作不在其列（2839 复盘根因 D 的"不对称
        # 枷锁"——对面专挑 r450 落 S13 二卡，我们规则上完全可以对等
        # 奉还却自缚手脚）。交付安全由既有 slack 闸门兜底
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        node = state.node(cur)
        if cur == state.terminal_node:
            return None  # S15 禁止设卡
        g = node.get("guard")
        if g and g.get("ownerTeamId"):  # 每节点同时只有 1 个有效卡
            active = g.get("active", g.get("defense", 0) > 0)
            if active:
                return None
        reguard = self._reguard_after_break(state, cur)
        if not reguard \
                and state.round - self._guard_sent.get(cur, -999) < self.GUARD_RETRY_GAP:
            return None
        if self._my_active_guards(state) >= 2:
            return None  # 每队上限 2 个，第 3 个会顶掉最早的

        # 对手确实还要从这里过：ETA 在窗口内（含其边上剩余进度，V3.7 修复：
        # 曾把边上对手当作已到达 → ETA=0 → 主动设卡上线以来从未触发过），
        # 且该节点在其高效路线上
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_pos:
            return None
        g_graph = state.graph
        opp_eta = self.planner._opp_eta(state, cur)
        if not (self.GUARD_MIN_OPP_ETA <= opp_eta <= self.GUARD_MAX_OPP_ETA):
            return None
        # 两侧都用含边上余量的同一度量（V3.7：度量不一致曾让该检查恒假）
        opp_to_gate = self.planner._opp_eta(state, state.gate_node)
        here_to_gate, p2 = g_graph.shortest_path(cur, state.gate_node, P.BASE_SPEED)
        if not p2 or opp_to_gate == float("inf") or \
                opp_eta + here_to_gate > opp_to_gate + self.GUARD_ROUTE_TOLERANCE:
            return None  # 绕开我们这里更快，卡了也白卡

        node_type = node.get("nodeType")
        choke_guard = node_type in self.GUARD_NODE_TYPES
        catchup_guard = self._catchup_merge_guard_opportunity(state, cur, opp_eta)
        score_lead_guard = self._score_lead_merge_guard_opportunity(
            state, cur, opp_eta)
        denial_guard = self._certain_denial_guard_opportunity(
            state, cur, opp_eta)
        parting_guard = self._parting_guard_opportunity(state, cur, opp_eta)
        rear_guard = catchup_guard or score_lead_guard or denial_guard \
            or self._rear_guard_opportunity(state, cur, opp_eta)
        if not (choke_guard or rear_guard or parting_guard):
            return None
        if rear_guard and not choke_guard and \
                opp_eta + here_to_gate > opp_to_gate + self.GUARD_REAR_ROUTE_TOLERANCE:
            return None
        if self._opp_squad_rich_guard_skip(state, reguard):
            return None
        # slack 闸门分档（V3.18/V3.23）：常规 65；关键关隘热窗口 25；
        # 普通汇入点只在路径必经的反手卡场景降低门槛；RUSH 起点二卡再放宽。
        slack_min = self.GUARD_SLACK_MIN
        if choke_guard and node_type == "KEY_PASS" \
                and opp_eta <= self.GUARD_HOT_OPP_ETA:
            slack_min = self.GUARD_SLACK_HOT
        elif node_type in self.GUARD_REAR_TYPES:
            slack_min = (self.GUARD_REAR_RUSH_SLACK
                         if state.phase == P.PHASE_RUSH
                         else self.GUARD_REAR_SLACK)
        elif catchup_guard:
            slack_min = self.GUARD_CATCHUP_SLACK
        elif score_lead_guard:
            slack_min = self.GUARD_SCORE_LEAD_SLACK
        elif denial_guard:
            slack_min = self.GUARD_DENIAL_SLACK
        elif rear_guard and not choke_guard:
            slack_min = (self.GUARD_REAR_RUSH_SLACK
                         if state.phase == P.PHASE_RUSH
                         else self.GUARD_REAR_SLACK)
        if parting_guard:
            slack_min = min(slack_min, self.GUARD_PARTING_SLACK)
        if plan.slack < slack_min:
            return None

        # 成本：关键关隘/宫门底价 1 好果 + 额外好果按节点防守值上限投满不投溢
        # （防 2 的卡 30 帧就风化半残，不值底价；投不满就不投）。
        # 宫门上限 4（6.2.1）：extra=1 即 2+2=4 拉满，extra=2 超上限的那篓
        # 不提防守且成本不返还，纯白烧
        good = me.get("goodFruit", 0)
        base_cost = 1 if node.get("nodeType") in ("KEY_PASS", "GATE") else 0
        extra = 1 if node.get("nodeType") == "GATE" else 2
        if good - base_cost - extra <= self.MIN_GOOD_RESERVE:
            return None  # 好果太紧，不做对抗投资
        min_arrival_def = (self.GUARD_MIN_ARRIVAL_DEFENSE if choke_guard
                           else self.GUARD_REAR_MIN_ARRIVAL_DEFENSE)
        if (not reguard
                and self._guard_defense_at_arrival(state, cur, extra, opp_eta)
                < min_arrival_def):
            return None
        if (not catchup_guard
                and not parting_guard
                and self._guard_bounty_exposure(state, cur, opp_eta, extra)):
            return None
        self._guard_sent[cur] = state.round
        if self.log:
            self.log.info("set%s guard @%s extra=%d (opp eta=%d)",
                          " follow-up" if reguard else "", cur, extra, opp_eta)
        return P.a_set_guard(cur, extra)

    def _opp_squad_rich_guard_skip(self, state, reguard=False):
        """零卡 farmer/跟随者还攒着小分队时，防 6 卡会被远程削弱套利。

        replay99：我们 9 好果三张卡只换来对手 6 人手+少量时间税；对方
        全局零设卡、任务已农高、squadAvailable 充足，这时好果比卡更值钱。
        """
        if self._opp_profile != "farmer" or self.planner._guard_seen:
            return False
        opp = state.opp or {}
        squads = opp.get("squadAvailable")
        if squads is None:
            return False
        threshold = (self.GUARD_REGUARD_SQUAD_SKIP if reguard
                     else self.GUARD_SQUAD_RICH_SKIP)
        return squads >= threshold

    def _track_own_guard_breaks(self, state):
        """记录我方刚被打穿的卡点，给同点补卡一个短窗口。

        防重试间隔挡的是同一帧/同一读条失败后的动作风暴；但 vs2931 里
        S10 卡已完整生效并逼出对手 3 次削弱，卡被打掉后我们仍站在 S10。
        这时补第二张卡是战术兑现，不是重试风暴。
        """
        current = {}
        for node_id, node in state.nodes.items():
            g = node.get("guard") or {}
            if g.get("ownerTeamId") == state.my_team:
                defense = g.get("defense", 0) or 0
                active = bool(g.get("active", defense > 0)) and defense > 0
                current[node_id] = (active, defense)
        for node_id, (was_active, _) in list(self._own_guard_seen.items()):
            now = current.get(node_id)
            if was_active and (not now or not now[0]):
                self._own_guard_broken[node_id] = state.round
        self._own_guard_seen = current
        for node_id, round_no in list(self._own_guard_broken.items()):
            if state.round - round_no > self.GUARD_REGUARD_WINDOW:
                del self._own_guard_broken[node_id]

    def _reguard_after_break(self, state, cur):
        """同点补卡：上一张我方卡刚被打穿、对手仍未通过时绕过重试间隔。"""
        last = self._own_guard_broken.get(cur)
        if last is None or state.round - last > self.GUARD_REGUARD_WINDOW:
            return False
        if state.node(cur).get("nodeType") not in self.GUARD_NODE_TYPES:
            return False
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        opp_task = opp.get("taskScore", 0) or 0
        opp_squads = opp.get("squadAvailable")
        freeze_option = opp_squads is not None and opp_squads <= 1
        if opp_task >= self.S10_TOLL_DENY_BASE and not freeze_option:
            return False
        if opp.get("currentNodeId") == cur and not opp.get("routeEdgeId"):
            return False
        return True

    def _guard_bounty_exposure(self, state, node_id, opp_eta, extra):
        """领先局不把一击可破、会挂悬赏的卡送给对手。

        对手已在目标边上时例外：它不能在 MOVING 状态攻坚，设卡收益来自
        中边冻结/削弱战，不是等待它到相邻节点后拿悬赏。
        """
        if self._opp_profile != "farmer":
            return False
        me, opp = state.me, state.opp
        if (opp.get("totalScore", 0) or 0) >= (me.get("totalScore", 0) or 0):
            return False
        if opp_eta < self.GUARD_BOUNTY_EXPOSE_ETA:
            return False
        if opp.get("routeEdgeId") and opp.get("nextNodeId") == node_id:
            return False
        good = opp.get("goodFruit", 0) or 0
        bad = opp.get("badFruit", 0) or 0
        attack = min(2, good) * 2 + min(2, bad) * 3
        return attack >= self._guard_defense_after_set(state, node_id, extra)

    def _guard_defense_after_set(self, state, node_id, extra):
        node = state.node(node_id)
        node_type = node.get("nodeType")
        if node_type == "GATE":
            cap = 4
        elif state.has_obstacle(node_id):
            cap = 5
        elif node_type == "KEY_PASS":
            cap = 7
        else:
            cap = 6
        return min(cap, 2 + extra * 2)

    def _guard_defense_at_arrival(self, state, node_id, extra, opp_eta):
        defense = self._guard_defense_after_set(state, node_id, extra)
        if opp_eta <= 0:
            return defense
        node_type = state.node(node_id).get("nodeType")
        first = FUNNEL_FIRST_WEATHER if (
            node_type == "KEY_PASS" and defense >= 4) else FUNNEL_WEATHER_GAP
        if opp_eta < first:
            return defense
        ticks = 1 + int((opp_eta - first) // FUNNEL_WEATHER_GAP)
        return max(0, defense - ticks)

    def _rear_guard_opportunity(self, state, cur, opp_eta):
        """普通汇入点/RUSH 起点反手卡：路径必经且我们来得及读条。"""
        node_type = state.node(cur).get("nodeType")
        if node_type in self.GUARD_NODE_TYPES or node_type in ("START", "FINISH"):
            return False
        if opp_eta > self.GUARD_REAR_OPP_ETA:
            return False
        if node_type in self.GUARD_REAR_TYPES:
            return True
        return (self._opp_ordinary_guard_seen
                or self.planner.farm_rusher_pressure(state, cur))

    def _parting_guard_opportunity(self, state, cur, opp_eta):
        """离站临别卡：对手已经在来当前节点的边上，离开前先把门焊上。

        replay 2026-07-04T235919：对手在 S09 读完站务后立卡再走，
        我方已在 S07->S09 长边上，最终被 115 帧卡税拖到未交付。这个门
        只看公开几何：对手 nextNode 是当前点、我们 4 帧内来得及成卡。
        """
        opp = state.opp or {}
        if not opp.get("routeEdgeId") or opp.get("nextNodeId") != cur:
            return False
        if state.node(cur).get("nodeType") in ("START", "FINISH"):
            return False
        if cur == "S10" and (state.me.get("taskScore", 0) or 0) >= self.S10_TOLL_BASE:
            return opp_eta > self.TRAP_GUARD_FRAMES
        if (state.me.get("taskScore", 0) or 0) < 120 or state.round < 220:
            return False
        return opp_eta > self.TRAP_GUARD_FRAMES

    def _catchup_merge_guard_opportunity(self, state, cur, opp_eta):
        """任务分明显落后时，把普通合流点抢先到位转化为对手通行税。"""
        node_type = state.node(cur).get("nodeType")
        if node_type in self.GUARD_NODE_TYPES or node_type in ("START", "FINISH"):
            return False
        if opp_eta > self.GUARD_CATCHUP_OPP_ETA:
            return False
        me_task = state.me.get("taskScore", 0) or 0
        opp_task = state.opp.get("taskScore", 0) or 0
        if me_task > self.GUARD_CATCHUP_MY_TASK_MAX:
            return False
        if opp_task - me_task < self.GUARD_CATCHUP_TASK_GAP:
            return False
        return self.planner._key_pass_ahead(state, cur)

    def _score_lead_merge_guard_opportunity(self, state, cur, opp_eta):
        """中盘高分对手将至时，普通合流点也应兑现先到设卡权。"""
        node_type = state.node(cur).get("nodeType")
        if node_type in self.GUARD_NODE_TYPES or node_type in ("START", "FINISH"):
            return False
        if opp_eta > self.GUARD_SCORE_LEAD_OPP_ETA:
            return False
        me_task = state.me.get("taskScore", 0) or 0
        opp_task = state.opp.get("taskScore", 0) or 0
        if opp_task < self.GUARD_SCORE_LEAD_TASK_MIN:
            return False
        if opp_task - me_task < self.GUARD_SCORE_LEAD_TASK_GAP:
            return False
        return self.planner._key_pass_ahead(state, cur)

    def _certain_denial_guard_opportunity(self, state, cur, opp_eta):
        """普通必经点的轻量拒止卡：小幅落后也不白放对手过合流口。"""
        node_type = state.node(cur).get("nodeType")
        if node_type in self.GUARD_NODE_TYPES or node_type in ("START", "FINISH"):
            return False
        if opp_eta > self.GUARD_DENIAL_OPP_ETA:
            return False
        me_task = state.me.get("taskScore", 0) or 0
        opp_task = state.opp.get("taskScore", 0) or 0
        if opp_task < self.GUARD_DENIAL_TASK_MIN:
            return False
        if opp_task - me_task < self.GUARD_DENIAL_TASK_GAP:
            return False
        return self.planner._key_pass_ahead(state, cur)

    def _my_active_guards(self, state):
        n = 0
        for node in state.nodes.values():
            g = node.get("guard")
            if g and g.get("ownerTeamId") == state.my_team \
                    and g.get("active", g.get("defense", 0) > 0):
                n += 1
        return n

    def _should_loiter(self, state, plan, cur):
        """尾段蹲刷判定：预算内、余量足、里程碑未满、身处任务候选点。

        跟随者战术（V3.10.1 修正）：仅当对手在前方（或已交付）才蹲——
        领先时蹲刷等于把走廊节奏还给对手，对设卡型对手（2614）是自杀。
        """
        if state.phase != P.PHASE_NORMAL or state.me.get("verified"):
            return False
        if self.planner.race_mode(state):
            return False  # 竞速窗口先抢走廊：刷新等赢下漏斗再吃（V3.18）
        if plan.slack < self.LOITER_MIN_SLACK:
            return False
        opp = state.opp
        if opp and not opp.get("delivered") and not opp.get("retired"):
            my_eta = state.graph.all_frames(cur).get(state.gate_node, 0)
            opp_eta = self.planner._opp_eta(state, state.gate_node)
            if my_eta < opp_eta:   # 我们领先：保节奏，不蹲
                return False
        if (state.me.get("taskScore", 0) or 0) >= self.LOITER_BASE_CAP:
            return False
        if self._loiter_spent >= self.LOITER_BUDGET:
            return False
        if cur in (state.gate_node, state.terminal_node):
            return False
        # 只蹲在会刷任务的节点上（地图配置的候选点并集；配置缺失则不蹲）
        candidates = set()
        for nodes in (state.task_candidates or {}).values():
            candidates.update(nodes)
        if cur not in candidates:
            return False
        self._loiter_spent += 1
        if self.log and self._loiter_spent % 10 == 1:
            self.log.info("loiter for task refresh @%s (%d/%d)",
                          cur, self._loiter_spent, self.LOITER_BUDGET)
        return True

    def _s10_toll_hold_active(self, state, plan, cur):
        """S10 收租驻守：只在 replay99 型水路领先终局打开。

        这不是泛化蹲刷：必须已经拿到 120+ 任务分、对手未交付且高效路仍
        经过 S10。收益来自截刷新任务和等待对手踏边后的临别/身体设卡冻结。
        """
        if cur != "S10" or not plan or plan.kind != "deliver":
            return False
        if state.me.get("verified") or state.me.get("taskScore", 0) < self.S10_TOLL_BASE:
            return False
        if plan.slack < self.S10_TOLL_MIN_SLACK:
            return False
        if self._s10_toll_hold_spent >= self.S10_TOLL_BUDGET:
            return False
        opp = state.opp or {}
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        opp_task = opp.get("taskScore", 0) or 0
        opp_squads = opp.get("squadAvailable")
        freeze_option = opp_squads is not None and opp_squads <= 1
        if opp_task >= self.S10_TOLL_DENY_BASE and not freeze_option:
            return False
        if opp.get("currentNodeId") == cur and not opp.get("routeEdgeId"):
            return False
        opp_eta = self.planner._opp_eta(state, cur)
        if not (0 < opp_eta <= self.S10_TOLL_MAX_OPP_ETA):
            return False
        opp_to_gate = self.planner._opp_eta(state, state.gate_node)
        here_to_gate, path = state.graph.shortest_path(cur, state.gate_node,
                                                       P.BASE_SPEED)
        if not path or opp_to_gate == float("inf"):
            return False
        return opp_eta + here_to_gate <= opp_to_gate + self.S10_TOLL_ROUTE_TOLERANCE

    def _s10_toll_denial_task(self, state, cur):
        """驻守 S10 时，截掉对手仍有边际的脚下刷新任务。"""
        if cur != "S10":
            return None
        opp = state.opp or {}
        if (opp.get("taskScore", 0) or 0) >= self.S10_TOLL_DENY_BASE:
            return None
        opp_eta = self.planner._opp_eta(state, cur)
        best = None
        best_key = None
        for t in state.claimable_tasks():
            if t.get("nodeId") != cur or (t.get("score", 0) or 0) <= 0:
                continue
            proc = t.get("processRound", 4) or 4
            if proc > self.S10_TOLL_TASK_MAX_PROC:
                continue
            if opp_eta <= proc:
                continue
            key = (t.get("score", 0) or 0, -proc)
            if best is None or key > best_key:
                best = t
                best_key = key
        return best

    def _same_node_low_score_task(self, state, plan, cur):
        """直送兜底：先吃脚下短任务，避免低任务分早交付。"""
        if plan.kind != "deliver" or state.phase != P.PHASE_NORMAL:
            return None
        if state.me.get("verified") or cur not in ("S09", "S10", "S11"):
            return None
        base = state.me.get("taskScore", 0) or 0
        milestone_rescue = 90 <= base < 120
        s10_score_rescue = cur in ("S10", "S11") and base < 150
        if not (milestone_rescue or s10_score_rescue):
            return None
        best = None
        for t in state.claimable_tasks():
            if t.get("nodeId") != cur or (t.get("score", 0) or 0) <= 0:
                continue
            if t.get("taskTemplateId") in ("T04", "T06"):
                continue
            proc = t.get("processRound", 4) or 4
            if proc > 6 or proc > plan.slack:
                continue
            expire = t.get("expireRound") or 0
            if expire and state.round + proc > expire:
                continue
            if self.planner._opp_processing_task(state, t):
                continue
            best_key = (best.get("score", 0), -(best.get("processRound", 4) or 4)) \
                if best else None
            if best is None or (t.get("score", 0), -proc) > best_key:
                best = t
        return best

    def _contested_claim_first(self, state, cur, plan):
        """任务前的稀缺资源保卫：对手赶得上在我们读条期间偷走时，先领。"""
        me = state.me
        res = me.get("resources") or {}
        stock = state.node(cur).get("resourceStock") or {}
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        task_frames = (plan.task or {}).get("processRound", 4) or 4
        window = task_frames + 4   # 任务读条 + 领取读条余量
        if self.planner._opp_eta(state, cur) > window:
            return None
        # 只为高价值资源打断任务顺序（冰鉴 17 分；马预留另有机制）
        if stock.get(P.ICE_BOX, 0) > 0 and res.get(P.ICE_BOX, 0) < 2:
            return P.a_claim_resource(cur, P.ICE_BOX)
        return None

    def _claim_horse_en_route_worthwhile(self, state, plan, cur, horse):
        """直送阶段顺手拿马前，确认剩余路程至少能省回领取读条。"""
        if not plan or plan.kind != "deliver":
            return True
        target = state.terminal_node if state.me.get("verified") else state.gate_node
        base_frames, base_path = state.graph.shortest_path(cur, target, P.BASE_SPEED)
        if not base_path:
            return False
        speed = P.SPEED_FAST_HORSE if horse == P.FAST_HORSE else P.SPEED_SHORT_HORSE
        horse_frames, horse_path = state.graph.shortest_path(cur, target, speed)
        if not horse_path:
            return False
        return base_frames - horse_frames >= self.HORSE_CLAIM_MIN_SAVE

    # ---------- 情报：空转帧顺手用（V3.12）----------
    # 注定 WAIT 的帧（排队/防陷阱/蹲刷等）不占主车队移动时间，此时若手里有情报，
    # 顺手标一个目标节点：效果与小分队探路相同（处理帧 -3，最低 2），但完全不占
    # 人手，机会成本≈0——专程为它停下不划算，只在反正要空等的帧里用。

    def _idle_upgrade(self, state, plan, min_wait=0):
        me = state.me
        task = self._idle_task_upgrade(state, plan, min_wait)
        if task:
            return P.a_claim_task(task["taskId"])
        if (me.get("resources") or {}).get(P.INTEL, 0) <= 0:
            return P.a_wait()
        cur = me.get("currentNodeId")
        if not cur:
            return P.a_wait()  # 移动/边上不能用情报（任务书 3.3.4），只在停靠时机会成立

        candidates = []
        if plan and plan.kind == "task" and plan.position:
            candidates.append(plan.position)
        # 宫门时机门与小分队探路同款（GATE_SCOUT_FROM）：标记只活 45 帧，
        # 验核最早 ~390 帧开放，355 帧前标宫门必然过期白扔
        if not me.get("verified") and state.round >= self.GATE_SCOUT_FROM:
            candidates.append(state.gate_node)
        node = state.node(cur)
        # 正在等的原因如果是本站还没处理完，标自己这站对下一次处理直接有用；
        # 否则不标 cur——它不需要处理时，情报只是被白白用掉
        if (node.get("processType") and node.get("processType") != "VERIFY"
                and node.get("processRound", 0) > 0 and not self._processed_here):
            candidates.append(cur)

        for target in candidates:
            if not target:
                continue
            if self.planner._has_our_scout_mark(state, target):
                continue
            if state.graph.shortest_distance(cur, target) > self.INTEL_DISTANCE_LIMIT:
                continue
            return P.a_use_resource(P.INTEL, target)
        return P.a_wait()

    def _idle_task_upgrade(self, state, plan, min_wait=0):
        """本来要空等时，顺手吃脚下短任务；只给显式长等待场景调用。"""
        if min_wait <= 0 or state.phase != P.PHASE_NORMAL:
            return None
        me = state.me
        cur = me.get("currentNodeId")
        if not cur or me.get("verified"):
            return None
        if plan and plan.slack < self.IDLE_TASK_MAX_PROC + 10:
            return None
        opp = state.opp or {}
        if opp and not opp.get("routeEdgeId") and opp.get("currentNodeId") == cur:
            return None
        base = me.get("taskScore", 0) or 0
        best = None
        best_key = None
        for t in state.claimable_tasks():
            if t.get("nodeId") != cur:
                continue
            if t.get("taskTemplateId") in ("T04", "T06"):
                continue
            proc = t.get("processRound", 4) or 4
            if proc > min(min_wait, self.IDLE_TASK_MAX_PROC):
                continue
            expire = t.get("expireRound") or 0
            if expire and state.round + proc > expire:
                continue
            if self.planner._opp_processing_task(state, t):
                continue
            value = marginal_task_value(base, t.get("score", 0) or 0)
            if value <= 0:
                continue
            key = (value, t.get("score", 0) or 0, -proc)
            if best is None or key > best_key:
                best, best_key = t, key
        return best

    # ---------- 突破敌方设卡 ----------
    # 平台败局教训：在 S09 干等 S10 敌卡风化 175 帧直接导致未交付（80:525）。
    # 优先级：攻坚(坏果优先,瞬发) > 小分队削弱后攻坚 > 强制通行(时间税<=50帧) > 等。
    # 蹲点例外（V3.14，V3.16 免试探）：卡主停靠在卡节点上且补得起卡（关键关隘/
    # 宫门底价 1 好果，普通节点免费）时，攻坚/削弱都是喂饵——语料 5/5 局
    # （36/56/57/58/59）拆掉即被原地补满，试探从未成功过，每次白送 2 好果 +
    # 1 坏果 + ~12 帧。直接强制通行：时间税在窗口创建时一次锁定、之后补卡
    # 不计入、通行不可冻结（任务书 6.3.2），是对蹲点补卡唯一有界的解。
    # 它补不起卡（好果见底）时才放心攻坚。
    # 临别卡宽限（V3.17）：语料 6/6 次"卡主在场"其实是它刚读完临别卡还没
    # 迈步（2614: r314卡→r318走 / r322卡→r323走；2839: r309卡→r310走 /
    # r450卡→r451走）——我们的强通总提交在它离开前的最后一帧，完美错过
    # 次帧就能用的节点攻坚（reports 局 S10：本可 2好果+1坏果 秒拆，实付
    # 117 帧强通）。新卡+卡主在场 → 先等 CAMPER_GRACE 帧看它走不走：
    # 走了节点攻坚白菜价；赖着不走才是真蹲点，再走强通（多花 ≤8 帧）。

    # V3.19：8 → 5。语料里临别卡对手全部在起卡后 1~4 帧离开（2614:
    # r314卡→r318走 / r322卡→r323走；2839: r309→r310 / r450→r451），
    # 5 = 观测上界 + 1 帧余量；竞技场 camper 局实测 8→5 把 3/24 的未交付
    # 清零（3 帧提前量级联：早出武关 → 赶在对手到潼关起卡前上边，整段
    # 45 帧汇聚等待消失），镜像局该参数不绑定（±30% margin 恰 0）无回归
    CAMPER_GRACE = 5
    CAMPER_LEAVE_PROBE = 3
    CAMPER_LEAVE_REMAIN = 2
    # 驻扎判定（V3.19）：宽限的依据是"临别卡 = 刚到就起卡、次帧就走"。
    # 起卡前已在该节点驻扎 ≥ 此帧数的对手是坐地户不是过客（竞技场 camper
    # 局实测：3/24 局死于终盘差 ~20 帧，白给的 8 帧宽限是其中一截），
    # 对它宽限只是给它多农 8 帧任务
    CAMPER_ESTABLISHED = 20      # 驻留口径含做任务帧（V3.22 复核确认）：
                                 # 2839 复盘曾疑此口径误伤"农 8 帧即走的
                                 # 过客"——实测 8 < 20 本就在宽限保护内，
                                 # V3.19 语义已正确。曾试"闲置驻留"双口径
                                 # （做任务帧重置），给农 25 帧的真蹲点
                                 # （CamperBot delay 变体）多送 5 帧宽限，
                                 # 与其动身帧共振拖死 camper seed0/5——
                                 # 语料里并无"长农过客"形态，按反过拟合
                                 # 纪律回退，只留临别卡回归钉子

    def _breakthrough(self, state, target, plan):
        me = state.me
        cur = me.get("currentNodeId")
        # 6.3.2 重复限制绑定在【发起节点】：停在上次强通到达节点时禁发（见
        # _last_forced_node 注释）。target 是否被强通过不构成限制
        forced_ok = cur != self._last_forced_node
        g = state.enemy_guard(target)
        defense = g.get("defense", 0) or 0
        invest = self._break_invest(defense, me.get("goodFruit", 0),
                                    me.get("badFruit", 0))

        if self._opp_at_node(state, target):
            base = 1 if state.node(target).get("nodeType") in ("KEY_PASS", "GATE") else 0
            can_reguard = (state.opp.get("goodFruit", 0) or 0) >= max(1, base) \
                if base else True
            if can_reguard:
                first = self._guard_first_seen.setdefault(target, state.round)
                # 起卡前就驻扎已久的对手不给宽限：它不是读完临别卡要走的
                # 过客，是坐地户——每帧宽限都是送它农任务
                stay_node, stay_since = self._opp_stationary
                established = (stay_node == target and
                               first - stay_since >= self.CAMPER_ESTABLISHED)
                if not established and state.round - first < self.CAMPER_GRACE:
                    return self._idle_upgrade(state, plan)  # 宽限：等它迈步
                node_type = state.node(target).get("nodeType")
                if (not established and invest
                        and self._opp_likely_leaving_guard_node(state, target)):
                    start = self._guard_leave_probe.setdefault(target, state.round)
                    if state.round - start < self.CAMPER_LEAVE_PROBE:
                        return self._idle_upgrade(state, plan)
                if not established and invest and node_type not in ("KEY_PASS", "GATE"):
                    gf, bf = invest
                    if self.log:
                        self.log.info("break transient guard %s def=%d with good=%d bad=%d",
                                      target, defense, gf, bf)
                    return P.a_break_guard(target, gf, bf)
                if forced_ok:
                    if self.log:
                        self.log.info("camper holds %s past grace, forced pass",
                                      target)
                    return P.a_forced_pass(target)
                return P.a_wait()

        # 1) 一击必破：攻坚值 = 好果x2 + 坏果x3，投入各最多 2 篓，无读条
        if invest:
            gf, bf = invest
            if self.log:
                self.log.info("break guard %s def=%d with good=%d bad=%d",
                              target, defense, gf, bf)
            return P.a_break_guard(target, gf, bf)

        # 2) 果品不够破：削弱 vs 强通按真实耗时选快的（V3.8：不再用 slack
        #    闸门 —— replay25 在 r325 因 slack<0 跳过削弱选了强通，吃了
        #    100 帧税+路程，实际比削弱路径慢 20+ 帧且截止越紧越输不起）。
        #    V3.17 削到能拆即止：坏果饥荒（鲜度管理好 → 全场坏果 0~1）下
        #    好果攻坚上限只有 4，防 6 的卡按"削到 0"算要 3 次派遣 6 人手，
        #    reports 局人手剩 5 被拒转强通白吃 70 帧——其实削 1 次到防 4
        #    就能 2 好果秒拆，2 人手足够
        max_attack = min(2, max(0, me.get("goodFruit", 0) - self.MIN_GOOD_RESERVE)) * 2 \
            + min(2, me.get("badFruit", 0) or 0) * 3
        dispatches = max(1, -(-(defense - max_attack) // 2))  # 削到可拆的次数
        weaken_time = (dispatches - 1) * self.WEAKEN_RESEND_GAP + 8  # 落地延迟
        node_type = state.node(target).get("nodeType")
        if node_type == "KEY_PASS":
            forced_tax = min(50, 15 + defense * 5)
        elif node_type == "GATE":
            forced_tax = min(32, 12 + defense * 5)
        else:
            forced_tax = min(40, 10 + defense * 5)
        # 卡主在场不削（与中边冻结分支同一纪律）：削弱落地 3~8 帧，
        # 它站在节点上随手就把防守值补回来，人手换零钱。
        # RUSH 期自禁复核确认（V3.22）：冲刺阶段新派小分队是服务端违规
        # （SQUAD_NOT_ALLOWED，arena:1131 按回放校准）——曾试解禁，
        # can_weaken 通过但 squad 层拒发，主车队卡在 WAIT 死循环。
        # RUSH 期坏果枯竭的场景走下面"等到可拆"分支（规则内真解）
        can_weaken = state.phase != P.PHASE_RUSH \
            and self._squad_avail(state) >= dispatches * 2 \
            and not self._opp_at_node(state, target)
        # 悬赏本该是"强通不清卡拿不到悬赏分，该多容忍削弱几帧"的理由，但穷举
        # 现有防守值上限（4/5/6/7）发现削弱耗时按 WEAKEN_RESEND_GAP=12 帧计，
        # 在 can_weaken 成立的每种真实防守值下都已经跑赢强通税+10 的门槛——
        # 加宽容忍度不会改变任何一次决策，是不会触发的死代码，因此不加这个闸门
        # （人手不够时 can_weaken 本身就是 False，容忍度调宽也救不了）。
        if can_weaken and weaken_time <= forced_tax + 10:
            if state.round - self._weaken_sent.get(target, -999) >= self.WEAKEN_RESEND_GAP:
                self._weaken_target = target  # squad_action 本帧发 SQUAD_WEAKEN
            return P.a_wait()

        # 2.5) 等到可拆（V3.22，2839 复盘根因 D 的规则内真解）：RUSH 期
        #     小分队违规、坏果枯竭时攻坚上限 4 打不动防 5+ 的卡，但风化
        #     时刻表公开确定（completeRound + 首风化 45/30，之后每 30 帧
        #     -1）。防守降到攻坚上限内的等待若比强通税便宜就等——真实
        #     S13 案例：r507 强通付 36 帧税，r510 卡就风化到防 4 可拆
        wait_b = self._frames_until_breakable(state, target, max_attack)
        if wait_b is not None and 0 < wait_b < forced_tax - 2:
            if self.log:
                self.log.info("wait %d frames for %s to weather breakable",
                              wait_b, target)
            return self._idle_upgrade(state, plan)

        # 3) 强制通行兜底：关键关隘时间税最多 50 帧，仍远好于等风化到 0
        if forced_ok:
            return P.a_forced_pass(target)
        return P.a_wait()

    def _frames_until_breakable(self, state, node_id, ceiling):
        """按公开风化时刻表算防守降到 ceiling 内还要几帧；字段缺失返回 None。

        风化规则（6.2.1，与漏斗模型同源）：满防 KEY_PASS 首风化 45 帧、
        其余 30 帧，此后每 30 帧 -1。guard 的 completeRound/initialDefense/
        defense 都是公开字段（通信协议 nodes[].guard）。"""
        g = state.enemy_guard(node_id)
        if not g:
            return None
        complete = g.get("completeRound")
        init = g.get("initialDefense")
        defense = g.get("defense", 0) or 0
        if complete is None or init is None or defense <= 0:
            return None
        if ceiling <= 0:
            return None
        ticks_needed = defense - ceiling
        if ticks_needed <= 0:
            return 0
        is_key = state.node(node_id).get("nodeType") == "KEY_PASS"
        first = FUNNEL_FIRST_WEATHER if (is_key and init >= 4) \
            else FUNNEL_WEATHER_GAP
        ticks_done = init - defense
        # 第 k 次风化发生在 complete + first + (k-1)*30
        k = ticks_done + ticks_needed
        when = complete + first + (k - 1) * FUNNEL_WEATHER_GAP
        return max(0, when - state.round)

    def _break_invest(self, defense, good, bad):
        """选攻坚投入 (好果, 坏果)：坏果免费优先，好果留底仓；破不动返回 None。"""
        if defense <= 0:
            return None
        best = None  # (成本, 好果, 坏果)
        max_gf = min(2, max(0, good - self.MIN_GOOD_RESERVE))
        for bf in range(min(2, bad) + 1):
            for gf in range(max_gf + 1):
                if gf + bf > 0 and gf * 2 + bf * 3 >= defense:
                    cost = gf * 1.9 + bf * 0.1  # 好果值 ~1.9 分，坏果近乎免费
                    if best is None or cost < best[0]:
                        best = (cost, gf, bf)
        return (best[1], best[2]) if best else None

    def _opp_likely_leaving_guard_node(self, state, node_id):
        opp = state.opp or {}
        if opp.get("nextNodeId") and opp.get("nextNodeId") != node_id:
            return True
        proc = opp.get("currentProcess") or {}
        if not proc:
            return False
        if opp.get("state") != P.ST_PROCESSING:
            return False
        action = proc.get("action") or proc.get("type")
        if action in ("SET_GUARD", "BREAK_GUARD", "SQUAD_REINFORCE"):
            return False
        remain = self._process_remain_round(proc)
        if remain is None or remain > self.CAMPER_LEAVE_REMAIN:
            return False
        target = proc.get("targetNodeId") or proc.get("nodeId")
        return bool(proc.get("taskId") or (target and target != node_id))

    @staticmethod
    def _process_remain_round(proc):
        for key in ("remainRound", "remainingRound", "remain", "remaining"):
            val = proc.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return None
        return None

    def _route_next_hop(self, state, cur, target):
        """与规划器共用同一套惩罚 + 天气边成本，保证走的路就是估值时算的路。

        租买改道承诺期间（V3.18）被避节点追加惩罚，保证后续帧继续走
        替代走廊而不是下一帧又拐回去重新开始等待。"""
        penalty = self.planner._penalty_fn(state)
        avoid, until = self._trap_avoid
        if avoid and state.round < until:
            base_pen = penalty

            def penalty(nid, _base=base_pen, _avoid=avoid):
                return _base(nid) + \
                    (self.TRAP_AVOID_PENALTY if nid == _avoid else 0)
        nxt = state.graph.next_hop(cur, target, state.my_speed(), penalty,
                                   self.planner._edge_cost_fn(state))
        if nxt in ("S04", "S05") and target not in ("S04", "S05"):
            base = state.me.get("taskScore", 0) or 0
            if self.planner._front_tempo_early_water_fork_blocked(
                    state, cur, nxt, base, P.WATER):
                road_next = "S03" if cur == "S02" else "S07"
                if state.graph.edge_between(cur, road_next):
                    return road_next
        return nxt

    # ---------- 同帧争抢规避 ----------

    @staticmethod
    def _yield_for_contention(state):
        """对手与我们同节点且空闲时，按 (帧号+playerId) 奇偶让行 1 帧。

        双方奇偶必然岔开：一方先开始读条后，对象被占用，另一方后续提交
        只会吃到无害的 OBJECT_BUSY 业务拒绝并排队 —— 窗口从一开始就不会创建。
        对镜像/同水平对手可根治固定处理站的平局死锁（成本：偶尔多等 1 帧）。
        """
        me, opp = state.me, state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        if opp.get("routeEdgeId") or opp.get("currentNodeId") != me.get("currentNodeId"):
            return False
        if opp.get("state") not in (P.ST_IDLE, P.ST_WAITING):
            return False
        return (state.round + state.player_id) % 2 == 1

    def _yield_process_after_draw(self, state, cur):
        if cur != "S02":
            return False
        key = (cur, P.CONTEST_DOCK)
        suppress_until = self._window_suppress_until.get(key, -1)
        if state.round <= suppress_until:
            return True
        count, last_round = self._window_draw_pressure.get(key, (0, -999))
        if count < 1 or state.round - last_round > self.WINDOW_DRAW_PRESSURE_DECAY:
            return False
        return self._yield_for_contention(state)

    @staticmethod
    def _opp_farming_here(state, node_id):
        """对手停靠在该节点且正在读任务条（farmer 有界等待的第三重门）。"""
        opp = state.opp
        if not opp or opp.get("routeEdgeId") \
                or opp.get("currentNodeId") != node_id:
            return False
        return bool((opp.get("currentProcess") or {}).get("taskId"))

    @staticmethod
    def _opp_at_node(state, node_id):
        """对手主车队正停靠在该节点上（能以 ≤3 好果原地补卡，削弱=喂饵）。"""
        opp = state.opp
        return bool(opp and not opp.get("delivered") and not opp.get("retired")
                    and not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == node_id
                    and not PlannerStrategy._opp_forced_passing_from(state, node_id))

    @staticmethod
    def _opp_forced_passing_from(state, node_id):
        """对手正在从 node_id 强通离开：currentNodeId 仍显示 node_id，但已不能设卡。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return False
        proc = opp.get("currentProcess") or {}
        action = proc.get("action") or proc.get("type")
        target = proc.get("targetNodeId")
        return bool(not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == node_id
                    and (opp.get("state") == P.ST_FORCED_PASSING
                         or action == "FORCED_PASS")
                    and target and target != node_id)

    def _intel_prewarm(self, state, target, proc_frames):
        """处理/验核前先上情报（读条 -3，最低 2）：读条 ≥4 帧净省 ≥1。

        目标就是脚下节点（距离 0，满足 3.3.4 的 ≤15 限制）；已有本队
        标记时不重复。"""
        res = state.me.get("resources") or {}
        if res.get(P.INTEL, 0) <= 0 or (proc_frames or 0) < 4:
            return None
        if self.planner._has_our_scout_mark(state, target):
            return None
        return P.a_use_resource(P.INTEL, target)

    def _should_claim_intel_en_route(self, state, plan, cur):
        """情报主动领取只保留给 S03 开局打包、后段走廊与 camper 慢局。

        情报 2 帧领取 + 1 帧使用最多省 3 帧，在前段竞速/悬崖/直送阶段
        等价于拿节奏换零收益；但 S03 开局已停车打包、camper seed5 这类
        极限收盘局，需要这一帧级减读条把 r600 交付救回来。S10/S11/S13
        这类后段走廊情报还兼具拒止价值：我们不拿，对手会拿去给自己
        入关/宫前读条减帧。"""
        if state.phase != P.PHASE_NORMAL:
            return False
        if self.planner.race_mode(state) or self.planner.race_cliff(state):
            return False
        if self.planner._front_tempo_active(
                state, cur, (state.me or {}).get("taskScore", 0)):
            return False
        if plan.kind == "deliver" and (
                state.round >= RUSH_EARLIEST
                or self.planner.farm_rusher_pressure(state, cur)):
            return False
        if cur == "S03" and state.round <= 130 and plan.slack >= 50:
            return True
        node_type = (state.node(cur) or {}).get("nodeType")
        if node_type in ("KEY_PASS", "PASS", "PALACE_STATION") \
                and self.planner._map_progress(state, cur) >= 0.55 \
                and plan.slack >= 20:
            return True
        return self._opp_profile == "camper"

    @staticmethod
    def _opp_processing_here(state, node_id):
        """对手是否正在处理我们所在站点的流程（此时提交只会被拒，等它完成）。"""
        proc = state.opp.get("currentProcess") or {}
        return proc.get("targetNodeId") == node_id and \
            (proc.get("action") or proc.get("type")) in ("PROCESS", "DOCK")

    @staticmethod
    def _opp_setting_guard(state, node_id):
        """对手是否正在目标节点读条设卡（currentProcess 公开可见）。"""
        proc = state.opp.get("currentProcess") or {}
        return proc.get("targetNodeId") == node_id and \
            (proc.get("action") or proc.get("type")) == "SET_GUARD"

    @staticmethod
    def _opp_can_guard(state, node_id):
        """对手此刻能否在 node_id 落一张有效卡（中边冻结威胁的存在性）。

        V3.28 修正（规则审计确认级发现）：曾把"对手已有 2 张有效卡"当
        无弹药豁免——但任务书 921 行原文是"新设卡完成后超过 2 个，移除
        本队最早完成的有效设卡，已扣成本不返还"，即第 3 张卡完全合法且
        顶掉旧卡无额外代价。配额子句是反向漏洞：对手挂两张免费废卡就能
        让全部中边陷阱防御静默失效，再掐我们的踏边。删除。
        唯一的规则硬门是果品底价：KEY_PASS/宫门 1 好果（普通节点免费）。
        """
        opp = state.opp
        if not opp:
            return False
        if state.node(node_id).get("nodeType") in ("KEY_PASS", "GATE") \
                and (opp.get("goodFruit", 0) or 0) < 1:
            return False
        return True

    def _mid_edge_trap_risk(self, state, cur, nxt, plan):
        """对手占着/将先我们到达下一跳节点 → 上边有被掐点冻结的风险。

        规则依据：SET_GUARD 目标必须是其当前节点。对手离开该节点后就永远
        不能再在那里设卡；等待期间它若留卡，我们站在节点上攻坚拆（2好果+
        坏果瞬拆）远比中边冻结（6人手削弱 / 180帧风化）便宜。
        """
        def give_up():
            self._trap_wait = (None, 0)
            return False

        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return give_up()
        # V3.12 删证据门：V3.9 曾要求"对手本局设过卡"才等待，但首卡必然
        # 没有前科（replay36: 2614 全场第一张卡 r314 掐在我们上边后，几何+
        # 地形全中仍被放行，冻 195 帧零交付）。replay27 型误伤由地形门兜底：
        # 三段罚站中两段目标是普通驿站，本就不该等；剩余咽喉段误伤上界
        # 为对手真实停留时长 << 冻结 180+ 帧。
        # 地形门（V3.22 校准，2839 复盘根因 C）：普通节点只对"驻扎"分支
        # 设防——第一名实战第 4 局先到 S09（普通驿站）站定、掐我们踏边
        # 落卡（118 帧中边暂停），决策帧它是 camped 状态，语料里没有
        # "后到收敛狙击普通节点"的案例。收敛分支保持只防咽喉：一刀切
        # 扩展被电池证伪——跟在领先对手身后每站罚站，camper 42→34/48、
        # farmer 48→42/48（-640 级死局）。普通节点的驻扎等待另设预算
        # （TRAP_ORDINARY_WAIT）：蹲普通节点的是农夫不是狙击手，预算内
        # 等它走/起卡（起卡后节点上攻坚拆是便宜路径），耗尽硬闯
        ordinary = state.node(nxt).get("nodeType") not in self.GUARD_NODE_TYPES

        opp_cur = opp.get("currentNodeId")
        opp_next = opp.get("nextNodeId")
        camped = not opp.get("routeEdgeId") and opp_cur == nxt
        if camped and ordinary and not self.TRAP_CAMPED_ORDINARY:
            return give_up()
        risk = False
        if camped:
            if self._opp_forced_passing_from(state, nxt):
                return give_up()
            # 短边豁免（V3.18）：设卡读条 4 帧、完成次帧生效——边长 ≤4 帧
            # 时它当帧起手也赶不上我们过完边（正在读条的情形由上游
            # _opp_setting_guard 分支拦截）。规则数学可证，不是赌
            edge = state.graph.edge_between(cur, nxt)
            our_edge = state.graph.edge_frames(edge, state.my_speed()) \
                if edge else 0
            if edge and our_edge <= self.TRAP_GUARD_FRAMES:
                return give_up()
            if self._rush_gate_entry_release(state, nxt, our_edge):
                return give_up()
            risk = True        # 它正站在我们的下一跳上
        elif opp_next == nxt and (not ordinary or (
                self.TRAP_CONVERGE_ORDINARY
                and self._ordinary_converge_threat(state))
                or self._ordinary_long_edge_converge_threat(state, cur, nxt)):
            # 它正赶往我们的下一跳（仅咽喉）：若它先到且来得及成卡，同样危险
            opp_eta = self.planner._opp_eta(state, nxt)
            edge = state.graph.edge_between(cur, nxt)
            our_eta = state.graph.edge_frames(edge, state.my_speed()) if edge else 0
            risk = opp_eta + self.TRAP_GUARD_FRAMES < our_eta

        if not risk:
            return give_up()
        # 普通节点驻扎的有界等待：蹲普通节点的是农夫不是狙击手（语料先验），
        # 但 2839 证明"站定普通汇入点掐踏边"存在——预算内等它走/起卡，
        # 耗尽硬闯。与咽喉的无界等待（V3.15 论断）刻意不同：咽喉蹲守者
        # 十掐九中，普通节点蹲守者大概率只是在等任务波次
        if ordinary:
            _, n_wait = self._trap_wait
            if self._trap_wait[0] == nxt and n_wait >= self.TRAP_ORDINARY_WAIT:
                return give_up()
        elif self._farmer_converge_release(state, nxt, camped, ordinary):
            if self._farmer_converge_no_overlap(state, nxt, our_eta):
                if self.log:
                    self.log.info("farmer converges to choke %s but leaves before us",
                                  nxt)
                return give_up()
            _, n_wait = self._trap_wait
            if self._trap_wait[0] == nxt \
                    and n_wait >= self.TRAP_FARMER_CONVERGE_WAIT:
                if self.log:
                    self.log.info("farmer converges to choke %s, walk-in after %d",
                                  nxt, n_wait)
                return False
        # farmer 咽喉有界等待（V3.29）：三重门全中才封顶——画像 farmer、
        # 全场未见其设卡、它此刻停靠在 nxt 读任务条（读条中规则上无法
        # 同帧起手 SET_GUARD）。等待超预算即走边，别把定价层已经买单的
        # 便宜官道等成 r598 交付
        if (camped and not ordinary
                and self._opp_profile == "farmer"
                and not self.planner._guard_seen
                and self._opp_farming_here(state, nxt)):
            _, n_wait = self._trap_wait
            if self._trap_wait[0] == nxt and n_wait >= self.TRAP_FARMER_WAIT:
                if self.log:
                    self.log.info("farmer occupies choke %s, walk-in after %d",
                                  nxt, n_wait)
                # 不走 give_up()：保留计数使走边决定粘性（清零会在下一帧
                # 决策点让等待从头再来）；对手离开后由上游正常复位
                return False
        # 无弹药豁免（V3.20）：中边冻结的前提是对手真能落卡——设卡每队
        # 同时至多 2 张，KEY_PASS 还要 1 好果底价。配额用满/掏不出底价时
        # 占位只是身位，没有冻结威胁，直接过边。与短边豁免同级：规则数学
        # 可证的确定性豁免，不是概率赌。
        # （注：曾试过"余量烧穿即赌边"的无条件抢救线——竞技场证伪：对能
        # 起卡的对手，读条 4 帧掐 56 帧长边十掐九中，3 个等待可活的局
        # [seed5/17/22] 被送进 135 帧冻结；且 slack 口径不含库存马匹，
        # 触发点早 ~55 帧。等待→它起卡→节点上强通，仍是唯一有界解）
        if not self._opp_can_guard(state, nxt):
            return give_up()
        if self._trap_deadline_escape(state, plan, nxt, ordinary):
            return give_up()
        # V3.15 删对峙上限硬闯（闸门过期复盘）：V3.5 的 30 帧上限防的是
        # "对手赖着不走白耗我们"，但两类风险场景它都给错答案——
        # · 汇聚中（replay56 直接死因）：r276 起等待，r305 上限到点硬闯 71 帧
        #   长边，对手 r310 到 S10、r314 起卡，冻到 r389，终局差 40 帧未交付。
        #   汇聚窗口以对手到点自然收束（≤~70 帧且逐帧递减），到点后要么离开
        #   （风险解除）、要么设卡（enemy_guard 分支接管，节点上攻坚/强通全可用）、
        #   要么干蹲（转入下面的常驻情形）——硬闯没有任何一个分支比等待好；
        # · 常驻蹲点（V3.14 已豁免）：设卡读条 4 帧比任何边都短，上边即必冻。
        # 等待期间不是干等：空转帧用情报、它要赢也必须动身去交付。
        # 误伤上界 = 对手真实停留时长（地形门已把范围压到咽喉节点），
        # 语料实测蹲点者停留 ~30 帧 << 冻结 180+ 帧 / 未交付 500 分级。
        node, n = self._trap_wait
        n = n + 1 if node == nxt else 1
        self._trap_wait = (nxt, n)
        if self.log and n in (self.TRAP_WAIT_MAX, self.TRAP_WAIT_MAX * 3):
            self.log.info("trap wait at %s reached %d frames (opp %s)",
                          nxt, n, "camped" if camped else "converging")
        return True

    def _ordinary_long_edge_converge_threat(self, state, cur, nxt):
        """普通节点窄门防掐边：只管 replay235919 的 S07->S09 长边形态。"""
        if state.node(nxt).get("nodeType") in self.GUARD_NODE_TYPES:
            return False
        if nxt not in ("S09", "S11"):
            return False
        opp = state.opp or {}
        if opp.get("nextNodeId") != nxt or not opp.get("routeEdgeId"):
            return False
        edge = state.graph.edge_between(cur, nxt)
        our_eta = state.graph.edge_frames(edge, state.my_speed()) if edge else 0
        opp_eta = self.planner._opp_eta(state, nxt)
        if not math.isfinite(opp_eta):
            return False
        return (our_eta >= self.TRAP_ORDINARY_CONVERGE_EDGE
                and opp_eta <= self.TRAP_ORDINARY_CONVERGE_ETA)

    def _trap_deadline_escape(self, state, plan, nxt, ordinary):
        """咽喉等待的死线逃逸：等下去确定未交付时，给赌边一个出口。"""
        if ordinary or not plan or plan.kind != "deliver":
            return False
        if state.phase != P.PHASE_RUSH:
            return False
        if plan.slack > self.TRAP_DEADLINE_ESCAPE_SLACK:
            return False
        node, waited = self._trap_wait
        if node != nxt or waited < self.TRAP_DEADLINE_ESCAPE_WAIT:
            return False
        if state.enemy_guard(nxt) or self._opp_setting_guard(state, nxt):
            return False
        return True

    def _farmer_converge_release(self, state, nxt, camped, ordinary):
        """零设卡高分 farmer 正在赶往咽喉时，等待有界，避免同分输用时。"""
        if camped or ordinary:
            return False
        opp = state.opp
        if not opp or not opp.get("routeEdgeId") or opp.get("nextNodeId") != nxt:
            return False
        if self._opp_profile != "farmer" or self.planner._guard_seen:
            return False
        if (opp.get("taskScore") or 0) < self.TRAP_FARMER_CONVERGE_TASK:
            return False
        if state.enemy_guard(nxt) or self._opp_setting_guard(state, nxt):
            return False
        return True

    def _farmer_converge_no_overlap(self, state, nxt, our_eta):
        """零设卡 farmer 在边上赶往咽喉；若可见任务读完早于我方抵达，不等。

        V3.46：replay vs2696 抓到固定 12 帧观察窗的负收益。对手在边上
        规则上无法设卡，且下一站有公开任务时，高分零卡 farmer 大概率只是
        去读条后离开；若它预计离场时刻与我方到达没有重叠，等待只会错过
        后续任务窗口。没有可见任务证据时保留旧 12 帧短观察窗。
        """
        opp = state.opp
        if not opp or not opp.get("routeEdgeId") or opp.get("nextNodeId") != nxt:
            return False
        opp_eta = self.planner._opp_eta(state, nxt)
        if not math.isfinite(opp_eta) or not math.isfinite(our_eta):
            return False
        task_frames = 0
        opp_id = state.opp_id
        for t in state.tasks or []:
            if t.get("nodeId") != nxt or not t.get("active") \
                    or t.get("completed") or t.get("failed"):
                continue
            owner = t.get("ownerPlayerId", t.get("ownerId", 0)) or 0
            prot = t.get("protectionPlayerId", t.get("protectPlayerId", 0)) or 0
            if owner not in (0, opp_id) or prot not in (0, opp_id):
                continue
            if t.get("taskTemplateId") in ("T04", "T06"):
                return False
            task_frames += t.get("processRound", 4) or 4
        if task_frames <= 0:
            return False
        return opp_eta + task_frames + 2 < our_eta

    def _rush_gate_entry_release(self, state, nxt, our_edge):
        """RUSH 将开时不在宫门外长等纯占位者。

        vs2696 里对手 r349 到 S14 等 RUSH，我们 r367 在 S11 外继续等到
        r398；若直接上边，正好 r390 到门口，至少能同步验核/争门。这个
        豁免只给宫门入口、未见卡对手、且我方能处理预期门卡的局面，避免
        回退 replay56/2839 的咽喉中边保护。
        """
        if nxt != state.gate_node or state.phase == P.PHASE_RUSH:
            return False
        if state.me.get("verified") or self.planner._guard_seen:
            return False
        if self._opp_profile == "camper":
            return False
        if state.round + our_edge < RUSH_EARLIEST - 3:
            return False
        return self.planner._can_break_expected_guard(state, nxt)

    def _ordinary_converge_threat(self, state):
        """普通节点收敛掐边只在设卡型/强推进信号下成立。"""
        return self._opp_ordinary_guard_seen or self.planner.race_cliff(state)

    def _trap_reroute(self, state, cur, blocked, target):
        """陷阱等待的租买止损（V3.18）：等待帧数 ≥ 换走廊的绕路差价时改道。

        ski-rental：先等（对手随时可能离开，等待是廉价选项），等到累计
        等待追平绕路差价还没解除，就承诺改道（_trap_avoid 记入寻路惩罚，
        对手离开或到期自动解除）。总代价不超过事后最优的 2 倍。
        绕不开（blocked 是真漏斗口，如 S10/S11）时返回 None 继续等待——
        V3.15 "汇聚窗口等待优于硬闯"的结论不回退，这里只处理有第二条
        走廊可选的情形。
        """
        waited = self._trap_wait[1] if self._trap_wait[0] == blocked else 0
        g = state.graph
        penalty = self.planner._penalty_fn(state)
        ecost = self.planner._edge_cost_fn(state)
        speed = state.my_speed()
        direct, dpath = g.shortest_path(cur, target, speed, penalty, ecost)
        if not dpath:
            return None

        def avoid_pen(nid):
            return penalty(nid) + \
                (self.TRAP_AVOID_PENALTY if nid == blocked else 0)

        alt_cost, alt_path = g.shortest_path(cur, target, speed, avoid_pen, ecost)
        if len(alt_path) < 2 or blocked in alt_path:
            return None  # 无第二条走廊，等待仍是唯一解
        if waited < alt_cost - direct:
            return None  # 还没等够绕路差价，继续持有廉价的等待期权
        self._trap_avoid = (blocked, state.round + self.TRAP_AVOID_WINDOW)
        if self.log:
            self.log.info("trap wait %d >= detour %.0f, reroute via %s (avoid %s)",
                          waited, alt_cost - direct, alt_path[1], blocked)
        return alt_path[1]

    # ---------- 小分队：给任务点 / 宫门提前打探路标记（读条 -3 帧） ----------
    # 标记只活 45 帧：只探 SCOUT_MAX_ETA 帧内能赶到的目标；宫门验核最早
    # ~390 帧才开放，355 帧前派去宫门的标记必然过期（实测一局浪费 6/8 人手）。

    def _squad_avail(self, state):
        """可用人手：服务端字段优先，缺失时用本地账本（初始 8）兜底。"""
        v = state.me.get("squadAvailable")
        if v is not None:
            return v
        return max(0, 8 - self._squad_spent)

    def squad_action(self, state, plan):
        if state.phase == P.PHASE_RUSH:
            return None  # 冲刺阶段禁止新派小分队
        me = state.me
        avail = self._squad_avail(state)
        if avail <= 0:
            return None

        # 削弱敌卡优先于探路（主车队正被挡住，每帧都在流血）。
        # 削弱不受走廊预留限制——预留攒的就是这个
        if self._weaken_target and avail >= 2:
            t = self._weaken_target
            self._weaken_sent[t] = state.round
            self._weaken_target = None
            self._squad_spent += 2
            return P.a_squad_weaken(t)

        # 走廊人手预留（V3.15）：过验核前，人手是设卡战的保命弹药——
        # 4 人手 = 2 次削弱 = 把防 6 的卡削到好果可拆（replay20：人手烧光后
        # S11 第二张卡冻到终场未交付）。探路省 3 帧 ≈ 0.7 分、续防/清障也是
        # 几分级的小便宜，不许把弹药买穿。对手已交付/退赛或我们已验核后
        # 不再有设卡威胁，敞开花
        opp = state.opp
        guard_threat = (not me.get("verified") and opp
                        and not opp.get("delivered") and not opp.get("retired"))

        def can_spend(cost):
            floor = self.SQUAD_CORRIDOR_RESERVE if guard_threat else 0
            return avail - cost >= floor

        cur = me.get("currentNodeId") or me.get("nextNodeId")
        targets = []
        # 宫门优先（时机窗口窄）
        if state.round >= self.GATE_SCOUT_FROM:
            targets.append(state.gate_node)
        if plan.kind == "task" and plan.position and plan.position != cur:
            targets.append(plan.position)
        if not can_spend(1):
            targets = []

        penalty = self.planner._penalty_fn(state)
        speed = state.my_speed()
        for t in targets:
            if not self._s02_fork_scout_allowed(state, cur, t):
                continue
            if self.planner._has_our_scout_mark(state, t):
                continue
            if state.round - self._scout_sent.get(t, -999) < self.SCOUT_RESEND_GAP:
                continue
            eta, path = state.graph.shortest_path(cur, t, speed, penalty)
            if not path or eta > self.SCOUT_MAX_ETA:
                continue  # 太远：标记会在我们到达前过期
            self._scout_sent[t] = state.round
            self._squad_spent += 1
            return P.a_squad_scout(t)

        # 续防自家设卡：常规仍只在落后时续；领先局只有在对手正在攻坚
        # 该卡时才用富余人手补防，并且必须完整吃到 +2（不补超上限）。
        if avail >= 2 and can_spend(2):
            reinforce_target = self._reinforce_opportunity(state)
            if reinforce_target:
                self._reinforce_sent[reinforce_target] = state.round
                self._squad_spent += 2
                return P.a_squad_reinforce(reinforce_target)

        # 远程清障（V3.12）：路上有障碍挡着非 T04 目标时，派小分队清，主车队
        # 不用绕路/停下来自己 CLEAR；绝不碰自己正在做的 T04 目标（那要靠
        # CLAIM_TASK 才算数，小分队/CLEAR 清障只会让该 T04 失败且不重刷）
        if avail >= 2 and can_spend(2):
            clear_target = self._squad_clear_opportunity(state, plan)
            if clear_target and state.round - self._clear_sent.get(
                    clear_target, -999) >= self.SQUAD_CLEAR_RESEND_GAP:
                self._clear_sent[clear_target] = state.round
                self._squad_spent += 2
                return P.a_squad_clear(clear_target)
        return None

    def _s02_fork_scout_allowed(self, state, cur, target):
        """S02 官道/水路未承诺前，不连续预投两个互斥分支。"""
        if cur != "S02" or target not in ("S03", "S04"):
            return True
        if state.me.get("routeEdgeId"):
            return True
        key = ("S02", P.CONTEST_DOCK)
        count, last_round = self._window_draw_pressure.get(key, (0, -999))
        if count > 0 and state.round - last_round <= self.WINDOW_DRAW_PRESSURE_DECAY:
            return False
        other = "S04" if target == "S03" else "S03"
        return other not in self._scout_sent

    def _can_spend_squad(self, state, cost):
        avail = self._squad_avail(state)
        if avail < cost:
            return False
        opp = state.opp
        guard_threat = (not state.me.get("verified") and opp
                        and not opp.get("delivered") and not opp.get("retired"))
        floor = self.SQUAD_CORRIDOR_RESERVE if guard_threat else 0
        return avail - cost >= floor

    def _should_wait_for_squad_clear(self, state, plan, nxt):
        if plan is None:
            return False
        if state.phase == P.PHASE_RUSH:
            return False
        if plan.kind == "task" and (plan.task or {}).get("taskTemplateId") == "T04":
            return False
        if not self._can_spend_squad(state, 2):
            return False
        if state.round - self._clear_sent.get(nxt, -999) < self.SQUAD_CLEAR_RESEND_GAP:
            return False
        return self._squad_clear_opportunity(state, plan) == nxt

    def _reinforce_opportunity(self, state):
        """给自己还有效的设卡续防守值，领先时仅救正在被攻坚的关键卡。"""
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        for node_id in self._guard_sent:
            node = state.node(node_id)
            g = node.get("guard")
            if not g or g.get("ownerTeamId") != state.my_team:
                continue
            defense = g.get("defense", 0) or 0
            max_defense = g.get("maxDefense") or self._node_guard_cap(state, node_id)
            if defense <= 0 or defense + 2 > max_defense:
                continue
            if state.round - self._reinforce_sent.get(node_id, -999) < self.REINFORCE_RESEND_GAP:
                continue
            attack_now = self._opp_breaking_our_guard(state, node_id)
            if state.is_behind():
                if defense >= self.REINFORCE_DEFENSE_FLOOR and not attack_now:
                    continue
            elif not attack_now:
                continue
            if not attack_now:
                opp_eta = self.planner._opp_eta(state, node_id)
                if not (self.GUARD_MIN_OPP_ETA <= opp_eta <= self.GUARD_MAX_OPP_ETA):
                    continue  # 对手已经绕开或还远得很，续了也是浪费人手
            return node_id
        return None

    @staticmethod
    def _opp_breaking_our_guard(state, node_id):
        proc = state.opp.get("currentProcess") or {}
        return proc.get("targetNodeId") == node_id and \
            (proc.get("action") or proc.get("type")) == "BREAK_GUARD"

    @staticmethod
    def _node_guard_cap(state, node_id):
        node = state.node(node_id)
        node_type = node.get("nodeType")
        if node_type == "GATE":
            return 4
        if state.has_obstacle(node_id):
            return 5
        if node_type == "KEY_PASS":
            return 7
        return 6

    def _squad_clear_opportunity(self, state, plan):
        """本队去任务/资源/终点路上被障碍挡住的下一个节点；没有则 None。

        故意排除我们自己正在做的 T04：清障任务要靠 CLAIM_TASK 才算数，
        小分队/CLEAR 清掉的话该 T04 直接失败，不会重刷替代任务（5.2）。
        """
        if plan is None or plan.kind not in ("task", "resource", "deliver"):
            return None
        if plan.kind == "task" and (plan.task or {}).get("taskTemplateId") == "T04":
            return None
        me = state.me
        cur = me.get("currentNodeId") or me.get("nextNodeId")
        if not cur:
            return None
        target = plan.position if plan.kind in ("task", "resource") else (
            state.terminal_node if me.get("verified") else state.gate_node)
        if not target or target == cur:
            return None
        _, path = state.graph.shortest_path(cur, target, state.my_speed(),
                                            self.planner._penalty_fn(state))
        for nid in path[1:]:
            if state.has_obstacle(nid):
                return nid
        return None

    # ---------- 窗口 ----------

    def _priority_contest(self, state, contests, plan):
        """多窗口同帧只能出一张牌：优先出在我们志在必得的对象上。"""
        def key(c):
            if plan.kind == "task" and c.get("taskId") == (plan.task or {}).get("taskId"):
                return 0
            if c.get("contestType") == P.CONTEST_GATE:
                return 1
            return 2
        return min(contests, key=key)

    @staticmethod
    def _contest_points(state, contest):
        """(我方拍分, 对方拍分)；无法辨认颜色时返回 (0, 0) 不触发锁定判断。"""
        if contest.get("bluePlayerId") == state.player_id:
            return contest.get("bluePoint", 0) or 0, contest.get("redPoint", 0) or 0
        if contest.get("redPlayerId") == state.player_id:
            return contest.get("redPoint", 0) or 0, contest.get("bluePoint", 0) or 0
        return 0, 0

    # 窗口对象的赌注权重（分级）：赢/输一拍对不同对象的价值差异巨大——
    # PASS 是我们强通的生死拍（输=通行失败+休整重试），GATE/TASK 直接挂着
    # 验核先手/30 分任务，RESOURCE 输了不过是少 17 分里的一部分
    CONTEST_STAKE = {P.CONTEST_PASS: 10.0, P.CONTEST_GATE: 10.0,
                     P.CONTEST_TASK: 8.0, P.CONTEST_DOCK: 6.0,
                     P.CONTEST_OBSTACLE: 5.0, P.CONTEST_RESOURCE: 3.0}
    CARD_TIE_EPS = 0.4      # 期望值差在此以内视为平手，随机选（防镜像平局链）
    CARD_MIX_RATE = 0.0     # 严格劣势牌不混；镜像破局交给 DRAW 后专门逻辑

    @staticmethod
    def _opp_card_pool(state):
        """对手本拍可负担的牌集（全部来自公开字段）——它出牌只会从这里选。"""
        opp = state.opp or {}
        res = opp.get("resources") or {}
        pool = [P.CARD_ABSTAIN]
        buffs = {(b.get("type") or b.get("buffType")) for b in opp.get("buffs") or []}
        if buffs & {"FAST_HORSE", "SHORT_HORSE", "RUSH_SPEED"} \
                or res.get(P.FAST_HORSE, 0) + res.get(P.SHORT_HORSE, 0) > 0:
            pool.append(P.CARD_QIANG_XING)
        if (opp.get("guardActionPoint") or 0) > 0:
            pool.append(P.CARD_BING_ZHENG)
        if opp.get("freshness", 0) >= 80 and opp.get("goodFruit", 0) >= 1:
            pool.append(P.CARD_XIAN_GONG)
        if res.get(P.PASS_TOKEN, 0) + res.get(P.OFFICIAL_PERMIT, 0) > 0:
            pool.append(P.CARD_YAN_DIE)
        return pool

    def _get_rng(self, state):
        """混合出牌的随机源（V3.18）：(matchId, playerId) 派生种子。

        回放回归可复现（同局同序列）；混合策略的博弈价值不受影响——
        对手不知道我们的种子派生方式，序列对它仍不可预测。"""
        if self._rng is None:
            self._rng = random.Random(f"{state.match_id}:{state.player_id}")
        return self._rng

    def pick_card(self, state, contest):
        """出牌 best-response（V3.16，V3.18 加对手画像）：对手手牌全公开。

        对手本拍可负担的牌集可由公开字段精确算出（文书/护卫点/鲜度好果/
        马类增益）；对可负担集的出牌概率用本局已观察的出牌历史做拉普拉斯
        平滑加权（无观测时退化为均匀先验），再按对象赌注加权、减自身出牌
        成本，取最优。期望值打平（±CARD_TIE_EPS）时随机；不再把严格劣势
        牌混进来。镜像平局破局交给 _window_draw_break_card 这条已证实的
        S02 专门逻辑，避免 replay99 决胜拍随机偏离献贡。
        """
        me = state.me
        res = me.get("resources") or {}
        ctype = contest.get("contestType")
        rng = self._get_rng(state)

        # 拍分数学锁定即弃权（V3.14）：三拍两胜，先到 2 分胜负已定
        mine, theirs = self._contest_points(state, contest)
        if mine >= 2 or theirs >= 2:
            return P.CARD_ABSTAIN
        if self._dock_low_stake_abandon(state, contest):
            return P.CARD_ABSTAIN

        # (牌, 成本折分)：成本 = 消耗资源的机会价值
        my_options = [(P.CARD_ABSTAIN, 0.0)]
        if state.has_move_buff():
            my_options.append((P.CARD_QIANG_XING, 0.0))   # 增益期免费
        if (me.get("guardActionPoint") or 0) > 0:
            my_options.append((P.CARD_BING_ZHENG, 0.1))   # 护卫点无其他用途
        if me.get("freshness", 0) >= 80 and me.get("goodFruit", 0) > 2:
            my_options.append((P.CARD_XIAN_GONG, 1.9))    # 1 好果
        if res.get(P.PASS_TOKEN, 0) + res.get(P.OFFICIAL_PERMIT, 0) > 0:
            my_options.append((P.CARD_YAN_DIE, 0.4))      # 文书暂无他用

        pool = self._opp_card_pool(state)
        stake = self.CONTEST_STAKE.get(ctype, 5.0)

        def beat(a, b):
            """a 对 b 的拍分：任何出牌胜弃权（5.4.5），其余按克制表。"""
            if a == b:
                return 0
            if b == P.CARD_ABSTAIN:
                return 1
            if a == P.CARD_ABSTAIN:
                return -1
            if b in P.CARD_BEATS.get(a, ()):
                return 1
            if a in P.CARD_BEATS.get(b, ()):
                return -1
            return 0

        # 对手出牌频率加权（V3.18）：拉普拉斯 +1 平滑，无观测退化为均匀。
        # 真实对手有出牌偏好（demo 2614：窗口全弃权），均匀假设在扔信息
        hist = self._opp_card_hist
        breaker = self._window_draw_break_card(
            state, contest, my_options, hist, beat)
        pw = [hist.get(oc, 0) + 1.0 for oc in pool]
        pw_total = sum(pw)

        scored = []
        for card, cost in my_options:
            ev = sum(beat(card, oc) * w for oc, w in zip(pool, pw)) \
                / pw_total * stake - cost
            if breaker and card == breaker:
                ev += stake + 0.5
            scored.append((ev, card))
        scored.sort(key=lambda x: -x[0])

        best_ev = scored[0][0]
        ties = [card for ev, card in scored if best_ev - ev <= self.CARD_TIE_EPS]
        return rng.choice(ties)

    def _dock_low_stake_abandon(self, state, contest):
        """S02 首次 DRAW 后的第二窗止损。

        首窗仍争先手；一旦已经 DRAW 过，S02 的真实盘口通常只剩几帧，
        而继续献贡会烧硬通货。镜像死锁由处理站错峰解决，不靠继续打牌。
        """
        key = self._window_pressure_key(contest)
        if key != ("S02", P.CONTEST_DOCK):
            return False
        count, last_round = self._window_draw_pressure.get(key, (0, -999))
        if count < 1 or state.round - last_round > self.WINDOW_DRAW_PRESSURE_DECAY:
            return False
        if state.has_move_buff():
            return False
        return True

    def _window_draw_break_card(self, state, contest, my_options, hist, beat):
        """重复平局后跳出镜像牌型；只处理已被证实卡住的 S02 固定处理窗。"""
        key = self._window_pressure_key(contest)
        if key != ("S02", P.CONTEST_DOCK):
            return None
        count, last_round = self._window_draw_pressure.get(key, (0, -999))
        if count < self.WINDOW_DRAW_BREAK_AFTER \
                or state.round - last_round > self.WINDOW_DRAW_PRESSURE_DECAY:
            return None
        option_cards = [card for card, _ in my_options]

        if hist:
            dominant = max(hist.items(), key=lambda kv: kv[1])[0]
            counters = [card for card in option_cards
                        if card != P.CARD_ABSTAIN and beat(card, dominant) > 0]
            if counters:
                return counters[0]

        red = contest.get("redPlayerId") == state.player_id
        blue = contest.get("bluePlayerId") == state.player_id
        if not red and not blue:
            red = (state.player_id % 2 == 0)
        flip = (count // 4) % 2 == 1
        if red ^ flip:
            prefs = (P.CARD_BING_ZHENG, P.CARD_QIANG_XING,
                     P.CARD_XIAN_GONG, P.CARD_YAN_DIE, P.CARD_ABSTAIN)
        else:
            prefs = (P.CARD_XIAN_GONG, P.CARD_QIANG_XING,
                     P.CARD_BING_ZHENG, P.CARD_YAN_DIE, P.CARD_ABSTAIN)
        for card in prefs:
            if card in option_cards:
                return card
        return None
