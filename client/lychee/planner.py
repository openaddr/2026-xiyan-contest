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
RUSH_EARLIEST = 390         # 宫宴冲刺最早可能触发帧（任务书 6.5）
GATE_VERIFY_FRAMES = 6      # 宫门验核处理帧数
DELIVER_FRAMES = 2          # 到终点 + 交付
FRESH_VALUE_PER_FRAME = 0.11   # 每帧鲜度损耗折分 ≈ 0.055(官道) × 1.8(分/鲜度) + 阈值摊销
TIME_SCORE_PER_FRAME = 70.0 / 600.0   # 用时分斜率（任务系数拉满时）
RAW_TIME_SCORE_EST = 25     # 估算用的原始用时分（约 385 帧交付）
CONTEST_RISK_DISCOUNT = 0.5  # 对手比我们更近时的估值折扣
# 阻挡节点的寻路惩罚按真实处理代价估：会计入 ETA，不能虚高
OBSTACLE_PENALTY = 10       # 清障 6 帧读条 + 1 好果 / 强通税 8 帧
GUARD_PENALTY = 35          # 强通时间税 min(40, 10+防守值×5) 量级


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
    """kind: 'task' 去做任务 / 'deliver' 直奔宫门交付线 / 'hold' 原地。"""

    __slots__ = ("kind", "task", "position", "detail", "slack")

    def __init__(self, kind, task=None, position=None, detail="", slack=0):
        self.kind = kind
        self.task = task          # 任务实例 dict
        self.position = position  # 执行任务应停靠的节点
        self.detail = detail
        self.slack = slack        # 交付截止余量（帧），负数=已进入抢救模式

    def __repr__(self):
        tid = self.task.get("taskId") if self.task else None
        return f"Plan({self.kind}, task={tid}, pos={self.position}, slack={self.slack}, {self.detail})"


class TaskPlanner:
    def __init__(self, logger=None):
        self.log = logger
        self.blacklist = {}   # taskId -> 解禁帧（吃到拒绝后临时拉黑）

    # ================= 对外入口 =================

    def plan(self, state):
        me = state.me
        cur = self._anchor_node(state)
        if not cur:
            return Plan("hold", detail="no position")
        penalty = self._penalty_fn(state)
        speed = state.my_speed()
        g = state.graph

        to_gate, _ = g.shortest_path(cur, state.gate_node, speed, penalty)
        gate_to_term, _ = g.shortest_path(state.gate_node, state.terminal_node, speed)
        eta_direct = to_gate + GATE_VERIFY_FRAMES + gate_to_term + DELIVER_FRAMES
        slack = state.duration_round - (state.round + eta_direct + SAFETY_MARGIN)

        # 已验核后离开宫门需要重新验核（6 帧），V1 不再接任务，直奔交付
        if me.get("verified"):
            return Plan("deliver", detail="verified", slack=slack)
        if slack <= 0:
            return Plan("deliver", detail="deadline", slack=slack)

        base = me.get("taskScore", 0) or 0
        best, best_net = None, 0.0
        for t in state.claimable_tasks():
            if self.blacklist.get(t["taskId"], 0) > state.round:
                continue
            ev = self._evaluate(state, t, cur, base, to_gate, eta_direct,
                                slack, speed, penalty)
            if ev and ev[0] > best_net:
                best_net, best = ev[0], (t, ev[1])

        if best:
            t, pos = best
            return Plan("task", t, pos, slack=slack,
                        detail=f"net={best_net:.0f} base={base}")
        return Plan("deliver", detail=f"no worthy task, base={base}", slack=slack)

    # ================= 估值 =================

    def _evaluate(self, state, task, cur, base, to_gate, eta_direct,
                  slack, speed, penalty):
        """返回 (净收益, 停靠节点)；不可行返回 None。"""
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

        g = state.graph
        pos, f_to = self._position_for(state, task, cur, speed, penalty)
        if pos is None:
            return None

        proc = task.get("processRound", 4) or 4
        if self._has_our_scout_mark(state, pos):
            proc = max(2, proc - 3)

        # 过期检查：赶到 + 读完条要在过期帧之前
        expire = task.get("expireRound") or 0
        if expire and state.round + f_to + proc > expire:
            return None

        # 绕路帧数 = (当前->任务点 + 任务点->宫门) - 当前->宫门直达
        f_back, back_path = g.shortest_path(pos, state.gate_node, speed, penalty)
        if not back_path:
            return None
        detour = max(0, f_to + f_back - to_gate)
        total_frames = detour + proc
        if total_frames > slack:  # 硬约束：不许危及交付
            return None

        value = marginal_task_value(base, task.get("score", 0))
        # 对手风险：对手离任务点更近时打折；对手正在处理该任务则放弃
        opp_eta = self._opp_eta(state, pos)
        if self._opp_processing_task(state, task):
            return None
        if opp_eta < f_to:
            value *= CONTEST_RISK_DISCOUNT

        cost = total_frames * self._frame_value(state, eta_direct)
        net = value - cost
        return (net, pos) if net > 0 else None

    def _position_for(self, state, task, cur, speed, penalty):
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
                f, path = g.shortest_path(cur, c, speed, penalty)
                if path and (best is None or f < best[1]):
                    best = (c, f)
            return best if best else (None, None)
        f, path = g.shortest_path(cur, node, speed, penalty)
        if not path:
            return None, None
        return node, f

    # ================= 帧价值与辅助 =================

    @staticmethod
    def _frame_value(state, eta_direct):
        """一帧的机会成本 = 鲜度损耗 + 用时分斜率。

        用时分按交付帧计算，而绕路 1 帧交付就晚 1 帧 —— 无论现在离
        宫宴冲刺多远，这 0.117 分/帧都是实打实的（平台三局我们交付
        都在 537+，比对手晚 40~60 帧，鲜度+用时合计输 30 分）。
        """
        return FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME

    @staticmethod
    def _anchor_node(state):
        """规划起点：停靠节点，或移动中的当前目标节点。"""
        me = state.me
        if me.get("routeEdgeId"):
            return me.get("nextNodeId") or me.get("currentNodeId")
        return me.get("currentNodeId")

    def _penalty_fn(self, state):
        """阻挡节点的附加帧数按真实处理代价估算（会计入 ETA）。"""
        def penalty(nid):
            p = 0
            if state.enemy_guard(nid):
                p += GUARD_PENALTY
            if state.has_obstacle(nid):
                p += OBSTACLE_PENALTY
            return p
        return penalty

    @staticmethod
    def _has_our_scout_mark(state, node_id):
        for m in state.node(node_id).get("scouted") or []:
            if m.get("teamId") == state.my_team and m.get("remainingTriggers", 1) > 0:
                return True
        return False

    def _opp_eta(self, state, node_id):
        opp = state.opp
        opp_node = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_node:
            return math.inf
        f, path = state.graph.shortest_path(opp_node, node_id)
        return f if path else math.inf

    @staticmethod
    def _opp_processing_task(state, task):
        proc = state.opp.get("currentProcess") or {}
        return proc.get("taskId") == task.get("taskId")

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
