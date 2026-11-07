#!/usr/bin/env python3
"""R24 / R16 at far origins. Same mm.elf motion as bench_payload, positions shifted by a base in um.
Levels: origin 4.4e18 um (about 29 AU, Neptune) and an edge level 1e9 um (1 km) below the Int64 maximum.
Success rule is bench_payload's: bit-exact roundtrip and keyframe rate <= 1%. struct '<q' raises on an out-of-range
position, so an Int64 overflow in encode shows up as an error, not a silent wrap."""
import json, struct
from multiprocessing import Pool
import bench_payload as F
import bench_roundtable as R

I64 = (1 << 63) - 1
BASES = {"origin_29AU": (4_400_000_000_000_000_000, 0, -4_400_000_000_000_000_000),
         "edge_int64": (I64 - 1_000_000_000, 0, -(I64 - 1_000_000_000))}
for hz in (60, 20):
    for name, base in BASES.items():
        def mk(s, j, hz=hz, base=base):
            run = F.gen_mm(s, j, hz)
            return [[dict(e, pos=tuple(p + b for p, b in zip(e["pos"], base))) for e in row] for row in run]
        R.PROBLEMS[f"mm_elf_{hz}Hz_{name}"] = mk

LEVELS = [(f"mm_elf_{hz}Hz_{n}", 100, lag) for hz in (60, 20) for n in BASES for lag in (6, 30)]

def job(a):
    prob, jit, lag, seed = a
    try:
        r = F.run_job(a)
    except Exception as e:
        return dict(prob=prob, lag=lag, seed=seed, error=f"{type(e).__name__}: {e}")
    run = R.PROBLEMS[prob](seed, jit)
    pred_out = 0
    for t in range(R.EVAL_FROM, R.T, 17):
        for i in range(R.N):
            for fn in ("hold", "linear_pkt_vel"):
                p = R.PRED[fn](run, t, lag, i)
                pred_out += any(not (-(I64 + 1) <= c <= I64) for c in p)
    return dict(prob=prob, lag=lag, seed=seed, players=r["players"], lanes=r["lanes"], pred_out_of_int64=pred_out)

def main():
    with Pool(8) as p:
        rows = p.map(job, [(pr, j, l, s) for pr, j, l in LEVELS for s in F.SEEDS], chunksize=1)
    out = []
    for pr, j, lag in LEVELS:
        rs = [r for r in rows if r["prob"] == pr and r["lag"] == lag]
        errs = sorted({r["error"] for r in rs if "error" in r})
        ok = [r for r in rs if "error" not in r]
        cell = dict(level=f"{pr}|{lag}", errors=errs, runs_ok=len(ok), pred_out_of_int64=sum(r["pred_out_of_int64"] for r in ok))
        for pn in F.PREDS:
            for w in F.WIDTHS:
                s = sum(r["players"][pn]["clean"][w] and r["players"][pn]["escapes"][w] <= F.ESC_LIMIT * r["players"][pn]["packets"] for r in ok)
                esc = sum(r["players"][pn]["escapes"][w] for r in ok); pk = sum(r["players"][pn]["packets"] for r in ok)
                cell[f"{pn}+R{w}"] = dict(success_of_8=s, keyframe_pct=round(100 * esc / pk, 3) if pk else None)
        out.append(cell)
    json.dump(out, open("results_far.json", "w"), indent=1)
    for c in out:
        print(c["level"], "errors", c["errors"], "pred_out", c["pred_out_of_int64"], {k: (v["success_of_8"], v["keyframe_pct"]) for k, v in c.items() if k in ("hold+R24", "makima_vel_int+R16", "makima_vel_int+R24")})

if __name__ == "__main__":
    main()
