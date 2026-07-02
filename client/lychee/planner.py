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

# 对手阴影惩罚（V3.1 走廊竞争）：对手会先于我们到达的节点，资源会被扫空、
# 设卡会掐点出现（四局回放实锤：落后 60~80 帧走同一走廊 = 全程吃陷阱）。
SHADOW_CHOKE_PENALTY = 35   # 咽喉类节点（对手大概率回手设卡）
SHADOW_NODE_PENALTY = 8     # 普通节点（资源被扫、任务被先手）
SHADOW_MARGIN = 5           # 到达时间差超过该值才算被抢先
# 阴影咽喉不含 GATE：宫门验核双方都要排队，实战对手从未卡宫门；
# 尾段(S11-S14)双走廊共用，过重的阴影只会虚压 slack 不改变路线
CHOKE_TYPES = {"KEY_PASS", "PASS", "MOUNTAIN_PASS"}
FORECAST_HORIZON = 120      # 预告天气只影响这个时间窗内的路段估价

# 资源提货目标价值（V3.2）：冰鉴 +10 鲜度 = 18 分，按 17 计（贴 100 上限有损耗）。
# 败局实锤：3 个冰鉴全在陆路，水路竞速交付快 25 帧却输 27 分鲜度。
RESOURCE_VALUES = {P.ICE_BOX: 17.0, P.FAST_HORSE: 4.0, P.SHORT_HORSE: 3.0}
CLAIM_FRAMES = 2            # 资源领取读条
# 对手到资源点的竞争折扣
SWEEP_DISCOUNT = 0.15       # 对手明显先到：大概率被扫空
CONTEST_DISCOUNT = 0.55     # 五五开：窗口期间双方同冻结，时间近乎免费

# V3.3 鲜度竞赛：
# 拒止倍率——资源在对手前进路线上时，抢到 = 我 +17 且对手 -18（双向摇摆）
DENIAL_FACTOR = 1.5
# 链式加成——目标点通往宫门路上的其他资源按半权计（赢下 S03 后 S07 顺路白拿）
CHAIN_WEIGHT = 0.5
# 不在对手路线上的资源，竞争折扣下限（对手专程绕过来的概率低）
OFFPATH_RACE_FLOOR = 0.7
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
        self.blacklist = {}   # taskId -> 解禁帧（吃到拒绝后临时拉黑）
        self._shadow_cache = (-1, frozenset())  # (round, 被对手抢先的节点集)
        self._opp_path_cache = (-1, frozenset())  # (round, 对手前进路线节点集)
        # 回头迟滞（V3.8）：刚离开的节点在窗口期内作为目标首跳要付额外代价。
        # replay25：走廊总价近似打平让 65 帧真实折返在绕路公式里"免费"，
        # S03→S02→S04 的回头使我们晚 70 帧到 S09，正好撞上对手设卡循环。
        self.back_node = None
        self.back_until = -1

    # ================= 对外入口 =================

    def plan(self, state):
        me = state.me
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
                                slack, speed, penalty, ecost)
            if ev and ev[0] > best_net:
                best_net, best = ev[0], ("task", t, ev[1], None)

        # 资源提货目标与任务同台竞价（冰鉴 17 分 vs 任务 45~99 分 vs 绕路成本）
        for node_id, rtype, net in self._resource_targets(
                state, cur, to_gate, slack, speed, penalty, ecost):
            if net > best_net:
                best_net, best = net, ("resource", None, node_id, rtype)

        if best:
            kind, t, pos, rtype = best
            return Plan(kind, t, pos, slack=slack, resource=rtype,
                        detail=f"net={best_net:.0f} base={base}")
        return Plan("deliver", detail=f"no worthy task, base={base}", slack=slack)

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
                d = max(d, OFFPATH_RACE_FLOOR)
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
            v = value * race_discount(pos, my_raw.get(pos, 0))
            if pos in opp_path:
                v *= DENIAL_FACTOR
            bonus += v
            frames += CLAIM_FRAMES
        node_raw = state.graph.all_frames(pos) if onward_path else {}
        for nb in set(onward_path or ()) - {pos}:
            nb_node = state.nodes.get(nb) or {}
            for rtype, value in stock_claimables(nb_node):
                eta = my_raw.get(pos, 0) + frames + node_raw.get(nb, 0)
                v = CHAIN_WEIGHT * value * race_discount(nb, eta)
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
                f_to += self._backtrack_tax(state, cur, node_id)
                f_back, back = g.shortest_path(node_id, state.gate_node, speed,
                                               penalty, ecost)
                if not back:
                    continue
                detour = max(0, f_to + f_back - to_gate) + CLAIM_FRAMES
                if detour > slack:
                    continue
                v = value * race_discount(node_id, my_raw.get(node_id, 0))
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
                        chain += CHAIN_WEIGHT * val2 * d2
                net = v + chain - detour * self._frame_value(state, to_gate)
                if net > 0:
                    out.append((node_id, rtype, net))
        return out

    # ================= 估值 =================

    def _evaluate(self, state, task, cur, base, to_gate, eta_direct,
                  slack, speed, penalty, ecost=None):
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
        pos, f_to = self._position_for(state, task, cur, speed, penalty, ecost)
        if pos is None:
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

        # 资源捆绑（V3.6）：任务点及其通往宫门沿途的可领资源计入任务估值。
        # replay21/22 教训：T01@S03 捆着官道双冰、T08@S04 只捆一匹马，
        # 单任务净值 argmax 把 30+ 分的鲜度捆绑包挤出局。
        bundle, bframes = self._resource_bundle(state, pos, back_path, cur)
        if total_frames + bframes > slack:
            bundle, bframes = 0.0, 0  # 余量装不下捆绑就只按裸任务估

        cost = (total_frames + bframes) * self._frame_value(state, eta_direct)
        net = value + bundle - cost
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

        def penalty(nid):
            p = time_penalty(nid)
            if nid in shadow:
                node = state.node(nid)
                p += (SHADOW_CHOKE_PENALTY
                      if node.get("nodeType") in CHOKE_TYPES
                      else SHADOW_NODE_PENALTY)
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
        active_types = {w.get("type") for w in (state.weather or {}).get("active") or []}

        def edge_cost(edge, base_frames):
            rt = edge.get("routeType")
            decay = P.ROUTE_FRESH_DECAY.get(rt, P.IDLE_FRESH_DECAY)
            region = 1.0
            for wt in active_types:
                region = max(region, WEATHER_FRESH_REGION.get((wt, rt), 1.0))
            scale = 1.5 if P.HOT in active_types else 1.0
            mult = 1.0 + (decay * region - P.IDLE_FRESH_DECAY) * scale * 1.8 / _FV
            return time_cost(edge, base_frames) * mult
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
