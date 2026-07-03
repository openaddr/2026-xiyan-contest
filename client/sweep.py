#!/usr/bin/env python3
"""参数敏感性扫描（V3.18 基建）：变体 vs 基线的镜像对战跑批。

设计：
- 每个 (参数, 扰动) 打在一侧、另一侧保持默认 → 直接得到"变体对基线"的
  胜率与分差；同一组种子分别把补丁打在 A 座和 B 座，座位效应对消。
- 同一组种子贯穿所有条件（配对比较），差异全部来自参数本身。
- 噪声底线由 baseline（双方默认）的 A 座胜率偏离 50% 的幅度给出。

用法：
    python3 sweep.py --baseline --seeds 24          # 噪声底线
    python3 sweep.py --seeds 24 --jobs 8            # 全参数扫描
    python3 sweep.py --param planner.RACE_FRAME_MULT --seeds 40   # 单参数加深
输出：stdout 表格 + sweep_results.json
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# (键, 默认值, 低扰动, 高扰动)——默认 ±30%；个别参数按语义定界
PARAMS = [
    ("planner.RACE_BAND",          25,   17,   33),
    ("planner.RACE_FRAME_MULT",    1.75, 1.25, 2.3),
    ("planner.SWITCH_MARGIN",      1.15, 1.10, 1.30),   # 扰动作用在超出 1 的裕度上
    ("planner.FUNNEL_GUARD_PRIOR", 0.7,  0.49, 0.91),
    ("planner.OFFPATH_RACE_FLOOR", 0.85, 0.60, 1.0),
    ("planner.SHADOW_CHOKE_PENALTY", 35, 24,   46),
    ("strategy.GUARD_SLACK_HOT",   25,   17,   33),
    ("strategy.GUARD_HOT_OPP_ETA", 60,   42,   78),
    ("strategy.GUARD_SLACK_MIN",   65,   45,   85),
    ("strategy.TRAP_AVOID_WINDOW", 120,  84,   156),
    ("strategy.LOITER_BUDGET",     50,   35,   65),
    ("strategy.CAMPER_GRACE",      8,    5,    11),
]


_OPP = None   # worker 进程内的陪练类名（"camper"/"rusher"/None=镜像）


def _init_worker(opp):
    global _OPP
    _OPP = opp


def _one(job):
    """job = (seed, patch_side, key, value)；返回变体视角的 (win, margin, 摘要)。"""
    from arena import run_match, PID_A, PID_B
    seed, side, key, value = job
    patches = {key: value} if key else None
    bot = None
    if _OPP:
        from sparring import BOTS
        bot = BOTS[_OPP]
    if side == "A":
        r = run_match(seed, patches_a=patches, cls_b=bot)
        me, opp = PID_A, PID_B
    else:
        r = run_match(seed, patches_b=patches, cls_a=bot)
        me, opp = PID_B, PID_A
    win = 1 if r[me]["score"] > r[opp]["score"] else 0
    draw = 1 if r[me]["score"] == r[opp]["score"] else 0
    return {"seed": seed, "side": side, "key": key, "value": value,
            "win": win, "draw": draw,
            "margin": r[me]["score"] - r[opp]["score"],
            "my_score": r[me]["score"], "opp_score": r[opp]["score"],
            "my_dlv": r[me]["deliverRound"], "opp_dlv": r[opp]["deliverRound"]}


def run_condition(pool, seeds, key, value):
    jobs = [(s, side, key, value) for s in seeds for side in ("A", "B")]
    return pool.map(_one, jobs)


def summarize(rows):
    n = len(rows)
    wins = sum(r["win"] for r in rows)
    draws = sum(r["draw"] for r in rows)
    margin = sum(r["margin"] for r in rows) / n
    dlv = [r["my_dlv"] for r in rows if r["my_dlv"]]
    return {"n": n, "win_rate": wins / n, "draws": draws,
            "avg_margin": round(margin, 1),
            "avg_deliver": round(sum(dlv) / len(dlv), 1) if dlv else None,
            "undelivered": sum(1 for r in rows if not r["my_dlv"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=max(1, mp.cpu_count() - 2))
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--param", type=str, help="只扫这个参数")
    ap.add_argument("--opp", choices=("camper", "rusher"),
                    help="对手用脚本陪练（变体打在我方侧）")
    ap.add_argument("--out", type=str, default="sweep_results.json")
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))
    t0 = time.time()
    results = {}

    with mp.Pool(args.jobs, initializer=_init_worker,
                 initargs=(args.opp,)) as pool:
        base_rows = run_condition(pool, seeds, None, None)
        base = summarize(base_rows)
        results["baseline"] = {"summary": base, "rows": base_rows}
        print(f"baseline(双方默认, 变体侧=空补丁): n={base['n']} "
              f"win={base['win_rate']:.2f} margin={base['avg_margin']:+} "
              f"dlv={base['avg_deliver']} undlv={base['undelivered']}")
        if args.baseline:
            _dump(args.out, results, t0)
            return

        params = PARAMS
        if args.param:
            params = [p for p in PARAMS if p[0] == args.param]
        for key, default, lo, hi in params:
            for tag, val in (("lo", lo), ("hi", hi)):
                rows = run_condition(pool, seeds, key, val)
                s = summarize(rows)
                results[f"{key}={val}"] = {"summary": s, "rows": rows}
                delta_wr = s["win_rate"] - 0.5
                print(f"{key:>34} {default} -> {val:<6} "
                      f"win={s['win_rate']:.2f} ({delta_wr:+.2f}) "
                      f"margin={s['avg_margin']:+7} dlv={s['avg_deliver']} "
                      f"undlv={s['undelivered']}", flush=True)
    _dump(args.out, results, t0)


def _dump(out, results, t0):
    slim = {k: v["summary"] for k, v in results.items()}
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summaries": slim,
                   "rows": {k: v["rows"] for k, v in results.items()}},
                  f, ensure_ascii=False, indent=1)
    print(f"\n{out} written, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
