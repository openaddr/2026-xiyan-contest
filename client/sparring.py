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


BOTS = {"camper": CamperBot, "rusher": RusherBot}
