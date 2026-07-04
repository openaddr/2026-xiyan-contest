"""策略层。

Strategy 是接口；BaselineStrategy 是能完整跑通「赶路 → 站点处理 → 验核 → 交付」
主线的基线实现；PlannerStrategy 在其上接入任务规划器（保 90 冲 110+）、
小分队探路和窗口出牌博弈。
- 每帧返回 actions[]（最多 1 主车队动作 + 1 小分队动作 + 1 窗口出牌 + 1 急策）；
- 用 events[]/actionResults[] 反馈修正本地状态（如站点处理是否完成）。
"""
import random

from . import protocol as P
from .planner import (TaskPlanner, FUNNEL_FIRST_WEATHER,
                      FUNNEL_WEATHER_GAP)

# 对手手册（V3.24）：按对手 playerId 应用赛前预设。前推偏置全局默认开
# 被电池证伪（camper 未交付 4→10——开局提速重掷与蹲点者起卡周期的相位
# 骰子），但对官道冲锋型对手价值巨大（vs toller margin +32→+248，代价
# 2/48）。第一名 2839（lychee-py，边冲边农+汇入点回手卡，四局逐帧复刻
# 般确定）是该预设的实锤适用对象。键 = 对手 playerId（开局即知）
OPPONENT_BOOK = {
    2839: {"planner.FORWARD_BIAS_FLOOR": 0.6},   # 第一名：官道冲锋型
}


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
    # 顺路领取清单：冰鉴/马之外补上文书（充实验牒 YAN_DIE 出牌池，克强行 QIANG_XING，
    # 此前从不主动领导致这张克制牌常年打不出）和情报（免费囤着，等空转帧顺手用掉）
    CLAIM_EN_ROUTE = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE,
                      P.PASS_TOKEN, P.OFFICIAL_PERMIT, P.INTEL)
    # 竞速模式下的收缩清单（V3.18）：只领交付硬件与速度资源
    RACE_CLAIM_ONLY = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE)
    CLAIM_LIMIT = {P.ICE_BOX: 2}    # 冰鉴多多益善（+10 鲜度 ≈ 18 分），其余各 1
    USE_ICE_BELOW = 91          # 鲜度 ≤90 就用冰鉴：+10 不溢出，且防跌破转坏阈值

    MIN_GOOD_RESERVE = 5        # 攻坚投入好果时保留的底仓（交付要求好果 > 0）
    WEAKEN_RESEND_GAP = 12      # 同一设卡的削弱重发间隔（落地延迟 ~3-5 帧）

    # ---- 主动设卡（V3）----
    GUARD_NODE_TYPES = {"KEY_PASS", "PASS", "MOUNTAIN_PASS", "GATE"}  # 咽喉类节点
    GUARD_MIN_OPP_ETA = 8       # 对手至少 8 帧后才到（4 帧读条 + 生效余量）
    GUARD_MAX_OPP_ETA = 150     # 太远则风化/悬赏先到，白设
    # V3.12：80 → 65。V3.7 修了 ETA 度量后 4 局仍 0 次设卡——replay31 领跑局
    # 仿真：S10/S11 咽喉停靠帧 slack 分布 70~84，全被 80 拦掉（SAFETY_MARGIN
    # 60 已内含一道保险，等效要求 140 帧真余量）。65 落在实测分布之下、
    # 仍留 4 帧读条的 16 倍缓冲。
    GUARD_SLACK_MIN = 65        # 自己交付余量充足才花这 4 帧
    GUARD_ROUTE_TOLERANCE = 15  # 判断该节点是否在对手高效路线上的容差（帧）
    GUARD_RETRY_GAP = 40        # 同一节点设卡重试间隔
    # 关隘热设卡（V3.18）：刚赢下漏斗竞速、对手正被汇过来（ETA ≤60）时，
    # 4 帧读条 + 1~3 好果换对手 45+ 帧死等/满防税，是竞速胜利的兑现动作
    # ——65 的常规闸门在这个场景下把"过关隘必设卡"整档拦掉（对手 2614
    # 的同款打法：r314 立卡后边卡边农）。热窗口降到 25，仍留 6 倍读条缓冲
    GUARD_SLACK_HOT = 25
    GUARD_HOT_OPP_ETA = 60      # 对手到本关隘的 ETA 在此内算"热"

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
    TRAP_CONVERGE_ORDINARY = False  # 实验开关：收敛分支是否也防普通节点
                                # （无界版被电池证伪 camper 34/48；有界
                                # 变体的配对对照见 trap-gate 实验脚本）
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

    # ---- 尾段蹲刷（V3.10）----
    # 任务刷新跟在车队身后：领跑者吃冰，跟随者吃刷新（29/30/31 三局对手
    # 全靠尾段刷新农到 180 任务分，我们前 200 帧后零任务）。余量充足且
    # 里程碑未拿满时，站在任务候选点上等刷新，比冲刺快 40 帧（≈8分）值钱。
    LOITER_MIN_SLACK = 110      # 蹲刷要求的最小交付余量（帧）
    LOITER_BASE_CAP = 110       # 任务基础分达到该值后不再蹲（末档里程碑已到手）
    LOITER_BUDGET = 50          # 整局蹲刷总预算（帧），有限下注

    # ---- 情报（INTEL，V3.12）----
    INTEL_DISTANCE_LIMIT = 15   # 任务书 3.3.4：目标距离超过 15 时使用被拒

    # ---- 小分队远程清障（V3.12）----
    SQUAD_CLEAR_RESEND_GAP = 18   # 落地延迟上限 15 帧 + 余量，防重复派人

    # ---- 小分队增援（V3.12）----
    REINFORCE_DEFENSE_FLOOR = 4    # 自家设卡防守值跌破该值才续
    REINFORCE_RESEND_GAP = 30      # 同一节点续防重试间隔

    def __init__(self, logger=None):
        super().__init__(logger)
        self.planner = TaskPlanner(logger)
        self._scout_sent = {}   # nodeId -> 派出帧
        self._rush_tactic_tried = False  # 护果令只尝试一次，被拒也不无限重试
        self._weaken_sent = {}  # nodeId -> 派出帧（削弱敌卡）
        self._guard_first_seen = {}  # nodeId -> 首见该敌卡的帧（临别卡宽限计时）
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
        self._trap_wait = (None, 0)      # (等待的目标节点, 连续等待帧数)
        self._trap_avoid = (None, -1)    # (租买改道要绕开的节点, 承诺到期帧)
        self._opp_card_hist = {}         # 对手本局出牌频次（WINDOW_CARD_REVEAL）
        self._rng = None                 # (matchId, playerId) 派生种子，回放可复现
        self._opp_stationary = (None, 0)  # (对手停靠节点, 起始帧)——驻扎判定
        self._book_applied = False       # 对手手册预设只应用一次
        self._loiter_spent = 0           # 尾段蹲刷已用帧数（预算制）
        self._last_main_action = None    # 上一帧提交的主车队动作（拒绝反馈的 join 键）
        self._clear_sent = {}            # nodeId -> 小分队清障派出帧（防重试风暴）
        self._reinforce_sent = {}        # nodeId -> 小分队增援派出帧（防重试风暴）
        self._opp_profile = "unknown"    # 对手画像（V3.20）：unknown/camper，粘性
        self._prof_idle_choke = 0        # 对手在 KEY_PASS 闲置驻扎的累计帧数

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

        # 对手手册（V3.24）：首帧按对手 playerId 应用赛前预设（只一次）
        if not self._book_applied:
            self._book_applied = True
            preset = OPPONENT_BOOK.get(getattr(state, "opp_id", None))
            if preset:
                for key, val in preset.items():
                    scope, attr = key.split(".", 1)
                    obj = self.planner if scope == "planner" else self
                    setattr(obj, attr, val)
                if self.log:
                    self.log.info("opponent book applied for %s: %r",
                                  state.opp_id, preset)

        # 敌卡消失（被拆/风化/失效）即重置宽限计时：同节点再立新卡重新起算
        for node_id in list(self._guard_first_seen):
            if not state.enemy_guard(node_id):
                del self._guard_first_seen[node_id]
        # 对手驻扎追踪（V3.19）：停靠在同一节点的起始帧，供"坐地户免宽限"
        # 与画像分类器用（原始口径：含做任务帧）
        opp = state.opp
        if opp and not opp.get("routeEdgeId") and opp.get("currentNodeId"):
            if self._opp_stationary[0] != opp["currentNodeId"]:
                self._opp_stationary = (opp["currentNodeId"], state.round)
        else:
            self._opp_stationary = (None, state.round)

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
        for e in state.events:
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

    def _profile_tick(self, state):
        opp = state.opp
        node_id, _ = self._opp_stationary
        if not opp or not node_id:
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
                # - 留 2 人手保底：第二张卡才是杀招（replay20: S10 烧光人手后
                #   S11 再冻 180 帧到终场未交付）
                if (state.phase != P.PHASE_RUSH
                        and self._squad_avail(state) >= 4
                        and not self._opp_at_node(state, nxt)
                        and state.round - self._weaken_sent.get(nxt, -999)
                        >= self.WEAKEN_RESEND_GAP):
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

        # 保鲜优先于一切等待/交付：+10 鲜度 ≈ 18 分，交付前一帧用也稳赚，
        # 且在宫门等 RUSH、终点等交付的空闲帧里防止跌破转坏阈值
        res = me.get("resources") or {}
        if me.get("freshness", 100) < self.USE_ICE_BELOW and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)

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

        # 固定处理站点必须先处理完才能离站。
        # V3.7：不再奇偶让行 —— 对不让行的对手等于每次白送 5 帧先手，
        # 而 S02 的 5 帧先手决定整条冰链归属（replay23 实锤）。同帧撞车
        # 就打 DOCK 窗口：我们有 4 张兵争 + 混合出牌，期望优于必然让行；
        # 镜像平局链由混合出牌概率性打破。
        node = state.node(cur)
        needs_process = (node.get("processType") and node.get("processType") != "VERIFY"
                         and node.get("processRound", 0) > 0)
        if needs_process and not self._processed_here:
            if self._opp_processing_here(state, cur):
                return self._idle_upgrade(state, plan)  # 排队等对手处理完，顺手用情报
            # 情报预热（V3.16）：读条 ≥4 帧的站先花 1 帧上情报（-3 净省 2）
            warm = self._intel_prewarm(state, cur, node.get("processRound", 0))
            if warm:
                return warm
            return P.a_process()

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

        # 主动设卡：领先通过咽喉节点时，回手一张卡挡住身后的对手
        guard = self._guard_opportunity(state, cur, plan)
        if guard:
            return guard

        # 顺路领取（余量闸门 15：领取只花 2 帧读条，换 +18 分几乎恒值；
        # 阴影惩罚会压低 slack，这里的闸门只挡真正的临门一脚）
        if plan.kind in ("task", "resource") or plan.slack > 15:
            stock = node.get("resourceStock") or {}
            # 竞速期（V3.18）只领交付硬件（冰=鲜度）和速度（马）：文书/
            # 情报各 2 帧读条在竞争带内是胜负帧，赢下漏斗后有的是空转帧补
            claim_list = self.RACE_CLAIM_ONLY \
                if self.planner.race_mode(state) else self.CLAIM_EN_ROUTE
            if self.planner.race_cliff(state):
                # 悬崖带（V3.21）：2 帧读条也是胜负帧——冰/马也不顺手领。
                # 只影响这个 ~60 帧窗口的路过领取；冰链作为规划目标的口径
                # （race_adjust=False）不受影响，追猎照常
                claim_list = ()
            for rt in claim_list:
                limit = self.CLAIM_LIMIT.get(rt, 1)
                if stock.get(rt, 0) > 0 and res.get(rt, 0) < limit:
                    if self._yield_for_contention(state):
                        return P.a_wait()  # 错峰一帧再领，资源窗口不值得打
                    return P.a_claim_resource(cur, rt)

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
            # 防中边陷阱：等对手离开我们的下一跳再上边。等待不是无价的——
            # 等够绕路差价后租买止损改道（V3.18），绕不开的真漏斗口继续等
            alt = self._trap_reroute(state, cur, nxt, target)
            if alt:
                return P.a_move(alt)
            return self._idle_upgrade(state, plan)
        return P.a_move(nxt)

    # ---------- 主动设卡（V3）----------
    # demo 用这招连赢我们四局：在咽喉节点身后设卡，对手要么烧果攻坚、
    # 要么吃 15+5×防守值 帧的强通税、要么等风化。成本仅 4 帧读条 + 0~3 好果。

    def _guard_opportunity(self, state, cur, plan):
        me, opp = state.me, state.opp
        if state.phase == P.PHASE_RUSH:
            return None
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        node = state.node(cur)
        if node.get("nodeType") not in self.GUARD_NODE_TYPES:
            return None
        if cur == state.terminal_node:
            return None  # S15 禁止设卡
        g = node.get("guard")
        if g and g.get("ownerTeamId"):  # 每节点同时只有 1 个有效卡
            active = g.get("active", g.get("defense", 0) > 0)
            if active:
                return None
        if state.round - self._guard_sent.get(cur, -999) < self.GUARD_RETRY_GAP:
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
        # slack 闸门分档（V3.18）：常规 65；关键关隘上对手正被漏斗汇过来
        # （热窗口）时降到 25——刚赢的竞速要立刻兑现，让它吃我们吃过的苦
        slack_min = self.GUARD_SLACK_MIN
        if node.get("nodeType") == "KEY_PASS" \
                and opp_eta <= self.GUARD_HOT_OPP_ETA:
            slack_min = self.GUARD_SLACK_HOT
        if plan.slack < slack_min:
            return None
        # 两侧都用含边上余量的同一度量（V3.7：度量不一致曾让该检查恒假）
        opp_to_gate = self.planner._opp_eta(state, state.gate_node)
        here_to_gate, p2 = g_graph.shortest_path(cur, state.gate_node, P.BASE_SPEED)
        if not p2 or opp_to_gate == float("inf") or \
                opp_eta + here_to_gate > opp_to_gate + self.GUARD_ROUTE_TOLERANCE:
            return None  # 绕开我们这里更快，卡了也白卡

        # 成本：关键关隘/宫门底价 1 好果 + 额外好果按节点防守值上限投满不投溢
        # （防 2 的卡 30 帧就风化半残，不值底价；投不满就不投）。
        # 宫门上限 4（6.2.1）：extra=1 即 2+2=4 拉满，extra=2 超上限的那篓
        # 不提防守且成本不返还，纯白烧
        good = me.get("goodFruit", 0)
        base_cost = 1 if node.get("nodeType") in ("KEY_PASS", "GATE") else 0
        extra = 1 if node.get("nodeType") == "GATE" else 2
        if good - base_cost - extra <= self.MIN_GOOD_RESERVE:
            return None  # 好果太紧，不做对抗投资
        self._guard_sent[cur] = state.round
        if self.log:
            self.log.info("set guard @%s extra=%d (opp eta=%d)", cur, extra, opp_eta)
        return P.a_set_guard(cur, extra)

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

    # ---------- 情报：空转帧顺手用（V3.12）----------
    # 注定 WAIT 的帧（排队/防陷阱/蹲刷等）不占主车队移动时间，此时若手里有情报，
    # 顺手标一个目标节点：效果与小分队探路相同（处理帧 -3，最低 2），但完全不占
    # 人手，机会成本≈0——专程为它停下不划算，只在反正要空等的帧里用。

    def _idle_upgrade(self, state, plan):
        me = state.me
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
                if forced_ok:
                    if self.log:
                        self.log.info("camper holds %s past grace, forced pass",
                                      target)
                    return P.a_forced_pass(target)
                return P.a_wait()

        # 1) 一击必破：攻坚值 = 好果x2 + 坏果x3，投入各最多 2 篓，无读条
        invest = self._break_invest(defense, me.get("goodFruit", 0),
                                    me.get("badFruit", 0))
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
        return state.graph.next_hop(cur, target, state.my_speed(), penalty,
                                    self.planner._edge_cost_fn(state))

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

    @staticmethod
    def _opp_at_node(state, node_id):
        """对手主车队正停靠在该节点上（能以 ≤3 好果原地补卡，削弱=喂饵）。"""
        opp = state.opp
        return bool(opp and not opp.get("delivered") and not opp.get("retired")
                    and not opp.get("routeEdgeId")
                    and opp.get("currentNodeId") == node_id)

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

        规则口径：每队同时激活的卡至多 2 张；KEY_PASS/宫门设卡有 1 好果
        底价（普通节点 d2 免费）。任一条不满足 → 占位无冻结威胁。
        """
        opp = state.opp
        if not opp:
            return False
        if sum(1 for nid in state.nodes if state.enemy_guard(nid)) >= 2:
            return False
        if state.node(node_id).get("nodeType") == "KEY_PASS" \
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
            # 短边豁免（V3.18）：设卡读条 4 帧、完成次帧生效——边长 ≤4 帧
            # 时它当帧起手也赶不上我们过完边（正在读条的情形由上游
            # _opp_setting_guard 分支拦截）。规则数学可证，不是赌
            edge = state.graph.edge_between(cur, nxt)
            our_edge = state.graph.edge_frames(edge, state.my_speed()) \
                if edge else 0
            if edge and our_edge <= self.TRAP_GUARD_FRAMES:
                return give_up()
            risk = True        # 它正站在我们的下一跳上
        elif opp_next == nxt and (not ordinary or self.TRAP_CONVERGE_ORDINARY):
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

        # 续防自家设卡（V3.12）：只在落后时续——领先时故意不续，让快风化的卡
        # 自然失效，别把它续到破关悬赏 30/60 帧的触发线上，被拆时反倒给
        # 对手送追分分（悬赏只在攻破方总分更低时结算，见 6.3.3）
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

    def _reinforce_opportunity(self, state):
        """给自己还有效的设卡续防守值，仅在落后时（见上方注释的悬赏风险）。"""
        if not state.is_behind():
            return None
        opp = state.opp
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        for node_id in self._guard_sent:
            node = state.node(node_id)
            g = node.get("guard")
            if not g or g.get("ownerTeamId") != state.my_team:
                continue
            defense = g.get("defense", 0) or 0
            if defense <= 0 or defense >= self.REINFORCE_DEFENSE_FLOOR:
                continue
            if state.round - self._reinforce_sent.get(node_id, -999) < self.REINFORCE_RESEND_GAP:
                continue
            opp_eta = self.planner._opp_eta(state, node_id)
            if not (self.GUARD_MIN_OPP_ETA <= opp_eta <= self.GUARD_MAX_OPP_ETA):
                continue  # 对手已经绕开或还远得很，续了也是浪费人手
            return node_id
        return None

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
    CARD_MIX_RATE = 0.15    # 少量混合：纯 best-response 对镜像会锁死同牌平局

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
        成本，取最优。期望值打平（±CARD_TIE_EPS）时随机、另留 CARD_MIX_RATE
        概率按软权重混合——纯确定性 best-response 对镜像对手会锁死同牌
        平局链（休整 3 帧 + 抑制 18 帧，曾双双 0 分）。
        """
        me = state.me
        res = me.get("resources") or {}
        ctype = contest.get("contestType")
        rng = self._get_rng(state)

        # 拍分数学锁定即弃权（V3.14）：三拍两胜，先到 2 分胜负已定
        mine, theirs = self._contest_points(state, contest)
        if mine >= 2 or theirs >= 2:
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
        pw = [hist.get(oc, 0) + 1.0 for oc in pool]
        pw_total = sum(pw)

        scored = []
        for card, cost in my_options:
            ev = sum(beat(card, oc) * w for oc, w in zip(pool, pw)) \
                / pw_total * stake - cost
            scored.append((ev, card))
        scored.sort(key=lambda x: -x[0])

        if rng.random() < self.CARD_MIX_RATE:
            # 软权重混合（EV 平移到正区间），保留对镜像的不可预测性
            floor_ = min(ev for ev, _ in scored)
            weights = [(ev - floor_ + 0.5, card) for ev, card in scored]
            total = sum(w for w, _ in weights)
            pick = rng.random() * total
            for w, card in weights:
                pick -= w
                if pick <= 0:
                    return card
            return weights[-1][1]
        best_ev = scored[0][0]
        ties = [card for ev, card in scored if best_ev - ev <= self.CARD_TIE_EPS]
        return rng.choice(ties)
