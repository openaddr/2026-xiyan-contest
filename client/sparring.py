#!/usr/bin/env python3
"""风格化陪练（V3.18 基建）：给离线竞技场提供非镜像的对抗形态。

镜像自博弈测不出反蹲点/反设卡参数（首轮扫描 1248 局里 margin 恰好 0.0），
这两个脚本对手复刻语料里的两类真实威胁形态：

- CamperBot：2614 (L4-contest-contr) 式走廊控制流——第一时间抢武关，
  清障铺路，设满防卡，蹲在卡后农任务，卡被拆原地补，尾段回手 S11 second
  guard 再去交付（replay36 §4 的六步套路）。
- RusherBot：零贪婪竞速流——最短路直冲宫门，顺手骑马，只捡零绕路的冰，
  到宫门蹲 RUSH，验核即交付。测我们的竞速模式与漏斗定价。

陪练是脚本不是 AI：行为确定、可预测，用于回归和参数绑定测试，
不追求强度。出牌一律弃权（2614 实测风格）。
"""
import random

from lychee import protocol as P
from lychee.strategy import BaselineStrategy


class ScriptedBot(BaselineStrategy):
    """公共骨架：固定处理站/验核/交付/清障的通用处理。"""

    def decide(self, state):
        self._absorb_feedback(state)
        me = state.me
        if not me or me.get("delivered") or me.get("retired"):
            return []
        if me.get("state") in P.BUSY_STATES:
            return []
        acts = []
        main = self.bot_action(state)
        if main:
            acts.append(main)
        squad = self.bot_squad(state)
        if squad:
            acts.append(squad)
        return acts

    def bot_action(self, state):
        return P.a_wait()

    def bot_squad(self, state):
        return None

    # ---- 通用助手 ----

    def _walk_to(self, state, target):
        """朝 target 走一步；处理固定处理站、障碍、敌卡（简单攻坚/等待）。"""
        me = state.me
        cur = me.get("currentNodeId")
        if me.get("routeEdgeId"):
            return None                     # 在边上，让系统推进
        node = state.node(cur)
        needs = (node.get("processType") and node.get("processType") != "VERIFY"
                 and node.get("processRound", 0) > 0)
        if needs and not self._processed_here:
            proc = (state.opp.get("currentProcess") or {})
            if proc.get("targetNodeId") == cur:
                return P.a_wait()           # 排队
            return P.a_process()
        if cur == target:
            return None
        nxt = state.graph.next_hop(cur, target, state.my_speed())
        if nxt is None:
            return P.a_wait()
        if state.has_obstacle(nxt):
            if me.get("goodFruit", 0) > 1:
                return P.a_clear(nxt)
            return P.a_wait()
        g = state.enemy_guard(nxt)
        if g:
            good, bad = me.get("goodFruit", 0), me.get("badFruit", 0)
            need = g.get("defense", 0)
            gf = min(2, max(0, good - 3))
            bf = min(2, bad)
            if gf * 2 + bf * 3 >= need:
                return P.a_break_guard(nxt, gf, bf)
            return P.a_wait()               # 破不动就等风化（脚本不强通）
        return P.a_move(nxt)

    def _claim_here(self, state, wanted):
        """脚下有想要的资源就领一个。"""
        me = state.me
        stock = state.node(me.get("currentNodeId")).get("resourceStock") or {}
        res = me.get("resources") or {}
        for rt in wanted:
            if stock.get(rt, 0) > 0 and res.get(rt, 0) < 1:
                return P.a_claim_resource(me["currentNodeId"], rt)
        return None


class CamperBot(ScriptedBot):
    """走廊控制流：抢武关 → 设卡 → 蹲点农任务 → 补卡 → 尾段交付。

    V3.20 起按 match_id 种子抖动关键参数（RANDOMIZE=False 恢复确定性）：
    确定性脚本只有 ~3 条轨迹类，参数扫描里大量反蹲点参数因此不绑定。
    抖动维度对应语料里的真实分布：蹲点位（2614 蹲武关、2839 蹲潼关）、
    离开帧（实测 r340~460）、起卡延迟（"到点即起卡" vs "先农一会儿再
    起卡"的坐地户——后者是对手画像分类器的收益窗口）。
    """

    CAMP_NODE = "S10"
    SECOND_GUARD = "S11"
    LEAVE_ROUND = 400          # 尾段动身（2614 实测 r340~460 间离开）
    TASK_CAP = 180
    GUARD_DELAY = 0            # 到达蹲点后先闲置/农任务 N 帧再起第一张卡
    RANDOMIZE = True           # 按 match_id 派生种子，按种子可复现

    def _setup(self, state):
        if getattr(self, "_cfg_done", False):
            return
        self._cfg_done = True
        self._arrived = None   # 到达蹲点的帧（GUARD_DELAY 计时起点）
        if not self.RANDOMIZE:
            return
        rng = random.Random(f"{state.match_id}:camperbot")
        self.CAMP_NODE, self.SECOND_GUARD = rng.choice(
            (("S10", "S11"), ("S10", "S11"), ("S11", "S10")))
        self.LEAVE_ROUND = rng.randrange(340, 461)
        self.TASK_CAP = rng.randrange(150, 201)
        self.GUARD_DELAY = rng.choice((0, 0, 25, 60, 120))

    def bot_action(self, state):
        self._setup(state)
        me = state.me
        if me.get("routeEdgeId"):
            return None                 # 边上让系统推进（WAIT 会暂停移动）
        cur = me.get("currentNodeId")
        rnd = state.round
        camp = self.CAMP_NODE

        # 交付线
        if me.get("verified"):
            step = self._walk_to(state, state.terminal_node)
            if step:
                return step
            if cur == state.terminal_node:
                return P.a_deliver()
            return P.a_wait()

        leaving = rnd >= self.LEAVE_ROUND \
            or (me.get("taskScore", 0) or 0) >= self.TASK_CAP

        if not leaving and cur != camp:
            claim = self._claim_here(state, (P.ICE_BOX,))
            if claim:
                return claim
            return self._walk_to(state, camp) or P.a_wait()

        if not leaving and cur == camp:
            if self._arrived is None:
                self._arrived = rnd
            # 1) 卡没了/防守清零 → 原地补卡（蹲点补卡循环）。
            #    首卡受 GUARD_DELAY 抖动（坐地户形态）；补卡不受
            guard_ready = (rnd - self._arrived >= self.GUARD_DELAY
                           or getattr(self, "_guard_placed", False))
            node = state.node(camp)
            g = node.get("guard")
            active = bool(g and g.get("ownerTeamId") == state.my_team
                          and g.get("active", g.get("defense", 0) > 0))
            if guard_ready and not active \
                    and not (g and g.get("ownerTeamId")
                             and g.get("ownerTeamId") != state.my_team) \
                    and me.get("goodFruit", 0) >= 4:
                self._guard_placed = True
                return P.a_set_guard(camp, 2)
            # 2) 农任务：脚下有活跃任务就做
            for t in state.claimable_tasks():
                if t.get("nodeId") == camp \
                        and t.get("taskTemplateId") != "T04":
                    return P.a_claim_task(t["taskId"])
            return P.a_wait()

        # 尾段：路过 S11 顺手第二张卡，然后宫门验核
        if cur == self.SECOND_GUARD and me.get("goodFruit", 0) >= 3:
            node = state.node(cur)
            g = node.get("guard")
            if not (g and g.get("ownerTeamId")):
                return P.a_set_guard(cur, 2)
        if cur == state.gate_node:
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()
        return self._walk_to(state, state.gate_node) or P.a_wait()

    def bot_squad(self, state):
        # 蹲点期顺手远程清掉 S11 障碍（2614 r260 同款铺路）
        me = state.me
        if state.phase == P.PHASE_RUSH:
            return None
        if me.get("currentNodeId") == self.CAMP_NODE \
                and state.has_obstacle(self.SECOND_GUARD) \
                and (me.get("squadAvailable") or 0) >= 2 \
                and not getattr(self, "_cleared_s11", False):
            self._cleared_s11 = True
            return P.a_squad_clear(self.SECOND_GUARD)
        return None


class RusherBot(ScriptedBot):
    """零贪婪竞速流：直冲宫门，顺手骑马，零绕路捡冰。"""

    def bot_action(self, state):
        me = state.me
        cur = me.get("currentNodeId")

        if me.get("routeEdgeId"):
            res = me.get("resources") or {}
            if not state.has_move_buff():
                for h in (P.FAST_HORSE, P.SHORT_HORSE):
                    if res.get(h, 0) > 0:
                        return P.a_use_resource(h)
            return None

        if me.get("verified"):
            step = self._walk_to(state, state.terminal_node)
            if step:
                return step
            if cur == state.terminal_node:
                return P.a_deliver()
            return P.a_wait()

        # 零绕路顺手：冰和马（都在冲刺路径的资源点上）
        claim = self._claim_here(state, (P.ICE_BOX, P.FAST_HORSE,
                                         P.SHORT_HORSE))
        if claim:
            return claim
        res = me.get("resources") or {}
        if me.get("freshness", 100) < 88 and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)

        if cur == state.gate_node:
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()
        return self._walk_to(state, state.gate_node) or P.a_wait()


class FarmerBot(ScriptedBot):
    """农任务型：巡回抢任务刷分，走廊控制零投入，尾段才动身交付。

    replay36 对手（首卡 r314 的"先农后卡"）的极端化形态，也是主动设卡
    收益窗口的假设对手——它注定落后走廊竞速、必须晚过关隘。
    按 match_id 抖动：动身帧 / 任务分上限 / 是否顺路捡冰。
    """

    LEAVE_ROUND = 380          # 动身帧（S09 起步到交付含处理站 ~205 帧）
    TASK_CAP = 230
    DEADLINE_PAD = 100         # 验核 6 + 宫门→终点 + 处理站/攻坚余量
    PICK_ICE = True
    RANDOMIZE = True

    def _setup(self, state):
        if getattr(self, "_cfg_done", False):
            return
        self._cfg_done = True
        if not self.RANDOMIZE:
            return
        rng = random.Random(f"{state.match_id}:farmerbot")
        self.LEAVE_ROUND = rng.randrange(320, 431)
        self.TASK_CAP = rng.randrange(200, 261)
        self.PICK_ICE = rng.random() < 0.7

    def bot_action(self, state):
        self._setup(state)
        me = state.me
        if me.get("routeEdgeId"):
            return None
        cur = me.get("currentNodeId")

        if me.get("verified"):
            step = self._walk_to(state, state.terminal_node)
            if step:
                return step
            if cur == state.terminal_node:
                return P.a_deliver()
            return P.a_wait()

        # 动态死线：剩余帧不够 走到宫门+验核+走终点 就立刻动身
        # （首版实测 r380 后从 S05 起步必然未交付——脚本也不该自杀）
        eta_gate, pg = state.graph.shortest_path(
            cur, state.gate_node, state.my_speed())
        deadline = pg and state.round + eta_gate + self.DEADLINE_PAD >= 600
        leaving = deadline or state.round >= self.LEAVE_ROUND \
            or (me.get("taskScore", 0) or 0) >= self.TASK_CAP
        if leaving:
            res = me.get("resources") or {}
            if me.get("freshness", 100) < 88 and res.get(P.ICE_BOX, 0) > 0:
                return P.a_use_resource(P.ICE_BOX)
            if cur == state.gate_node:
                if state.phase == P.PHASE_RUSH:
                    return P.a_verify_gate()
                return P.a_wait()
            return self._walk_to(state, state.gate_node) or P.a_wait()

        # 农任务巡回：脚下有任务先做，否则走向最近的活跃任务节点
        if self.PICK_ICE:
            claim = self._claim_here(state, (P.ICE_BOX,))
            if claim:
                return claim
        res = me.get("resources") or {}
        has_horse = any(res.get(h, 0) > 0
                        for h in (P.FAST_HORSE, P.SHORT_HORSE))
        # T04 要在障碍点做；T06 领取要消耗一匹马（没马领取被拒，
        # 每帧重试会把自己钉死到任务过期——首版实测 S09 蹲 150 帧零收）
        tasks = [t for t in state.claimable_tasks()
                 if t.get("taskTemplateId") != "T04"
                 and (t.get("taskTemplateId") != "T06" or has_horse)]
        here = [t for t in tasks if t.get("nodeId") == cur]
        if here:
            return P.a_claim_task(here[0]["taskId"])
        best = None
        best_eta = float("inf")
        for t in tasks:
            eta, path = state.graph.shortest_path(
                cur, t["nodeId"], state.my_speed())
            if not path or eta >= best_eta:
                continue
            # 只追赶得上的：到点还得赶在过期前领上（首版实测追了
            # 114 帧奔 S05，到点任务已过期，交付窗口也搭进去了）
            expire = t.get("expireRound") or 0
            if expire and state.round + eta + 6 > expire:
                continue
            # 且做完还得赶得上交付：长边一旦踏上就是 80+ 帧的承诺，
            # 不做"回不了家"的远征（seed5 实测 S07→S04 一步走进死局）
            back, pb = state.graph.shortest_path(
                t["nodeId"], state.gate_node, state.my_speed())
            if not pb or state.round + eta + back + self.DEADLINE_PAD > 600:
                continue
            best_eta, best = eta, t["nodeId"]
        if best:
            return self._walk_to(state, best) or P.a_wait()
        return P.a_wait()


class TollerBot(ScriptedBot):
    """第一名 2839 (lychee-py) 形态复刻（语料：vs2839-firstplace-2026-07-03.md）。

    边冲边农 + 汇入点自适应回手卡 + RUSH 起点免费二卡：
    - 官道直冲，途经节点顺路做任务（农不停步，实测它 r181 离 S07 时 90 分）；
    - 走廊节点（S09/S10/S11）上，若对手尚未过此点且正在逼近/已踏上来边，
      离开时回手一张满防卡（不限咽喉——S09 普通驿站掐踏边是它的招牌）；
    - r448+ 在 S13 宫前驿落第二张免费卡；
    - 直奔交付（它四局 r497±1 交付 ~743）。
    确定性复刻用于修复对照；RANDOMIZE 抖动起卡阈值/农任务上限做泛化测试。
    """

    GUARD_NODES = ("S09", "S10", "S11")
    GUARD_OPP_ETA = 150        # 对手到本节点 ETA 在此内才值得回手卡
    SECOND_GUARD_NODE = "S13"
    SECOND_GUARD_ROUND = 448
    TASK_CAP = 150
    RANDOMIZE = True

    def _setup(self, state):
        if getattr(self, "_cfg_done", False):
            return
        self._cfg_done = True
        self._guards_used = 0
        if not self.RANDOMIZE:
            return
        rng = random.Random(f"{state.match_id}:tollerbot")
        self.GUARD_OPP_ETA = rng.choice((110, 150, 150, 190))
        self.TASK_CAP = rng.choice((120, 150, 150, 180))
        self.SECOND_GUARD_ROUND = rng.randrange(440, 461)

    def _opp_eta_to(self, state, node_id):
        opp = state.opp
        if not opp:
            return float("inf")
        pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not pos:
            return float("inf")
        eta, path = state.graph.shortest_path(pos, node_id, P.BASE_SPEED)
        return eta if path else float("inf")

    def _rear_guard_here(self, state, cur):
        """离开走廊节点前的回手卡判断（含掐踏边狙击）。"""
        me, opp = state.me, state.opp
        if cur not in self.GUARD_NODES or self._guards_used >= 2:
            return None
        if not opp or opp.get("delivered") or opp.get("retired"):
            return None
        node = state.node(cur)
        g = node.get("guard")
        if g and g.get("ownerTeamId"):
            return None
        if me.get("goodFruit", 0) < 5:
            return None
        # 对手已过此点（在我们身后的意义上）就不用卡：看它到宫门的路
        # 是否还要经过这里
        opp_pos = opp.get("nextNodeId") or opp.get("currentNodeId")
        if not opp_pos:
            return None
        _, p_gate = state.graph.shortest_path(opp_pos, state.gate_node,
                                              P.BASE_SPEED)
        if cur not in p_gate:
            return None
        # 掐踏边：对手已踏上通往这里的边 → 立即落卡（读条 4 帧 < 边剩余）
        snipe = (opp.get("currentNodeId") is None
                 and opp.get("nextNodeId") == cur)
        if snipe or self._opp_eta_to(state, cur) <= self.GUARD_OPP_ETA:
            self._guards_used += 1
            return P.a_set_guard(cur, 2)
        return None

    def bot_action(self, state):
        self._setup(state)
        me = state.me
        if me.get("routeEdgeId"):
            return None
        cur = me.get("currentNodeId")

        if me.get("verified"):
            step = self._walk_to(state, state.terminal_node)
            if step:
                return step
            if cur == state.terminal_node:
                return P.a_deliver()
            return P.a_wait()

        # 鲜度维护（它四局交付鲜度 82 上下）
        res = me.get("resources") or {}
        if me.get("freshness", 100) < 88 and res.get(P.ICE_BOX, 0) > 0:
            return P.a_use_resource(P.ICE_BOX)

        # RUSH 起点的 S13 二卡
        if cur == self.SECOND_GUARD_NODE \
                and state.round >= self.SECOND_GUARD_ROUND \
                and self._guards_used < 2 \
                and me.get("goodFruit", 0) >= 3:
            node = state.node(cur)
            g = node.get("guard")
            if not (g and g.get("ownerTeamId")):
                self._guards_used += 1
                return P.a_set_guard(cur, 2)

        # 走廊节点回手卡（含掐踏边）
        guard = self._rear_guard_here(state, cur)
        if guard:
            return guard

        # 边冲边农：脚下有任务就做（不专程绕路——农不停步的核心）
        if (me.get("taskScore", 0) or 0) < self.TASK_CAP:
            has_horse = any(res.get(h, 0) > 0
                            for h in (P.FAST_HORSE, P.SHORT_HORSE))
            for t in state.claimable_tasks():
                if t.get("nodeId") != cur:
                    continue
                tpl = t.get("taskTemplateId")
                if tpl == "T04" or (tpl == "T06" and not has_horse):
                    continue
                return P.a_claim_task(t["taskId"])
        claim = self._claim_here(state, (P.ICE_BOX,))
        if claim:
            return claim

        if cur == state.gate_node:
            if state.phase == P.PHASE_RUSH:
                return P.a_verify_gate()
            return P.a_wait()
        return self._walk_to(state, state.gate_node) or P.a_wait()


BOTS = {"camper": CamperBot, "rusher": RusherBot, "farmer": FarmerBot,
        "toller": TollerBot}
