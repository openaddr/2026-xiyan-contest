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
    SCOUT_RESERVE = 0           # 保留人手（V1 全部可用于探路）
    CLAIM_EN_ROUTE = (P.ICE_BOX, P.FAST_HORSE, P.SHORT_HORSE)  # 顺路领取清单

    def __init__(self, logger=None):
        super().__init__(logger)
        self.planner = TaskPlanner(logger)
        self._scout_sent = {}   # nodeId -> 派出帧

    # ---------- 每帧入口 ----------

    def decide(self, state):
        self._absorb_feedback(state)
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
        me = state.me
        if (actions and me.get("state") == P.ST_MOVING and me.get("nextNodeId")
                and not any(a["action"] in P.MAIN_ACTION_TYPES for a in actions)):
            actions.append(P.a_move(me["nextNodeId"]))
        return actions

    # ---------- 反馈：任务被拒时临时拉黑 ----------

    def _absorb_feedback(self, state):
        super()._absorb_feedback(state)
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
            # 移动中只能用马类资源：没有移动增益就顺手上马（不耽误本帧推进）
            res = me.get("resources") or {}
            if not state.has_move_buff():
                for horse in (P.FAST_HORSE, P.SHORT_HORSE):
                    if res.get(horse, 0) > 0:
                        return P.a_use_resource(horse)
            return None  # 让系统继续推进

        cur = me.get("currentNodeId")
        gate, terminal = state.gate_node, state.terminal_node
        verified = me.get("verified")
        plan = plan or self.planner.plan(state)

        # 终点交付
        if cur == terminal:
            if verified and me.get("goodFruit", 0) > 0 and me.get("freshness", 0) > 0:
                return P.a_deliver()
            return P.a_wait()

        # 宫门验核（仅 RUSH）
        if cur == gate and not verified and plan.kind == "deliver":
            if state.phase == P.PHASE_RUSH:
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

        # 保鲜
        res = me.get("resources") or {}
        if me.get("freshness", 100) < self.USE_ICE_BELOW and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)

        # 顺路领取（截止余量充足时才花这 2 帧）
        if plan.kind == "task" or plan.slack > 80:
            stock = node.get("resourceStock") or {}
            for rt in self.CLAIM_EN_ROUTE:
                if stock.get(rt, 0) > 0 and res.get(rt, 0) == 0:
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
        if state.has_obstacle(nxt):
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        if state.enemy_guard(nxt):
            return P.a_wait()  # 攻坚/强制通行留给 V2
        return P.a_move(nxt)

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

    # ---------- 小分队：给任务点 / 宫门提前打探路标记（读条 -3 帧） ----------

    def squad_action(self, state, plan):
        if state.phase == P.PHASE_RUSH:
            return None  # 冲刺阶段禁止新派小分队
        me = state.me
        if (me.get("squadAvailable") or 0) <= self.SCOUT_RESERVE:
            return None

        cur = me.get("currentNodeId")
        targets = []
        # 任务点：人还没到才值得探（标记落地有延迟，就地开读条来不及吃到减时）
        if plan.kind == "task" and plan.position and plan.position != cur:
            targets.append(plan.position)
        targets.append(state.gate_node)  # 宫门验核 6->3 帧，稳赚

        for t in targets:
            if self.planner._has_our_scout_mark(state, t):
                continue
            if state.round - self._scout_sent.get(t, -999) < self.SCOUT_RESEND_GAP:
                continue
            self._scout_sent[t] = state.round
            return P.a_squad_scout(t)
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
