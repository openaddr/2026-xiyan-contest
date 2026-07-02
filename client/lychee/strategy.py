"""策略层。

Strategy 是接口；BaselineStrategy 是能完整跑通「赶路 → 站点处理 → 验核 → 交付」
主线的基线实现；PlannerStrategy 在其上接入任务规划器（保 90 冲 110+）、
小分队探路和窗口出牌博弈。
- 每帧返回 actions[]（最多 1 主车队动作 + 1 小分队动作 + 1 窗口出牌 + 1 急策）；
- 用 events[]/actionResults[] 反馈修正本地状态（如站点处理是否完成）。
"""
import random

from . import protocol as P
from .planner import TaskPlanner


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
    GATE_SCOUT_FROM = 355       # 宫门验核最早 ~390 帧，此前派的标记必然过期
    CLAIM_EN_ROUTE = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE)  # 顺路领取清单
    CLAIM_LIMIT = {P.ICE_BOX: 2}    # 冰鉴多多益善（+10 鲜度 ≈ 18 分），其余各 1
    USE_ICE_BELOW = 91          # 鲜度 ≤90 就用冰鉴：+10 不溢出，且防跌破转坏阈值

    MIN_GOOD_RESERVE = 5        # 攻坚投入好果时保留的底仓（交付要求好果 > 0）
    WEAKEN_RESEND_GAP = 12      # 同一设卡的削弱重发间隔（落地延迟 ~3-5 帧）

    STALL_FRAMES = 8            # 移动进度停滞判定帧数（看门狗）

    # ---- 主动设卡（V3）----
    GUARD_NODE_TYPES = {"KEY_PASS", "PASS", "MOUNTAIN_PASS", "GATE"}  # 咽喉类节点
    GUARD_MIN_OPP_ETA = 8       # 对手至少 8 帧后才到（4 帧读条 + 生效余量）
    GUARD_MAX_OPP_ETA = 150     # 太远则风化/悬赏先到，白设
    GUARD_SLACK_MIN = 80        # 自己交付余量充足才花这 4 帧
    GUARD_ROUTE_TOLERANCE = 15  # 判断该节点是否在对手高效路线上的容差（帧）
    GUARD_RETRY_GAP = 40        # 同一节点设卡重试间隔

    def __init__(self, logger=None):
        super().__init__(logger)
        self.planner = TaskPlanner(logger)
        self._scout_sent = {}   # nodeId -> 派出帧
        self._rush_tactic_tried = False  # 护果令只尝试一次，被拒也不无限重试
        self._weaken_sent = {}  # nodeId -> 派出帧（削弱敌卡）
        self._weaken_target = None       # 本帧主车队让 squad_action 去削弱的目标
        self._last_forced_node = None    # 上次强制通行到达节点（规则禁止重复）
        self._squad_spent = 0            # 本地人手账本（服务端字段缺失时兜底）
        self._stall = (None, None, 0)    # (edgeId, progressMs, 连续停滞帧数)
        self._guard_sent = {}            # nodeId -> 设卡提交帧（防重试风暴）

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
        return actions

    # ---------- 反馈：任务被拒时临时拉黑 ----------

    def _absorb_feedback(self, state):
        super()._absorb_feedback(state)
        self._weaken_target = None

        # 移动进度停滞检测：MOVING 且 (edge, progress) 连续 N 帧不变
        me = state.me
        edge, prog = me.get("routeEdgeId"), me.get("edgeProgressMs")
        if edge and me.get("state") == P.ST_MOVING:
            last_edge, last_prog, n = self._stall
            self._stall = (edge, prog,
                           n + 1 if (edge, prog) == (last_edge, last_prog) else 0)
        else:
            self._stall = (None, None, 0)

        for e in state.my_events("FORCED_PASS_END"):
            p = e.get("payload") or {}
            node = p.get("nodeId") or p.get("targetNodeId")
            if node:
                self._last_forced_node = node  # 规则：该节点不能再次强制通行
        for action, code in state.my_rejections():
            if action == "CLAIM_TASK" and code in (
                    "TASK_REQUIREMENT_NOT_MET", "TASK_PROTECTED", "OBJECT_BUSY",
                    "TASK_EXPIRED", "TASK_NOT_FOUND", "WINDOW_DRAW_RETRY_LIMIT"):
                proc = state.me.get("currentProcess") or {}
                tid = proc.get("taskId")
                # 拒绝发生在上一帧，没有可靠 taskId 时拉黑当前计划任务
                plan = self.planner.plan(state)
                tid = tid or (plan.task or {}).get("taskId")
                if tid:
                    self.planner.blacklist_task(tid, state.round + 40)
                    if self.log:
                        self.log.info("blacklist task %s until r%d (%s)",
                                      tid, state.round + 40, code)

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
                if state.phase != P.PHASE_RUSH and self._squad_avail(state) >= 2:
                    self._weaken_target = nxt  # squad_action 本帧发 SQUAD_WEAKEN
                return None
            # 看门狗兜底：进度停滞 >=8 帧但看不到敌卡（数据异常/未知阻挡），
            # 改道走本段起点的其他相邻节点，放弃当前进度总好过冻死到终场
            if self._stall[2] >= self.STALL_FRAMES:
                alt = self._reroute_from_edge(state, me, nxt)
                if alt:
                    if self.log:
                        self.log.warning("stall watchdog: frozen %d frames on %s, "
                                         "reroute to %s", self._stall[2],
                                         me.get("routeEdgeId"), alt)
                    self._stall = (None, None, 0)
                    return P.a_move(alt)
            # 移动中只能用马类资源：没有移动增益就顺手上马（不耽误本帧推进）。
            # 马匹经济：T06 类任务要消耗整匹马，留足预留量才骑（详见 planner）
            res = me.get("resources") or {}
            if not state.has_move_buff():
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

        # 宫门验核（仅 RUSH）；验核前一帧先打护果令（免费，终段鲜度损耗 ×0.2）
        if cur == gate and not verified and plan.kind == "deliver":
            if state.phase == P.PHASE_RUSH:
                if (me.get("rushTacticUsedCount") or 0) == 0 \
                        and not self._rush_tactic_tried \
                        and me.get("freshness", 0) < 100:
                    self._rush_tactic_tried = True
                    return P.a_rush_protect()
                return P.a_verify_gate()
            return P.a_wait()

        # 固定处理站点必须先处理完才能离站
        node = state.node(cur)
        needs_process = (node.get("processType") and node.get("processType") != "VERIFY"
                         and node.get("processRound", 0) > 0)
        if needs_process and not self._processed_here:
            if self._opp_processing_here(state, cur):
                return P.a_wait()   # 对手正在处理本站流程：排队，不制造窗口
            if self._yield_for_contention(state):
                return P.a_wait()   # 同位错峰：奇偶帧让行 1 帧，避免同帧抢同一对象
            return P.a_process()

        # 任务：已在执行位置就开始读条（任务是独占对象，不让行，靠出牌博弈）
        if plan.kind == "task" and cur == plan.position:
            return P.a_claim_task(plan.task["taskId"])

        # 主动设卡：领先通过咽喉节点时，回手一张卡挡住身后的对手
        guard = self._guard_opportunity(state, cur, plan)
        if guard:
            return guard

        # 顺路领取（截止余量充足时才花这 2 帧）
        if plan.kind == "task" or plan.slack > 80:
            stock = node.get("resourceStock") or {}
            for rt in self.CLAIM_EN_ROUTE:
                limit = self.CLAIM_LIMIT.get(rt, 1)
                if stock.get(rt, 0) > 0 and res.get(rt, 0) < limit:
                    if self._yield_for_contention(state):
                        return P.a_wait()  # 错峰一帧再领，资源窗口不值得打
                    return P.a_claim_resource(cur, rt)

        # 赶路：任务点 / 宫门 / 终点
        target = plan.position if plan.kind == "task" else (terminal if verified else gate)
        if target == cur:
            return P.a_wait()
        nxt = self._route_next_hop(state, cur, target)
        if nxt is None:
            return P.a_wait()
        if state.has_obstacle(nxt) and not state.enemy_guard(nxt):
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        if state.enemy_guard(nxt):
            return self._breakthrough(state, nxt, plan)
        if self._opp_setting_guard(state, nxt):
            # 对手正在下一跳读条设卡：此时上边会在半路被冻结（边上不能攻坚），
            # 等 1~4 帧卡成型后站在节点上攻坚拆掉再走，代价小一个数量级
            return P.a_wait()
        return P.a_move(nxt)

    # ---------- 主动设卡（V3）----------
    # demo 用这招连赢我们四局：在咽喉节点身后设卡，对手要么烧果攻坚、
    # 要么吃 15+5×防守值 帧的强通税、要么等风化。成本仅 4 帧读条 + 0~3 好果。

    def _guard_opportunity(self, state, cur, plan):
        me, opp = state.me, state.opp
        if state.phase == P.PHASE_RUSH or plan.slack < self.GUARD_SLACK_MIN:
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

        # 对手确实还要从这里过：ETA 在窗口内，且该节点在其高效路线上
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_pos:
            return None
        g_graph = state.graph
        opp_eta, path = g_graph.shortest_path(opp_pos, cur, P.BASE_SPEED)
        if not path or not (self.GUARD_MIN_OPP_ETA <= opp_eta <= self.GUARD_MAX_OPP_ETA):
            return None
        opp_to_gate, p1 = g_graph.shortest_path(opp_pos, state.gate_node, P.BASE_SPEED)
        here_to_gate, p2 = g_graph.shortest_path(cur, state.gate_node, P.BASE_SPEED)
        if not p1 or not p2 or \
                opp_eta + here_to_gate > opp_to_gate + self.GUARD_ROUTE_TOLERANCE:
            return None  # 绕开我们这里更快，卡了也白卡

        # 成本：关键关隘/宫门底价 1 好果 + 额外 2 好果拉满防守值 6
        # （防 2 的卡 30 帧就风化半残，不值底价；投不满就不投）
        good = me.get("goodFruit", 0)
        base_cost = 1 if node.get("nodeType") in ("KEY_PASS", "GATE") else 0
        extra = 2
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

    # ---------- 突破敌方设卡 ----------
    # 平台败局教训：在 S09 干等 S10 敌卡风化 175 帧直接导致未交付（80:525）。
    # 优先级：攻坚(坏果优先,瞬发) > 小分队削弱后攻坚 > 强制通行(时间税<=50帧) > 等。

    def _breakthrough(self, state, target, plan):
        me = state.me
        g = state.enemy_guard(target)
        defense = g.get("defense", 0) or 0

        # 1) 一击必破：攻坚值 = 好果x2 + 坏果x3，投入各最多 2 篓，无读条
        invest = self._break_invest(defense, me.get("goodFruit", 0),
                                    me.get("badFruit", 0))
        if invest:
            gf, bf = invest
            if self.log:
                self.log.info("break guard %s def=%d with good=%d bad=%d",
                              target, defense, gf, bf)
            return P.a_break_guard(target, gf, bf)

        # 2) 果品不够破：派小分队削弱（-2/次），主车队本帧等待
        #    截止吃紧(slack<0)时跳过，直接走强制通行
        if plan.slack > 0 and state.phase != P.PHASE_RUSH \
                and self._squad_avail(state) >= 2:
            if state.round - self._weaken_sent.get(target, -999) >= self.WEAKEN_RESEND_GAP:
                self._weaken_target = target  # squad_action 本帧发 SQUAD_WEAKEN
            return P.a_wait()

        # 3) 强制通行兜底：关键关隘时间税最多 50 帧，仍远好于等风化
        if target != self._last_forced_node:
            return P.a_forced_pass(target)
        return P.a_wait()

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
        """与规划器共用同一套阻挡惩罚，保证走的路就是估值时算的路。"""
        return state.graph.next_hop(cur, target, state.my_speed(),
                                    self.planner._penalty_fn(state))

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

        # 削弱敌卡优先于探路（主车队正被挡住，每帧都在流血）
        if self._weaken_target and avail >= 2:
            t = self._weaken_target
            self._weaken_sent[t] = state.round
            self._weaken_target = None
            self._squad_spent += 2
            return P.a_squad_weaken(t)

        cur = me.get("currentNodeId") or me.get("nextNodeId")
        targets = []
        # 宫门优先（时机窗口窄）；保留 1 人手给宫门
        if state.round >= self.GATE_SCOUT_FROM:
            targets.append(state.gate_node)
        if plan.kind == "task" and plan.position and plan.position != cur and avail > 1:
            targets.append(plan.position)

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
        return None

    def _reroute_from_edge(self, state, me, blocked_next):
        """路线边上改道：从本段起点的其他合法相邻节点中，选一条仍能到
        目标（宫门/终点）的最快替代路；没有可行替代返回 None。"""
        seg_start = me.get("currentNodeId")
        if not seg_start:
            return None
        target = state.terminal_node if me.get("verified") else state.gate_node
        penalty = self.planner._penalty_fn(state)
        speed = state.my_speed()
        best = None
        for nb, _ in state.graph.neighbors(seg_start):
            if nb == blocked_next or state.is_blocked(nb):
                continue
            eta, path = state.graph.shortest_path(nb, target, speed, penalty)
            if path and (best is None or eta < best[1]):
                best = (nb, eta)
        return best[0] if best else None

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

    def pick_card(self, state, contest):
        """混合策略出牌：可负担的牌按强度加权随机。

        确定性出牌遇到同水平对手会陷入平局链（休整 3 帧 + 抑制 18 帧，
        镜像对局曾因此双双 0 分锁死在 S02）。加权随机让平局概率降到
        每拍 ~30% 以下，2~3 拍内大概率分出胜负。
        """
        me = state.me
        res = me.get("resources") or {}
        ctype = contest.get("contestType")

        options = []  # (牌, 权重)
        if state.has_move_buff():
            options.append((P.CARD_QIANG_XING, 3.0))   # 增益期免费
        if (me.get("guardActionPoint") or 0) > 0:
            options.append((P.CARD_BING_ZHENG, 3.0))   # 克验牒/强行，无其他用途
        if me.get("freshness", 0) >= 80 and me.get("goodFruit", 0) > 2:
            options.append((P.CARD_XIAN_GONG, 2.0))    # 克验牒/兵争，费 1 好果
        if res.get(P.PASS_TOKEN, 0) + res.get(P.OFFICIAL_PERMIT, 0) > 0:
            options.append((P.CARD_YAN_DIE, 1.5))      # 克强行，文书暂无他用
        if not options:
            return P.CARD_ABSTAIN

        # 低价值对象（顺手抢的资源）多混弃权省成本；关键对象少弃权
        abstain_w = 1.5 if ctype == P.CONTEST_RESOURCE else 0.5
        options.append((P.CARD_ABSTAIN, abstain_w))

        total = sum(w for _, w in options)
        pick = random.random() * total
        for card, w in options:
            pick -= w
            if pick <= 0:
                return card
        return options[-1][0]
