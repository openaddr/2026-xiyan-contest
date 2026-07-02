"""StrategySession：在官方 lychee_basic_client.ClientSession 基础上接入策略。

官方包原样引入不做修改；本类只覆盖消息处理钩子：
- start   -> 解析进 GameState，通知策略，回 ready（沿用官方消息构造器）；
- inquire -> 策略决策，回带 actions 的 action 包（决策异常时退化为空动作心跳）；
- over    -> 记录最终结算明细后正常退出；
- error   -> 记录后继续等下一帧（官方基础实现是直接退出；按协议第 11 章，
             error 只表示该包未进入结算，不应终止对局）。
"""
import json
import time

from lychee_basic_client.config import Config
from lychee_basic_client.framing import write_frame
from lychee_basic_client.messages import ready_message
from lychee_basic_client.session import ClientSession

from .log import get_logger
from .state import GameState
from .strategy import PlannerStrategy


def action_message(match_id, round_no, player_id, actions):
    """带任意 actions 的 action 包（官方 messages 只有心跳和 MOVE）。"""
    return {
        "msg_name": "action",
        "msg_data": {
            "matchId": match_id,
            "round": round_no,
            "playerId": player_id,
            "actions": actions,
        },
    }


class StrategySession(ClientSession):
    def __init__(self, sock, config: Config, strategy=None, logger=None):
        super().__init__(sock, config)
        self.log = logger or get_logger(config.player_id)
        self.state = GameState(config.player_id)
        self.strategy = strategy or PlannerStrategy(self.log)

    # ---------- 覆盖消息分发：接管 over / error ----------

    def _handle_message(self, message):
        msg_name = message.get("msg_name")
        data = message.get("msg_data") or {}
        if msg_name == "over":
            self._handle_over(data)
            return 0
        if msg_name == "error":
            self.log.error("server error: %s", json.dumps(data, ensure_ascii=False))
            return None  # 该包未结算；继续等下一帧 inquire
        return super()._handle_message(message)

    # ---------- start ----------

    def _handle_start(self, data):
        self.state.on_start(data)
        self._match_id = self.state.match_id  # 官方基类字段，保持同步
        self.strategy.on_start(self.state)
        write_frame(self._sock, ready_message(
            self.state.match_id, data.get("round", 1), self._config.player_id))
        self.log.info("match %s started, team=%s opp=%s",
                      self.state.match_id, self.state.my_team, self.state.opp_id)

    # ---------- inquire ----------

    def _handle_inquire(self, data):
        t0 = time.monotonic()
        self.state.on_inquire(data)
        try:
            actions = self.strategy.decide(self.state) or []
        except Exception:
            self.log.exception("decide failed at round %d", self.state.round)
            actions = []  # 空动作心跳兜底，绝不能缺帧
        write_frame(self._sock, action_message(
            self.state.match_id, self.state.round, self._config.player_id, actions))

        cost_ms = (time.monotonic() - t0) * 1000
        me = self.state.me
        self.log.debug(
            "r%d/%s pos=%s st=%s fresh=%.1f good=%s score=%s -> %s (%.0fms)",
            self.state.round, self.state.phase, me.get("currentNodeId"),
            me.get("state"), me.get("freshness", 0) or 0, me.get("goodFruit"),
            me.get("totalScore"), json.dumps(actions, ensure_ascii=False), cost_ms)
        if cost_ms > 300:
            self.log.warning("slow decide: %.0fms at round %d", cost_ms, self.state.round)

    # ---------- over ----------

    def _handle_over(self, data):
        winner = data.get("winnerPlayerId")
        self.log.info("=== OVER round=%s type=%s reason=%s winner=%s %s===",
                      data.get("overRound"), data.get("resultType"),
                      data.get("overReason"), winner,
                      "(WE WIN) " if winner == self._config.player_id else "")
        for p in data.get("players") or []:
            self.log.info("  %s(%s): total=%s delivered=%s detail=%s",
                          p.get("playerName"), p.get("playerId"),
                          p.get("totalScore"), p.get("delivered"),
                          json.dumps(p.get("scoreDetail"), ensure_ascii=False))
