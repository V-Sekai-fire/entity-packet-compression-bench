#!/usr/bin/env python3
"""Entity packet width / delta / zstd-dictionary benchmark.

Synthetic zone, wire layout copied from EntityPacket/Codec.lean
(V-Sekai-fire/transport-iouring-feed-server, dev). Little-endian.
Reproduce: `.venv/bin/python bench.py`  (seeded; writes results.csv, results.json, results.md)
"""
import json, math, platform, random, struct, sys, time, csv
from multiprocessing import Pool
import zstandard as zstd

# ---- conditions -----------------------------------------------------------
N = 256               # entities in the zone, all sent every tick
HZ = 60
T_TRAIN, T_TEST = 240, 300
EVAL_FROM = 40        # first evaluated tick (baselines for lag <= 30 exist)
PKT_STRIDE = 5        # per-packet modes sample every 5th test tick
LEVEL = 3
V_MAX = 500000        # um/tick over i16 (PBVH scale in Codec.lean)
ZONE = 1_000_000_000  # zone is +-1 km in um, fits Int32 (+-2147 km... see README)
ORIGIN_FAR = (300_000_000_000, 0, -50_000_000_000)  # um, why Int64 is needed
MIX = (0.70, 0.20, 0.10)  # static, walker 1.4 m/s, fast 10 m/s
DICT_PKT, DICT_BATCH = 8192, 65536
SEED = 20261107
FLAGS = dict(write_content_size=False, write_checksum=False, write_dict_id=False)

# ---- primitives -----------------------------------------------------------
def zz(n):  # zigzag, 64-bit
    return ((n << 1) ^ (n >> 63)) & 0xFFFFFFFFFFFFFFFF
def unzz(u):
    return (u >> 1) ^ -(u & 1)
def varint(u):
    out = bytearray()
    while True:
        b = u & 0x7F; u >>= 7
        if u: out.append(b | 0x80)
        else:
            out.append(b); return bytes(out)
def read_varint(b, o):
    u = 0; s = 0
    while True:
        c = b[o]; o += 1
        u |= (c & 0x7F) << s; s += 7
        if not c & 0x80: return u, o

# ---- synthetic zone -------------------------------------------------------
def gen(seed, T, jitter, payload):
    rnd = random.Random(seed)
    ents = []
    for i in range(N):
        r = rnd.random()
        kind = 0 if r < MIX[0] else (1 if r < MIX[0] + MIX[1] else 2)
        ents.append(dict(
            kind=kind, gid=1000 + i, owner=i % 200,
            p=[rnd.uniform(-ZONE, ZONE), 0.0, rnd.uniform(-ZONE, ZONE)],
            head=rnd.uniform(0, 2 * math.pi),
            speed={0: 0.0, 1: 1400.0, 2: 10000.0}[kind] * 1000 / HZ,  # um/tick
            turn={0: 0.0, 1: 0.03, 2: 0.008}[kind],
            payload=(bytes(42) if payload == "zero" else bytes(rnd.getrandbits(8) for _ in range(42)))))
    run = []
    for t in range(T):
        row = []
        for e in ents:
            if e["kind"]:
                e["head"] += rnd.gauss(0, e["turn"])
                vx, vz = e["speed"] * math.cos(e["head"]), e["speed"] * math.sin(e["head"])
                for ax, v in ((0, vx), (2, vz)):
                    e["p"][ax] += v
                    if abs(e["p"][ax]) > ZONE:  # bounce at zone edge
                        e["p"][ax] -= 2 * v; e["head"] += math.pi
                        vx, vz = (-vx, vz) if ax == 0 else (vx, -vz)
                v = (vx, 0.0, vz)
            else:
                v = (0.0, 0.0, 0.0)
            pos = tuple(int(round(c + (rnd.gauss(0, jitter) if (jitter and e["kind"]) else 0.0))) for c in e["p"])
            vel = tuple(max(-32768, min(32767, int(round(c * 32767 / V_MAX)))) for c in v)
            yaw = int(round((e["head"] % (2 * math.pi)) * 65536 / (2 * math.pi)))
            yaw = ((yaw + 32768) % 65536) - 32768
            row.append(dict(gid=e["gid"], pos=pos, vel=vel, yaw=yaw, owner=e["owner"],
                            klass=e["kind"] + 1, payload=e["payload"], hlc=(t << 8)))
        run.append(row)
    return run

# ---- wire pieces (Codec.lean offsets) -------------------------------------
def tail(e):  # bytes 28..100: vel@28 pad(34..40) hlc@40 class_owner@44 sub@48 rot@52 payload@58
    return struct.pack("<hhh6xIIIhhh", *e["vel"], e["hlc"] & 0xFFFFFFFF,
                       ((e["klass"] << 24) | e["owner"]) & 0xFFFFFFFF, 0, 0, 0, e["yaw"]) + e["payload"]
def head(e): return struct.pack("<I", e["gid"])

def rec_A(e, origin):
    return head(e) + struct.pack("<qqq", *(c + o for c, o in zip(e["pos"], origin))) + tail(e)
def rec_B(e):  return head(e) + struct.pack("<iii", *e["pos"]) + tail(e)
def rec_C(e):  return head(e) + struct.pack("<ddd", *((c + o) / 1e6 for c, o in zip(e["pos"], ORIGIN_FAR))) + tail(e)
def rec_D(e, b): return head(e) + struct.pack("<iii", *(c - bc for c, bc in zip(e["pos"], b["pos"]))) + tail(e)
def rec_E(e, b): return head(e) + b"".join(varint(zz(c - bc)) for c, bc in zip(e["pos"], b["pos"])) + tail(e)
def predict(e, b, lag):
    return tuple(bc + int(round(v * V_MAX / 32767 * lag)) for bc, v in zip(b["pos"], e["vel"]))
def rec_F(e, b, lag): return head(e) + b"".join(varint(zz(c - p)) for c, p in zip(e["pos"], predict(e, b, lag))) + tail(e)

# decoders (roundtrip proof; compared to truth)
def dec_AB(rec, fmt, origin):
    pos = struct.unpack("<" + fmt, rec[4:4 + struct.calcsize(fmt)])
    return tuple(p - o for p, o in zip(pos, origin)), rec[4 + struct.calcsize(fmt):]
def dec_E(rec, b, lag, dr):
    o = 4; vals = []
    for _ in range(3):
        u, o = read_varint(rec, o); vals.append(unzz(u))
    t = rec[o:]
    base = predict(dict(vel=struct.unpack("<hhh", t[:6])), b, lag) if dr else b["pos"]
    return tuple(x + d for x, d in zip(base, vals)), t

VARIANTS = ["A_i64_far", "A_i64_origin0", "B_i32_local", "C_f64_m_far",
            "D_i32_delta_slot", "E_varint_delta", "F_varint_deadreckon"]

def records(run, t, lag, variant):
    row, base = run[t], run[t - lag]
    if variant == "A_i64_far":          return [rec_A(e, ORIGIN_FAR) for e in row]
    if variant == "A_i64_origin0":      return [rec_A(e, (0, 0, 0)) for e in row]
    if variant == "B_i32_local":        return [rec_B(e) for e in row]
    if variant == "C_f64_m_far":        return [rec_C(e) for e in row]
    if variant == "D_i32_delta_slot":   return [rec_D(e, b) for e, b in zip(row, base)]
    if variant == "E_varint_delta":     return [rec_E(e, b) for e, b in zip(row, base)]
    if variant == "F_varint_deadreckon":return [rec_F(e, b, lag) for e, b in zip(row, base)]
    raise KeyError(variant)

def verify(run, lag):
    for t in range(EVAL_FROM, len(run), 37):
        row, base = run[t], run[t - lag]
        for e, b, r in zip(row, base, records(run, t, lag, "A_i64_far")):
            pos, tl = dec_AB(r, "qqq", ORIGIN_FAR); assert pos == e["pos"] and tl == tail(e)
        for e, r in zip(row, records(run, t, lag, "B_i32_local")):
            pos, tl = dec_AB(r, "iii", (0, 0, 0)); assert pos == e["pos"] and tl == tail(e)
        for e, b, r in zip(row, base, records(run, t, lag, "D_i32_delta_slot")):
            d = struct.unpack("<iii", r[4:16]); assert tuple(x + y for x, y in zip(b["pos"], d)) == e["pos"]
        for var, dr in (("E_varint_delta", False), ("F_varint_deadreckon", True)):
            for e, b, r in zip(row, base, records(run, t, lag, var)):
                pos, tl = dec_E(r, b, lag, dr); assert pos == e["pos"] and tl == tail(e), var
        for e, r in zip(row, records(run, t, lag, "C_f64_m_far")):
            m = struct.unpack("<ddd", r[4:28])
            assert tuple(int(round(x * 1e6)) - o for x, o in zip(m, ORIGIN_FAR)) == e["pos"]

# ---- compression ----------------------------------------------------------
def train(samples, size):
    try:
        return zstd.train_dictionary(size, samples)
    except Exception as ex:
        return None

def run_config(args):
    jitter, payload, lag = args
    cfg = dict(jitter_um=jitter, payload=payload, lag=lag)
    tr = gen(SEED + 1, T_TRAIN, jitter, payload)      # different population from test
    te = gen(SEED, T_TEST, jitter, payload)
    verify(te, lag)
    rows = []
    def emit(variant, mode, total, n):
        rows.append(dict(**cfg, variant=variant, mode=mode, bytes_per_update=round(total / n, 2)))
    rnd = random.Random(SEED)
    cplain = zstd.ZstdCompressor(level=LEVEL, **FLAGS)
    test_ticks = list(range(EVAL_FROM, T_TEST))
    pkt_ticks = test_ticks[::PKT_STRIDE]
    for v in VARIANTS:
        # raw
        raw = sum(len(r) for t in test_ticks for r in records(te, t, lag, v))
        emit(v, "raw", raw, len(test_ticks) * N)
        # batch (one frame per tick snapshot)
        snaps = {t: b"".join(records(te, t, lag, v)) for t in test_ticks}
        emit(v, "batch_nodict", sum(len(cplain.compress(s)) for s in snaps.values()), len(test_ticks) * N)
        trs = [b"".join(records(tr, t, lag, v)) for t in range(EVAL_FROM, T_TRAIN, 2)]
        d = train(trs, DICT_BATCH)
        if d:
            c = zstd.ZstdCompressor(level=LEVEL, dict_data=d, **FLAGS)
            emit(v, "batch_traineddict64k", sum(len(c.compress(s)) for s in snaps.values()), len(test_ticks) * N)
        # per packet
        pk = [r for t in pkt_ticks for r in records(te, t, lag, v)]
        emit(v, "pkt_nodict", sum(len(cplain.compress(r)) for r in pk), len(pk))
        trp = [r for t in range(EVAL_FROM, T_TRAIN) for r in records(tr, t, lag, v)]
        rnd.shuffle(trp)
        d = train(trp[:30000], DICT_PKT)
        if d:
            c = zstd.ZstdCompressor(level=LEVEL, dict_data=d, **FLAGS)
            emit(v, "pkt_traineddict8k", sum(len(c.compress(r)) for r in pk), len(pk))
    # G: Int64 wire unchanged, previous ACKED snapshot (t-lag) as raw-content dictionary
    v = "A_i64_far"
    tot = 0; dec = zstd.ZstdDecompressor()
    for t in test_ticks:
        prev = b"".join(records(te, t - lag, lag, v) if False else [rec_A(e, ORIGIN_FAR) for e in te[t - lag]])
        d = zstd.ZstdCompressionDict(prev, dict_type=zstd.DICT_TYPE_RAWCONTENT)
        cur = b"".join(rec_A(e, ORIGIN_FAR) for e in te[t])
        out = zstd.ZstdCompressor(level=LEVEL, dict_data=d, **FLAGS).compress(cur)
        if t % 37 == 0:
            assert zstd.ZstdDecompressor(dict_data=d).decompress(out, max_output_size=len(cur)) == cur
        tot += len(out)
    emit("G_i64_far_prefix_prev_ack", "batch_prefix_prev_snapshot", tot, len(test_ticks) * N)
    tot = 0; n = 0
    for t in pkt_ticks:
        for e, b in zip(te[t], te[t - lag]):
            d = zstd.ZstdCompressionDict(rec_A(b, ORIGIN_FAR), dict_type=zstd.DICT_TYPE_RAWCONTENT)
            cur = rec_A(e, ORIGIN_FAR)
            out = zstd.ZstdCompressor(level=LEVEL, dict_data=d, **FLAGS).compress(cur)
            tot += len(out); n += 1
    emit("G_i64_far_prefix_prev_ack", "pkt_prefix_prev_packet", tot, n)
    return rows

def main():
    jobs = [(j, p, 6) for j in (0, 100, 1000) for p in ("zero", "const")]
    jobs += [(100, "zero", l) for l in (1, 3, 12, 30)]   # ack-lag sweep
    t0 = time.time()
    with Pool(min(len(jobs), 10)) as pool:
        out = pool.map(run_config, jobs)
    rows = [r for rs in out for r in rs]
    cond = dict(machine=platform.platform(), python=sys.version.split()[0], zstandard=zstd.__version__,
                zstd=".".join(map(str, zstd.ZSTD_VERSION)), level=LEVEL, N=N, hz=HZ, train_ticks=T_TRAIN,
                test_ticks=T_TEST - EVAL_FROM, pkt_stride=PKT_STRIDE, dict_pkt=DICT_PKT, dict_batch=DICT_BATCH,
                mix_static_walker_fast=MIX, seed=SEED, frame_flags=FLAGS, wall_s=round(time.time() - t0, 1))
    json.dump(dict(conditions=cond, rows=rows), open("results.json", "w"), indent=1)
    with open("results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(json.dumps(cond, indent=1))

if __name__ == "__main__":
    main()
