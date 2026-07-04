#!/usr/bin/env python3
"""全形态回归矩阵（V3.20 起每轮收尾必跑）：python3 regress.py

镜像 12 局 / 确定性 camper 48 局 / 随机化 camper 48 局 / rusher 24 局，
输出各形态胜率、margin、未交付清单与画像命中分布。历史基线见
docs/opp-profile-2026-07-04.md 附表——数字回退即回归。
"""
import os
import sys
from collections import Counter
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arena import run_match, PID_A, PID_B          # noqa: E402
from sparring import CamperBot, RusherBot          # noqa: E402


class FixedCamper(CamperBot):
    RANDOMIZE = False


def game(spec):
    kind, seed, seat = spec
    if kind == "mirror":
        r = run_match(seed)
        return {"kind": kind, "seed": seed, "win": r["winner"] == PID_A,
                "draw": r["winner"] == 0, "margin": r["margin"],
                "undlv": (not r[PID_A]["delivered"])
                + (not r[PID_B]["delivered"]),
                "prof": (r[PID_A]["oppProfile"], r[PID_B]["oppProfile"])}
    bot = {"camper": CamperBot, "camper_fixed": FixedCamper,
           "rusher": RusherBot}[kind]
    if seat == "A":
        r = run_match(seed, cls_b=bot)
        us, them = PID_A, PID_B
    else:
        r = run_match(seed, cls_a=bot)
        us, them = PID_B, PID_A
    return {"kind": kind, "seed": seed, "win": r["winner"] == us,
            "draw": r["winner"] == 0,
            "margin": r[us]["score"] - r[them]["score"],
            "undlv": not r[us]["delivered"], "prof": r[us]["oppProfile"]}


def main():
    specs = ([("mirror", s, None) for s in range(12)]
             + [(k, s, seat) for k in ("camper", "camper_fixed")
                for s in range(24) for seat in ("A", "B")]
             + [("rusher", s, seat) for s in range(12)
                for seat in ("A", "B")])
    with Pool(10) as p:
        rows = p.map(game, specs)
    for kind in ("mirror", "camper_fixed", "camper", "rusher"):
        rs = [r for r in rows if r["kind"] == kind]
        n = len(rs)
        w = sum(r["win"] for r in rs)
        d = sum(r["draw"] for r in rs)
        m = sum(r["margin"] for r in rs) / n
        u = sum(r["undlv"] for r in rs)
        prof = Counter(str(r["prof"]) for r in rs)
        print(f"{kind:13}: n={n} win={w} draw={d} lose={n-w-d} "
              f"margin={m:+.1f} undlv={u}")
        print(f"{'':13}  画像 {dict(prof)}")
        bad = sorted((r["seed"], r["margin"]) for r in rs
                     if r["kind"] != "mirror" and r["undlv"])
        if bad:
            print(f"{'':13}  未交付 {bad}")


if __name__ == "__main__":
    main()
