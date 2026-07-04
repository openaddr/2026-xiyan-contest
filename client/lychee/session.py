"""StrategySession：在官方 lychee_basic_client.ClientSession 基础上接入策略。

官方包原样引入不做修改；本类覆盖消息处理钩子与读循环：
- start   -> 解析进 GameState，通知策略，回 ready（沿用官方消息构造器）；
- inquire -> 策略决策，回带 actions 的 action 包（决策异常时退化为空动作心跳）；
- over    -> 记录最终结算明细后正常退出；
- error   -> 记录后继续等下一帧（官方基础实现是直接退出；按协议第 11 章，
             error 只表示该包未进入结算，不应终止对局）；
- run     -> 宽容读帧（V3.16.1）：平台序列化缺陷会产出非法 JSON（replay61
             r503 实测：验核绑破关令 + 同帧形成 GATE 窗口时，
             breakOrderCostTypes 被序列化成 {2744:"GOOD_FRUIT"}——玩家 ID
             作整数键不带引号），官方 read_frame 的 json.loads 直接抛异常
             杀死读循环 → 连续缺 60 帧动作被强制退赛（0 分）。修复分两层：
             按 JSON 规范给裸整数键补引号重试；仍失败则跳帧回空心跳，
             读循环在任何情况下不许死。
"""
import json
import re
import time

from lychee_basic_client.config import Config
from lychee_basic_client.framing import MAX_BODY, read_exact, write_frame
from lychee_basic_client.messages import heartbeat_action, ready_message
from lychee_basic_client.session import ClientSession

from .log import get_logger
from .state import GameState
from .strategy import PlannerStrategy

# 非法 JSON 修复：对象里的裸整数键补引号（{2744:"x"} → {"2744":"x"}）
_INT_KEY_RE = re.compile(r'([{,]\s*)(\d+)(\s*:)')
_ROUND_RE = re.compile(r'"round"\s*:\s*(\d+)')


class FrameDecodeError(ValueError):
    """帧体连修复后都无法解析；携带原文供跳帧兜底抠 round 号。"""

    def __init__(self, body):
        super().__init__("undecodable frame body")
        self.body = body


def lenient_loads(body):
    """json.loads 的宽容版：失败时按规范给裸整数键补引号再试一次。"""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        try:
            return json.loads(_INT_KEY_RE.sub(r'\1"\2"\3', body))
        except json.JSONDecodeError:
            raise FrameDecodeError(body) from None


def read_frame_lenient(sock):
    """官方 framing.read_frame 的宽容版（分帧逻辑一致，仅解析换 lenient_loads）。"""
    prefix = read_exact(sock, 5)
    try:
        length = int(prefix.decode("ascii"))
    except ValueError as exc:
        raise ValueError(f"invalid frame prefix: {prefix!r}") from exc
    if length < 0 or length > MAX_BODY:
        raise ValueError(f"invalid frame length: {length}")
    return lenient_loads(read_exact(sock, length).decode("utf-8"))


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

    # ---------- 覆盖读循环：宽容读帧 + 跳帧兜底（replay61 强制退赛复盘） ----------

    def run(self):
        self._send_registration()
        while True:
            try:
                message = read_frame_lenient(self._sock)
            except EOFError:
                self.log.info("connection closed")
                return 0
            except FrameDecodeError as e:
                # 补引号都救不回来的帧：跳过，正则抠 round 回空心跳保命——
                # 缺 1 帧动作无伤大雅，连续缺 60 帧就是强制退赛
                m = _ROUND_RE.search(e.body)
                if m:
                    self._safe_heartbeat(int(m.group(1)))
                self.log.error("undecodable frame skipped (round=%s): %.200s",
                               m.group(1) if m else "?", e.body)
                continue
            except (ConnectionError, OSError, ValueError) as e:
                # 传输层死亡（RST/前缀失步）：成帧无法恢复，干净落地
                # 而不是裸 traceback 崩溃（V3.28 加固审计第 2 条）
                self.log.error("transport failure, session ends: %r", e)
                return 1
            # 保命网（V3.28 加固审计第 1 条）：帧是合法 JSON 但结构出乎
            # 意料（字段缺失/为 null——平台有序列化缺陷前科 replay61），
            # state 硬取字段抛 KeyError 曾能一路穿透杀进程 → 连续缺 60 帧
            # 强制退赛 0 分。处理失败 = 跳帧 + 心跳，读循环在任何情况下
            # 不许死；state 按帧全量重建，脏帧不会污染下一帧
            try:
                result = self._handle_message(message)
            except Exception:
                rnd = (message.get("msg_data") or {}).get("round") \
                    or self.state.round or 1
                self.log.exception(
                    "handler failed (msg=%s round=%s), frame skipped",
                    message.get("msg_name"), rnd)
                self._safe_heartbeat(rnd)
                continue
            if result is not None:
                return result

    def _safe_heartbeat(self, round_no):
        """兜底心跳：socket 已死时不再二次崩溃（读循环自会发现 EOF）。"""
        try:
            write_frame(self._sock, heartbeat_action(
                self._match_id, round_no, self._config.player_id))
        except (ConnectionError, OSError) as e:
            self.log.error("heartbeat write failed: %r", e)

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
        try:
            self.strategy.on_start(self.state)
        except Exception:
            # ready 必须发出去：strategy 初始化钩子失败不构成弃赛理由
            self.log.exception("strategy.on_start failed; continuing")
        write_frame(self._sock, ready_message(
            self.state.match_id, data.get("round", 1), self._config.player_id))
        self.log.info("match %s started, team=%s opp=%s",
                      self.state.match_id, self.state.my_team, self.state.opp_id)

    # ---------- inquire ----------

    def _handle_inquire(self, data):
        t0 = time.monotonic()
        # round 兜底先于一切取好：state 更新失败时心跳也要带可用帧号
        round_no = data.get("round") or (self.state.round or 0) + 1
        try:
            self.state.on_inquire(data)
            round_no = self.state.round
            actions = self.strategy.decide(self.state) or []
        except Exception:
            # state/decide 任何一层失败都退化为空动作心跳（V3.28：
            # 曾只护 decide，state.on_inquire 的 KeyError 直接穿透杀进程）
            self.log.exception("state/decide failed at round %s", round_no)
            actions = []
        write_frame(self._sock, action_message(
            self._match_id, round_no, self._config.player_id, actions))

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
