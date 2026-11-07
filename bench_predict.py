#!/usr/bin/env python3
"""Dead-reckoning predictors against an acked snapshot history.

makima_in_time is a line-for-line port of makimaInTime in
V-Sekai-fire/interactor-xr-pilot core/src/Makima.cpp (itself a port of Godot's
Animation::makima_interpolate_in_time). Predictor inputs are exact integer positions
from acked snapshots at ticks <= t - lag. The decoder runs the same function; this
does NOT prove bit-identity across languages (floating point). Spec before shipping.
"""
import json, math, random, sys
from multiprocessing import Pool
import zstandard as zstd
import bench as B

N, HZ, LEVEL = B.N, B.HZ, B.LEVEL
T_TEST, EVAL_FROM = 300, 40
SEED = 20261107
UM_PER_TICK = lambda mps: mps * 1e6 / HZ

# ---- makima (port) ----------------------------------------------------------
def _tangent(p0, p1, p2, p3):
    w1 = abs(p3 - p2) + abs(p3 + p2) * 0.5
    w2 = abs(p1 - p0) + abs(p1 + p0) * 0.5
    return (p1 + p2) * 0.5 if w1 + w2 == 0.0 else (w1 * p1 + w2 * p2) / (w1 + w2)

def makima_in_time(frm, to, pre, post, prepre, postpost, w, toT, preT, postT, prepreT, postpostT):
    """Returns (value at weight w, tangent at `to`)."""
    ex = lambda e, n: 2.0 * e - n
    toC, preC, postC = toT == 0.0, preT == 0.0, postT - toT == 0.0
    prepreC, postpostC = preT - prepreT == 0.0, postpostT - postT == 0.0
    mFrom = 0.0 if toC else (to - frm) / toT
    mPre = 0.0 if preC else (frm - pre) / -preT
    mPost = 0.0 if postC else (post - to) / (postT - toT)
    if preC: mPre = mFrom if postC else ex(mFrom, mPost)
    if postC: mPost = mFrom if preC else ex(mFrom, mPre)
    mPrePre = ex(mPre, mFrom) if prepreC else (pre - prepre) / (preT - prepreT)
    mPostPost = ex(mPost, mFrom) if postpostC else (postpost - post) / (postpostT - postT)
    tFrom = _tangent(mPrePre, mPre, mFrom, mPost)
    tTo = _tangent(mPre, mFrom, mPost, mPostPost)
    s2 = w * w; s3 = s2 * w
    val = ((2*s3 - 3*s2 + 1) * frm + (s3 - 2*s2 + w) * toT * tFrom
           + (-2*s3 + 3*s2) * to + (s3 - s2) * toT * tTo)
    return val, tTo

def predict_axis(hist, lag, mode):
    """hist = positions at ticks L-3..L (L = acked baseline tick). Predict tick L+lag."""
    p0, p1, p2, p3 = hist
    # knots: t = L-3, L-2, L-1, L, L, L  (end repeated, Akima edge rule), segment [L-1, L]
    val, tTo = makima_in_time(p2, p3, p1, p3, p0, p3, 1.0 + lag, 1.0, -1.0, 1.0, -2.0, 1.0)
    if mode == "cubic":   # Godot's curve evaluated past its segment (weight 1+lag)
        return val
    return p3 + tTo * lag  # "tangent": last acked position + makima end slope * lag

def clamp_step(pred, base, lag):  # PBVH velocity cap 500000 um/tick (Codec.lean V_MAX)
    cap = B.V_MAX * lag
    return max(base - cap, min(base + cap, pred))

# ---- trajectories -----------------------------------------------------------
def gen2(seed, T, jitter, mix):
    rnd = random.Random(seed)
    ents = []
    for i in range(N):
        r = rnd.random(); acc = 0; kind = 0
        for k, m in enumerate(mix):
            acc += m
            if r < acc: kind = k; break
        sp = [0, 1.4, 10.0, 5.0, 1.4][kind]
        ents.append(dict(kind=kind, gid=1000 + i, owner=i % 200, head=rnd.uniform(0, 6.28),
                         p=[rnd.uniform(-B.ZONE, B.ZONE), 0.0, rnd.uniform(-B.ZONE, B.ZONE)],
                         speed=UM_PER_TICK(sp), phase=rnd.randrange(120)))
    run = []
    for t in range(T):
        row = []
        for e in ents:
            k = e["kind"]
            if k in (1, 2): e["head"] += rnd.gauss(0, 0.03 if k == 1 else 0.008)
            if k == 3: e["head"] += 0.05                  # smooth arc, 0.05 rad per tick
            moving = k in (1, 2, 3) or (k == 4 and ((t + e["phase"]) // 60) % 2 == 0)
            if moving:
                v = (e["speed"] * math.cos(e["head"]), 0.0, e["speed"] * math.sin(e["head"]))
                for ax in (0, 2):
                    e["p"][ax] += v[ax]
                    if abs(e["p"][ax]) > B.ZONE:
                        e["p"][ax] -= 2 * v[ax]; e["head"] += math.pi
            else:
                v = (0.0, 0.0, 0.0)
            pos = tuple(int(round(c + (rnd.gauss(0, jitter) if (jitter and k) else 0.0))) for c in e["p"])
            vel = tuple(max(-32768, min(32767, int(round(c * 32767 / B.V_MAX)))) for c in v)
            yaw = ((int(round((e["head"] % 6.2832) * 65536 / 6.2832)) + 32768) % 65536) - 32768
            row.append(dict(gid=e["gid"], pos=pos, vel=vel, yaw=yaw, owner=e["owner"], klass=k + 1,
                            payload=bytes(42), hlc=(t << 8), kind=k))
        run.append(row)
    return run

# ---- predictors: (name, fn(run, t, lag, i) -> predicted pos tuple) -----------
def pred_hold(run, t, lag, i):  return run[t - lag][i]["pos"]
def pred_linear_pkt_vel(run, t, lag, i):  # variant F: velocity from the packet being decoded
    return B.predict(run[t][i], run[t - lag][i], lag)
def pred_linear_base_vel(run, t, lag, i):  # velocity known at the baseline (BVH-style)
    return B.predict(run[t - lag][i], run[t - lag][i], lag)
def _mk(mode):
    def f(run, t, lag, i):
        L = t - lag
        out = []
        for ax in range(3):
            hist = [run[L - 3 + k][i]["pos"][ax] for k in range(4)]
            out.append(int(round(clamp_step(predict_axis(hist, lag, mode), hist[3], lag))))
        return tuple(out)
    return f
PREDICTORS = [("hold", pred_hold), ("linear_pkt_vel", pred_linear_pkt_vel),
              ("linear_base_vel", pred_linear_base_vel), ("makima_cubic", _mk("cubic")),
              ("makima_tangent", _mk("tangent")), ("oracle", lambda r, t, l, i: r[t][i]["pos"])]

def run_cfg(args):
    name, mix, jitter, lag = args
    te = gen2(SEED, T_TEST, jitter, mix)
    cz = zstd.ZstdCompressor(level=LEVEL, **B.FLAGS)
    out = []
    ticks = list(range(EVAL_FROM, T_TEST))
    for pname, fn in PREDICTORS:
        raw = 0; comp = 0; absum = [0.0] * 5; cnt = [0] * 5
        for t in ticks:
            recs = []
            for i, e in enumerate(te[t]):
                p = fn(te, t, lag, i)
                res = tuple(a - b for a, b in zip(e["pos"], p))
                assert tuple(b + r for b, r in zip(p, res)) == e["pos"]   # roundtrip
                recs.append(B.head(e) + b"".join(B.varint(B.zz(r)) for r in res) + B.tail(e))
                absum[e["kind"]] += sum(abs(r) for r in res) / 3; cnt[e["kind"]] += 1
            raw += sum(map(len, recs)); comp += len(cz.compress(b"".join(recs)))
        n = len(ticks) * N
        out.append(dict(scenario=name, jitter_um=jitter, lag_ticks=lag, predictor=pname,
                        raw_bytes_per_update=round(raw / n, 2), zstd_bytes_per_update=round(comp / n, 2),
                        mean_abs_residual_um={k: round(absum[k] / cnt[k], 1) for k in range(5) if cnt[k]}))
    return out

def main():
    scen = [("original_mix_70static_20walk_10fast", (0.7, 0.2, 0.1, 0, 0)),
            ("mover_heavy_10static_20walk_10fast_40arc_20stopstart", (0.1, 0.2, 0.1, 0.4, 0.2))]
    jobs = [(n, m, j, l) for n, m in scen for j in (0, 100) for l in (1, 6, 30)]
    with Pool(8) as p: rows = [r for rs in p.map(run_cfg, jobs) for r in rs]
    json.dump(rows, open("results_predict.json", "w"), indent=1)
    print(len(rows), "rows")

if __name__ == "__main__":
    main()
