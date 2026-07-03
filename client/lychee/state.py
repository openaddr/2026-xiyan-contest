"""对局状态：缓存 start 静态信息，每帧吸收 inquire 公开状态。

读取口径（通信协议第 5/7 章）：
- 静态地图优先读顶层 msg_data.nodes[] / edges[]，语义点读 map.gameplay.roles；
- 运行时 edges 优先用 inquire.edges[]，缺失回退 start.edges[]；
- 动作结果结合 events[] + actionResults[] + 下一帧状态判断。
"""
from . import protocol as P
from .world import MapGraph


class GameState:
    def __init__(self, player_id):
        self.player_id = int(player_id)

        # ---- start 静态信息 ----
        self.match_id = None
        self.duration_round = 600
        self.my_team = None          # "RED" / "BLUE"
        self.opp_id = None
        self.roles = {}              # startNodeId / gateNodeId / terminalNodeIds / ...
        self.static_nodes = {}       # nodeId -> node
        self.resource_config = []    # 资源投放配置
        self.task_templates = {}     # templateId -> template
        self.task_candidates = {}    # templateId -> 候选站点列表
        self.graph = None            # MapGraph

        # ---- 每帧 inquire ----
        self.round = 0
        self.phase = P.PHASE_NORMAL
        self.players = {}            # playerId -> player
        self.nodes = {}              # nodeId -> node（运行时状态）
        self.tasks = []
        self.bounties = []
        self.contests = []
        self.events = []
        self.action_results = []
        self.weather = {"active": [], "forecast": []}
        self.score_preview = {}

    # ================= 消息入口 =================

    def on_start(self, d):
        self.match_id = d["matchId"]
        self.duration_round = d.get("durationRound", 600)

        for p in d.get("players", []):
            if p["playerId"] == self.player_id:
                self.my_team = p.get("teamId")
            else:
                self.opp_id = p["playerId"]

        game_play = (d.get("map") or {}).get("gameplay") or {}
        self.roles = game_play.get("roles") or {}

        nodes = d.get("nodes") or (d.get("map") or {}).get("nodes") or []
        self.static_nodes = {n["nodeId"]: n for n in nodes}

        edges = d.get("edges") or (d.get("map") or {}).get("edges") or []
        self.graph = MapGraph(edges)

        # 资源/任务模板：顶层为空时回退 map.gameplay
        self.resource_config = d.get("resources") or game_play.get("resources") or []
        self.task_templates = {
            t["taskTemplateId"]: t for t in d.get("taskTemplates") or []
        }
        # 任务候选点（模板 ID -> 站点列表）：判断本图会刷哪些任务模板
        self.task_candidates = game_play.get("taskCandidates") or {}

    def on_inquire(self, d):
        self.round = d["round"]
        self.phase = d.get("phase", P.PHASE_NORMAL)
        self.players = {p["playerId"]: p for p in d.get("players", [])}
        self.nodes = {n["nodeId"]: n for n in d.get("nodes", [])}
        self.tasks = d.get("tasks") or []
        self.bounties = d.get("bounties") or []
        self.contests = d.get("contests") or []
        self.events = d.get("events") or []
        self.action_results = d.get("actionResults") or []
        self.weather = d.get("weather") or {"active": [], "forecast": []}
        self.score_preview = d.get("scorePreview") or {}
        edges = d.get("edges")
        if edges:  # 每帧同步的路线边优先
            self.graph = MapGraph(edges)

    # ================= 便捷访问 =================

    @property
    def me(self):
        return self.players.get(self.player_id, {})

    @property
    def opp(self):
        return self.players.get(self.opp_id, {})

    @property
    def start_node(self):
        return self.roles.get("startNodeId", "S01")

    @property
    def gate_node(self):
        return self.roles.get("gateNodeId", "S14")

    @property
    def terminal_node(self):
        ids = self.roles.get("terminalNodeIds") or ["S15"]
        return ids[0]

    def node(self, node_id):
        return self.nodes.get(node_id) or self.static_nodes.get(node_id) or {}

    def my_speed(self):
        """当前基础每帧移动量（考虑马类 / 疾行令增益）。"""
        speed = P.BASE_SPEED
        for b in self.me.get("buffs") or []:
            t = b.get("type")
            if t == P.FAST_HORSE:
                speed = max(speed, P.SPEED_FAST_HORSE)
            elif t == P.SHORT_HORSE:
                speed = max(speed, P.SPEED_SHORT_HORSE)
            elif t == P.RUSH_SPEED:
                speed = max(speed, P.SPEED_RUSH)
        return speed

    def has_move_buff(self):
        return any((b.get("type") in (P.FAST_HORSE, P.SHORT_HORSE, P.RUSH_SPEED))
                   for b in self.me.get("buffs") or [])

    # ---- 节点阻挡判断 ----

    def enemy_guard(self, node_id):
        """目标节点上敌方有效设卡；无则返回 None。"""
        g = self.node(node_id).get("guard")
        if not g:
            return None
        # active 是服务端计算字段，缺失时按口径回退: ownerTeamId != null && defense > 0
        active = g.get("active", bool(g.get("ownerTeamId")) and g.get("defense", 0) > 0)
        if active and g.get("ownerTeamId") and g["ownerTeamId"] != self.my_team:
            return g
        return None

    def has_obstacle(self, node_id):
        return bool(self.node(node_id).get("hasObstacle"))

    def is_blocked(self, node_id):
        """普通移动会被挡：敌方有效设卡 或 道路障碍。"""
        return self.enemy_guard(node_id) is not None or self.has_obstacle(node_id)

    # ---- 窗口 ----

    def my_open_contests(self):
        """本队正在参与、尚未结算、非抑制记录的窗口。"""
        out = []
        for c in self.contests:
            if c.get("resolved"):
                continue
            if c.get("status") == "SUPPRESSED":
                continue
            if self.player_id in (c.get("redPlayerId"), c.get("bluePlayerId")):
                out.append(c)
        return out

    # ---- 上一帧反馈 ----

    def my_events(self, *types):
        """上一帧与本队相关的事件（types 为空则不过滤类型）。"""
        out = []
        for e in self.events:
            if types and e.get("type") not in types:
                continue
            p = e.get("payload") or {}
            pid = p.get("playerId")
            if pid is None or pid == self.player_id:
                out.append(e)
        return out

    def my_rejections(self):
        """上一帧本队被拒绝/非法的动作: [(action, errorCode)]"""
        out = []
        for e in self.my_events("ACTION_REJECTED", "INVALID_ACTION"):
            p = e.get("payload") or {}
            out.append((p.get("action"), p.get("errorCode")))
        return out

    # ---- 悬赏与总分（追分判定，任务书 6.3.3） ----

    def is_behind(self):
        """本结算帧开始时，公开总分是否落后对手（破关悬赏结算的追分口径）。"""
        opp = self.opp
        if not opp:
            return False
        return (self.me.get("totalScore", 0) or 0) < (opp.get("totalScore", 0) or 0)

    def bounty_for_node(self, node_id):
        """目标节点当前生效（未完成、未过期）的悬赏；无则 None。"""
        for b in self.bounties:
            if b.get("nodeId") == node_id and b.get("active") and not b.get("completed"):
                return b
        return None

    def enemy_bounties(self):
        """挂在敌方仍然有效设卡上的悬赏：只有这类悬赏才轮到我方攻破拿分。

        以 enemy_guard() 现场结果为准，不单独信任 bounty.ownerTeamId ——
        悬赏失效跟着设卡失效走（6.3.3），设卡状态才是唯一真值。
        """
        out = []
        for b in self.bounties:
            if not b.get("active") or b.get("completed"):
                continue
            if self.enemy_guard(b.get("nodeId")):
                out.append(b)
        return out

    # ---- 任务 ----

    def claimable_tasks(self):
        """当前活跃、未完成、未被对方占用/保护的任务实例。"""
        out = []
        for t in self.tasks:
            if not t.get("active") or t.get("completed") or t.get("failed"):
                continue
            owner = t.get("ownerPlayerId") or 0
            if owner not in (0, self.player_id):
                continue
            prot = t.get("protectionPlayerId") or 0
            if prot not in (0, self.player_id):
                continue
            out.append(t)
        return out
