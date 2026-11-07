#!/usr/bin/env python3
"""Roundtable: predictor x container players, frontier-learning rounds, five seeds.

Levels (problems): synthetic original mix, synthetic mover-heavy, and the root trajectory of the
motion-matching clip library (clip root motion, NOT controller output; mm.elf is not run).
Unit of every score: zstd bytes per entity update (lower wins).

Rule written before the run (winner of a cell): lowest mean over seeds; any player within
2 x the standard deviation (over seeds) of the best, floored at 0.05 bytes, ties to the simpler player.
Round 1 runs the baseline family on every cell. A cell is on the frontier when the round-1 champion
is more than 5% above the lower bound (oracle residuals in a fixed slot with a prefix). Round 2 runs
the new players on frontier cells only.
"""
import json, math, os, random, statistics, struct, sys, time
from multiprocessing import Pool
import zstandard as zstd
import bench as B
import bench_predict as P

CLIPS = os.environ.get("CLIPS", os.path.expanduser("~/.hermes/cache/scratch/mmg/data/kimodo-locomotion"))
T, EVAL_FROM, PKT_STRIDE = 200, 70, 5
SEEDS = [20261107 + 11 * k for k in range(5)]
LAGS = (1, 6, 30)
N = B.N
GRAPH = {"idle": ["idle", "start_walk", "start_run"], "start_walk": ["walk"],
         "walk": ["walk", "stop_walk", "start_run"], "stop_walk": ["idle"], "start_run": ["run"],
         "run": ["run", "stop_run", "run_circle_left", "run_circle_right"],
         "run_circle_left": ["run", "stop_run"], "run_circle_right": ["run", "stop_run"],
         "stop_run": ["idle"]}

# ---------------------------------------------------------------- clip root motion
_clip_cache = {}
def clip_deltas(name, hz):
    key = (name, hz)
    if key in _clip_cache: return _clip_cache[key]
    d = f"{CLIPS}/{name}"
    b = open(f"{d}/transforms.f32", "rb").read(); n = len(b) // 4
    f = struct.unpack(f"<{n}f", b)
    pts = [(f[i * 360 + 9], f[i * 360 + 10], f[i * 360 + 11]) for i in range(n // 360)]   # joint 0 origin, metres
    if os.path.exists(f"{d}/loop.txt"):
        w = open(f"{d}/loop.txt").read().split()
        a, e = int(w[w.index("from") + 1]), int(w[w.index("to") + 1])
        if a < len(pts) - 1: pts = pts[a:e + 1]   # v0 behaviour kept (a slice past the end clamps); only a start outside the clip (run_circle_left) falls back to the whole clip
    if hz == 30: pts = pts[::2]      # the 30 fps samples Kimodo made; 60 fps is makima-upsampled from them
    elif hz == 20: pts = pts[::3]    # every third 60 fps frame: 20 Hz, partly makima-interpolated
    out = [tuple((q - p) * 1e6 for p, q in zip(pts[i], pts[i + 1])) for i in range(len(pts) - 1)]
    _clip_cache[key] = out
    return out

def _dir(ds):
    x, z = sum(d[0] for d in ds), sum(d[2] for d in ds)
    return None if math.hypot(x, z) / max(len(ds), 1) < 0.0 else (math.atan2(x, z), math.hypot(x, z))

def traj(rnd, hz, nticks, idle_only):
    out, theta, he = [], rnd.uniform(0, 2 * math.pi), None
    cur = "idle" if idle_only else rnd.choice(["idle", "walk", "run"])
    half = hz // 2
    while len(out) < nticks:
        raw = clip_deltas(cur, hz)
        head = raw[:half]
        sx, sz = sum(d[0] for d in head), sum(d[2] for d in head)
        speed = math.hypot(sx, sz) / len(head) * hz / 1e6
        if speed > 0.3:
            hs = math.atan2(sx, sz)
            theta = (he - hs) if he is not None else rnd.uniform(0, 2 * math.pi)
        c, s = math.cos(theta), math.sin(theta)
        rot = [(d[0] * c + d[2] * s, d[1], -d[0] * s + d[2] * c) for d in raw]
        out += rot
        tail = rot[-half:]
        tx, tz = sum(d[0] for d in tail), sum(d[2] for d in tail)
        he = math.atan2(tx, tz) if math.hypot(tx, tz) / len(tail) * hz / 1e6 > 0.3 else None
        cur = "idle" if idle_only else rnd.choice(GRAPH[cur])
    return out[:nticks]

def gen_clip(seed, nticks, jitter, hz):
    rnd = random.Random(seed)
    ents = []
    for i in range(N):
        idle_only = rnd.random() < 0.3
        ents.append(dict(idle=idle_only, d=traj(rnd, hz, nticks, idle_only),
                         p=[rnd.uniform(-B.ZONE, B.ZONE) / 2, 0.0, rnd.uniform(-B.ZONE, B.ZONE) / 2],
                         owner=i % 200, gid=1000 + i, head=0.0))
    jr = random.Random(seed ^ 0x5EED)   # seeded jitter (v0 used the global unseeded random)
    run = []
    for t in range(nticks):
        row = []
        for e in ents:
            d = e["d"][t] if t else (0.0, 0.0, 0.0)
            for ax in range(3): e["p"][ax] += d[ax]
            if math.hypot(d[0], d[2]) > 1e-3: e["head"] = math.atan2(d[0], d[2])
            pos = tuple(int(round(c + (jr.gauss(0, jitter) if (jitter and not e["idle"]) else 0.0))) for c in e["p"])
            vel = tuple(max(-32768, min(32767, int(round(c * 32767 / B.V_MAX)))) for c in d)
            yaw = ((int(round((e["head"] % 6.2832) * 65536 / 6.2832)) + 32768) % 65536) - 32768
            row.append(dict(gid=e["gid"], pos=pos, vel=vel, yaw=yaw, owner=e["owner"], klass=2 - e["idle"],
                            payload=bytes(42), hlc=(t << 8), kind=0 if e["idle"] else 1))
        run.append(row)
    return run

PROBLEMS = {
    "synth_original_60Hz": lambda s, j: P.gen2(s, T, j, (0.7, 0.2, 0.1, 0, 0)),
    "synth_mover_heavy_60Hz": lambda s, j: P.gen2(s, T, j, (0.1, 0.2, 0.1, 0.4, 0.2)),
    "clip_root_30Hz": lambda s, j: gen_clip(s, T, j, 30),
    "clip_root_60Hz_makima_upsampled": lambda s, j: gen_clip(s, T, j, 60),
}
CELLS = [("synth_original_60Hz", 100), ("synth_mover_heavy_60Hz", 100),
         ("clip_root_30Hz", 0), ("clip_root_30Hz", 100),
         ("clip_root_60Hz_makima_upsampled", 0), ("clip_root_60Hz_makima_upsampled", 100)]

# ---------------------------------------------------------------- predictors
def pred_makima_vel(run, t, lag, i):
    """Base + sum of a makima curve through the velocity samples at L-2, L-1, L and the packet's own at t."""
    L, sc, base = t - lag, B.V_MAX / 32767, run[t - lag][i]["pos"]
    out = []
    for ax in range(3):
        v = lambda tau: run[tau][i]["vel"][ax] * sc
        if lag == 1: s = v(t)
        else:
            s = 0.0
            for k in range(1, lag + 1):
                val, _ = P.makima_in_time(v(L), v(t), v(L - 1), v(t), v(L - 2), v(t), k / lag,
                                          float(lag), -1.0, float(lag), -2.0, float(lag))
                s += val
        out.append(int(round(P.clamp_step(base[ax] + s, base[ax], lag))))
    return tuple(out)

PRED = {"hold": P.pred_hold, "linear_pkt_vel": P.pred_linear_pkt_vel, "linear_base_vel": P.pred_linear_base_vel,
        "makima_tangent": P._mk("tangent"), "makima_cubic": P._mk("cubic"),
        "makima_vel_int": pred_makima_vel, "oracle": lambda r, t, l, i: r[t][i]["pos"]}
CONTROLS = {"oracle", "makima_cubic"}
SIMPLE_PRED = {"-": 0, "hold": 0, "linear_pkt_vel": 1, "linear_base_vel": 1, "makima_tangent": 2, "makima_vel_int": 3}
SIMPLE_CONT = {"zzslot_batch": 1, "zzslot_prefix": 4, "zzslot_pkt_dict8k": 8, "slot_pkt_dict8k": 8, "abs_batch": 0, "varint_batch": 1, "slot_batch": 2, "abs_prefix": 3, "varint_prefix": 4,
               "slot_prefix": 5, "abs_pkt_dict8k": 6, "varint_pkt_dict8k": 7}

# ---------------------------------------------------------------- containers
def fmt_of(cont): return cont.split("_")[0]

def make_ctx(run, lag):
    cache = {}
    def resid(pred, t):
        k = (pred, t)
        if k not in cache:
            row = []
            for i, e in enumerate(run[t]):
                p = PRED[pred](run, t, lag, i)
                row.append(tuple(a - b for a, b in zip(e["pos"], p)) + p)
            cache[k] = row
        return cache[k]
    def recs(pred, cont, t):
        f = fmt_of(cont); out = []
        if f == "abs": return [B.rec_A(e, B.ORIGIN_FAR) for e in run[t]]
        for e, r in zip(run[t], resid(pred, t)):
            res = r[:3]
            if f == "varint": body = b"".join(B.varint(B.zz(x)) for x in res)
            elif f == "zzslot": body = struct.pack("<QQQ", *(B.zz(x) for x in res))
            else: body = struct.pack("<qqq", *res)
            out.append(B.head(e) + body + B.tail(e))
            if f != "varint": assert len(out[-1]) == 100
        if t % 17 == 0:
            for e, r, rec in zip(run[t], resid(pred, t), out):
                if f == "varint":
                    o, vals = 4, []
                    for _ in range(3):
                        u, o = B.read_varint(rec, o); vals.append(B.unzz(u))
                    assert rec[o:] == B.tail(e)
                elif f == "zzslot":
                    vals = [B.unzz(u) for u in struct.unpack("<QQQ", rec[4:28])]
                else:
                    vals = list(struct.unpack("<qqq", rec[4:28]))
                assert tuple(a + b for a, b in zip(vals, r[3:])) == e["pos"], (pred, cont)
        return out
    return recs

def score(run, tr_ctx, lag, pred, cont, cz, rnd):
    recs = make_ctx(run, lag)
    ticks = list(range(EVAL_FROM, T))
    if cont.endswith("_batch"):
        tot = sum(len(cz.compress(b"".join(recs(pred, cont, t)))) for t in ticks)
        return tot / (len(ticks) * N)
    if cont.endswith("_prefix"):
        tot = 0
        for t in ticks:
            d = zstd.ZstdCompressionDict(b"".join(recs(pred, cont, t - lag)), dict_type=zstd.DICT_TYPE_RAWCONTENT)
            tot += len(zstd.ZstdCompressor(level=3, dict_data=d, **B.FLAGS).compress(b"".join(recs(pred, cont, t))))
        return tot / (len(ticks) * N)
    if cont.endswith("_pkt_dict8k"):
        trrecs = tr_ctx()
        samples = [r for t in range(EVAL_FROM, T, 3) for r in trrecs(pred, cont, t)]
        rnd.shuffle(samples)
        d = zstd.train_dictionary(8192, samples[:30000])
        c = zstd.ZstdCompressor(level=3, dict_data=d, **B.FLAGS)
        pk = [r for t in ticks[::PKT_STRIDE] for r in recs(pred, cont, t)]
        return sum(len(c.compress(r)) for r in pk) / len(pk)
    raise KeyError(cont)

def run_job(args):
    prob, jit, lag, seed, players = args
    run = PROBLEMS[prob](seed, jit)
    tr_holder = []
    def tr_ctx():
        if not tr_holder: tr_holder.append(make_ctx(PROBLEMS[prob](seed + 1000, jit), lag))
        return tr_holder[0]
    cz = zstd.ZstdCompressor(level=3, **B.FLAGS)
    rnd = random.Random(seed)
    return [dict(prob=prob, jitter_um=jit, lag_ticks=lag, seed=seed, pred=p, cont=c,
                 bytes_per_update=score(run, tr_ctx, lag, p, c, cz, rnd)) for p, c in players]

# ---------------------------------------------------------------- players per round
P1 = [(p, "varint_batch") for p in ("hold", "linear_pkt_vel", "linear_base_vel", "makima_tangent")] + \
     [("-", "abs_batch"), ("-", "abs_prefix"), ("oracle", "varint_batch"), ("oracle", "slot_prefix"),
      ("makima_cubic", "varint_batch")]
PREDS_ALL = ["hold", "linear_pkt_vel", "linear_base_vel", "makima_tangent", "makima_vel_int"]
P2 = [(p, c) for p in PREDS_ALL for c in ("slot_batch", "slot_prefix", "varint_prefix", "varint_pkt_dict8k")] + \
     [("makima_vel_int", "varint_batch"), ("-", "abs_pkt_dict8k"),
      ("oracle", "slot_batch"), ("oracle", "varint_prefix")]
P2 = [x for x in P2 if x not in P1]

def run_round(players_by_cell, label):
    jobs = [(prob, jit, lag, seed, pl) for (prob, jit, lag), pl in players_by_cell.items() for seed in SEEDS]
    t0 = time.time()
    with Pool(8) as pool: rows = [r for rs in pool.map(run_job, jobs, chunksize=1) for r in rs]
    print(f"{label}: {len(jobs)} jobs, {len(rows)} scores, {time.time() - t0:.0f} s", flush=True)
    return rows

def table(rows):
    t = {}
    for r in rows:
        t.setdefault((r["prob"], r["jitter_um"], r["lag_ticks"]), {}).setdefault((r["pred"], r["cont"]), []).append(r["bytes_per_update"])
    return t

def winner(cell):
    stats = {pl: (statistics.mean(v), statistics.pstdev(v)) for pl, v in cell.items() if pl[0] not in CONTROLS}
    best = min(stats, key=lambda k: stats[k][0])
    tol = 2 * max(stats[best][1], 0.05)
    near = [k for k in stats if stats[k][0] <= stats[best][0] + tol]
    return min(near, key=lambda k: (SIMPLE_CONT[k[1]], SIMPLE_PRED[k[0]])), best, stats

def dict_version_control():
    """Planted defect: a client holds another dictionary version. It must fail to decode, never decode wrong."""
    run = PROBLEMS["clip_root_30Hz"](SEEDS[0], 100)
    recs = make_ctx(run, 6)
    samples = [r for t in range(EVAL_FROM, T, 3) for r in recs("linear_pkt_vel", "varint_pkt_dict8k", t)]
    random.Random(1).shuffle(samples)
    dA = zstd.train_dictionary(8192, samples[:len(samples) // 2])
    dB = zstd.train_dictionary(8192, samples[len(samples) // 2:])
    test = recs("linear_pkt_vel", "varint_pkt_dict8k", 120)
    res = {}
    for ids in (True, False):
        cA = zstd.ZstdCompressor(level=3, dict_data=dA, write_dict_id=ids, write_content_size=False, write_checksum=False)
        dec = zstd.ZstdDecompressor(dict_data=dB)
        err = wrong = right = 0
        for r in test:
            try:
                out = dec.decompress(cA.compress(r), max_output_size=len(r) + 64)
                right += out == r; wrong += out != r
            except zstd.ZstdError:
                err += 1
        res["dict_id_on" if ids else "dict_id_off"] = dict(records=len(test), refused=err, decoded_wrong=wrong, decoded_right=right)
    return res

def main():
    t_all = time.time()
    r1 = run_round({(p, j, l): P1 for p, j in CELLS for l in LAGS}, "round 1")
    t1 = table(r1)
    frontier, champs = {}, {}
    for key, cell in t1.items():
        w, best, stats = winner(cell)
        bound = statistics.mean(cell[("oracle", "slot_prefix")])
        champs[key] = (w, stats[w][0], bound)
        if stats[w][0] > 1.05 * bound: frontier[key] = P2
    r2 = run_round(frontier, "round 2") if frontier else []
    rows = r1 + r2
    t2 = table(rows)
    out = dict(conditions=dict(T=T, eval_from=EVAL_FROM, seeds=SEEDS, N=N, lags_ticks=LAGS, clips=CLIPS,
                               zstd=zstd.__version__, level=3, wall_s=round(time.time() - t_all)),
               round1_champions={"|".join(map(str, k)): [list(v[0]), v[1], v[2]] for k, v in champs.items()},
               frontier=["|".join(map(str, k)) for k in frontier], rows=rows, dict_version_control=dict_version_control())
    json.dump(out, open("results_roundtable.json", "w"), indent=1)
    print("done", out["conditions"]["wall_s"], "s")

if __name__ == "__main__":
    main()
