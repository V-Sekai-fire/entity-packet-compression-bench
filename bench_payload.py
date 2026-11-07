#!/usr/bin/env python3
"""Frontier learning for a FIXED 100 byte packet: maximise payload bytes per packet.

Rules fixed BEFORE the run (RFD 2309 and 2323 are deleted from the record; see FRONTIER.md for what was recovered):
- Objective: payload bytes per packet = 100 - header bytes. zstd is not in the objective.
- Layouts (bytes): fixed fields gid 4, vel 6, hlc 4, class_owner 4, sub 4, rot 6 = 28.
    A_current  pos 24 + 6 pad (the real Codec.lean: bytes 34..39 unassigned)   -> payload 42
    A_pad      pos 24, pad reclaimed                                            -> payload 48
    B_i32_local pos 12 (Int32 um, zone-local origin)                            -> payload 60
    R16 / R24 / R32  residual against the predicted position in a 16 / 24 / 32 bit slot per axis,
                     plus a mode byte and a lag byte                            -> payload 64 / 61 / 58
  A residual that does not fit sets mode = 1 (keyframe): the full Int64 x3 position takes the slot and the
  start of the payload area, so that packet carries 46 payload bytes. The unit of a residual is 1 um (lossless).
- A level is (motion source, jitter, ack lag). A problem is one run with a drawn seed; 8 seeds = the last eight runs.
- Success of a player (predictor, width) on a problem: every sampled packet decodes bit-exact AND the keyframe rate <= 1%.
- Regret of a (level, player) = 1[s >= 1] - s/8. Frontier = regret > 0.
- Planted-defect lanes, each run on every level: silent_wrap (escape disabled, hold predictor, R16), stale_baseline
  (decoder uses the snapshot one tick older), float32_scale (decoder's velocity scale rounded to float32),
  rate_scale (decoder's velocity scale off by 3x, the 20 Hz against 60 Hz mistake), payload_overrun (one byte more
  than the payload area; the checked encoder must refuse by name, the unchecked one must make the packet longer than 100).
- Mutation: a lane found in fewer than 8 of 8 runs moves ONE attribute (ticks examined 1 -> 4 -> 16), kept only when
  the find rate rises. A level is calibrated when every lane reaches 8 of 8. Player results count only on calibrated levels.
- Width ladder: R16 -> R24 -> R32; the smallest width with s = 8 is the level's width for that predictor.
"""
import json, random, statistics, struct, sys, time
from multiprocessing import Pool
import bench as B
import bench_predict as P
import bench_roundtable as R

R.PROBLEMS["clip_root_20Hz"] = lambda s, j: R.gen_clip(s, R.T, j, 20)

def gen_mm(seed, jitter, hz):
    """Character position of the real mm.elf (mm_record.gd), 60 Hz controller steps, decimated to the wire rate.
    Velocity = position difference per wire tick (the controller does not report one). Zone offset added in double."""
    import os
    step = 60 // hz
    b = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mm_data", f"mm_{seed}.bin"), "rb").read()
    f = struct.unpack(f"<{len(b) // 4}f", b)
    rnd, jr, ents = random.Random(seed), random.Random(seed ^ 0x5EED), []
    for e in range(N):
        pts = [(f[(e * 600 + i) * 4], f[(e * 600 + i) * 4 + 1], f[(e * 600 + i) * 4 + 2]) for i in range(0, 600, step)][:R.T]
        travel = max(abs(p[0] - pts[0][0]) + abs(p[1] - pts[0][1]) for p in pts)
        ents.append(dict(pts=pts, idle=travel < 0.5, off=(rnd.uniform(-B.ZONE, B.ZONE) / 2, rnd.uniform(-B.ZONE, B.ZONE) / 2)))
    run = []
    for t in range(R.T):
        row = []
        for i, e in enumerate(ents):
            x, z, yaw = e["pts"][t]; px, pz, _ = e["pts"][t - 1] if t else (x, z, yaw)
            d = ((x - px) * 1e6, 0.0, (z - pz) * 1e6)
            p = (e["off"][0] + x * 1e6, 0.0, e["off"][1] + z * 1e6)
            pos = tuple(int(round(c + (jr.gauss(0, jitter) if (jitter and not e["idle"]) else 0.0))) for c in p)
            vel = tuple(max(-32768, min(32767, int(round(c * 32767 / B.V_MAX)))) for c in d)
            yc = ((int(round((yaw % 6.2832) * 65536 / 6.2832)) + 32768) % 65536) - 32768
            row.append(dict(gid=1000 + i, pos=pos, vel=vel, yaw=yc, owner=i % 200, klass=2 - e["idle"],
                            payload=bytes(42), hlc=(t << 8), kind=0 if e["idle"] else 1))
        run.append(row)
    return run

for _hz in (60, 30, 20):
    R.PROBLEMS[f"mm_elf_{_hz}Hz"] = (lambda hz: lambda s, j: gen_mm(s, j, hz))(_hz)
RUNS, KS, ESC_LIMIT = 8, (1, 4, 16), 0.01
SEEDS = [20261107 + 11 * k for k in range(RUNS)]
SOURCES = ["synth_original_60Hz", "synth_mover_heavy_60Hz", "clip_root_20Hz", "clip_root_30Hz",
           "clip_root_60Hz_makima_upsampled",
           "mm_elf_60Hz", "mm_elf_30Hz", "mm_elf_20Hz"]
LEVELS = [(p, 100, l) for p in SOURCES for l in (6, 30)]
WIDTHS, PREDS = (16, 24, 32), ("hold", "linear_pkt_vel", "makima_vel_int")
ET = [R.EVAL_FROM + 1 + 8 * j for j in range(16)]
N = R.N
LAYOUT = {"A_current": (24, 0, 6), "A_pad": (24, 0, 0), "B_i32_local": (12, 0, 0),
          "R16": (6, 2, 0), "R24": (9, 2, 0), "R32": (12, 2, 0)}    # position bytes, mode+lag bytes, pad bytes
FIXED = 28
PAYLOAD = {k: 100 - FIXED - sum(v) for k, v in LAYOUT.items()}
KEY_PAYLOAD = 100 - FIXED - 24 - 2     # a keyframe packet: 46 payload bytes
F32 = lambda x: struct.unpack("f", struct.pack("f", x))[0]
fits = lambda r, half: -half <= r < half

class PayloadTooLong(Exception): pass

def payload_bytes(e, t, n): return bytes(((e["gid"] * 7 + i * 13 + t) & 255) for i in range(n))

def encode(name, e, t, lag, pred=None, wrap=False, checked=True, over=0):
    pos_b, extra, pad = LAYOUT[name]
    b = bytearray(100); o = 0
    struct.pack_into("<I", b, o, e["gid"]); o += 4
    ext = b""
    if name.startswith("R"):
        w = pos_b * 8 // 3; half = 1 << (w - 1); nb = w // 8
        res = [x - y for x, y in zip(e["pos"], pred)]
        if wrap or all(fits(r, half) for r in res):
            mode = 0
            body = b"".join((((r + half) % (2 * half)) - half).to_bytes(nb, "little", signed=True) for r in res)
        else:
            mode = 1; full = struct.pack("<qqq", *e["pos"]); body, ext = full[:pos_b], full[pos_b:]
        b[o] = mode; b[o + 1] = lag & 255; o += 2
    else:
        body = struct.pack("<qqq" if pos_b == 24 else "<iii", *e["pos"])
    b[o:o + pos_b] = body; o += pos_b
    struct.pack_into("<hhhIIIhhh", b, o, *e["vel"], e["hlc"] & 0xFFFFFFFF, ((e["klass"] << 24) | e["owner"]) & 0xFFFFFFFF,
                     0, 0, 0, e["yaw"])
    o += 28 - 4 + 4 - 4                   # vel 6 + hlc 4 + class_owner 4 + sub 4 + rot 6 = 24
    o = 4 + (2 if extra else 0) + pos_b + 24 + pad
    b[o:o + len(ext)] = ext; o += len(ext)
    avail = 100 - o; pl = payload_bytes(e, t, avail + over)
    if len(pl) > avail:
        if checked: raise PayloadTooLong(f"{name}: {len(pl)} bytes offered, {avail} fit")
        b[o:o + len(pl)] = pl            # unchecked: the packet grows past 100 bytes
    else:
        b[o:] = pl
    return bytes(b), avail

def decode(name, buf, pred=None):
    pos_b, extra, pad = LAYOUT[name]
    o = 4; mode = 0
    if extra: mode = buf[o]; o += 2
    slot = buf[o:o + pos_b]; o += pos_b
    vel = struct.unpack_from("<hhh", buf, o); hlc, co, sub = struct.unpack_from("<III", buf, o + 6)
    rot = struct.unpack_from("<hhh", buf, o + 18); o += 24 + pad
    ext = b""
    if name.startswith("R"):
        if mode == 0:
            nb = pos_b // 3
            pos = tuple(p + int.from_bytes(slot[i * nb:(i + 1) * nb], "little", signed=True) for i, p in enumerate(pred))
        else:
            ext = buf[o:o + 24 - pos_b]; pos = struct.unpack("<qqq", slot + ext)
    else:
        pos = struct.unpack("<qqq" if pos_b == 24 else "<iii", slot)
    o += len(ext)
    return dict(gid=struct.unpack_from("<I", buf, 0)[0], pos=pos, vel=vel, hlc=hlc, co=co, sub=sub, rot=rot, payload=buf[o:])

def same(e, t, d):
    return (d["gid"] == e["gid"] and d["pos"] == tuple(e["pos"]) and d["vel"] == tuple(e["vel"]) and d["hlc"] == (e["hlc"] & 0xFFFFFFFF)
            and d["co"] == (((e["klass"] << 24) | e["owner"]) & 0xFFFFFFFF) and d["sub"] == 0 and d["rot"] == (0, 0, e["yaw"])
            and d["payload"] == payload_bytes(e, t, len(d["payload"])))

def pred_sc(run, t, lag, i, sc):
    base = run[t - lag][i]["pos"]; out = []
    for ax in range(3):
        v = lambda tau: run[tau][i]["vel"][ax] * sc
        if lag == 1: s = v(t)
        else:
            s = 0.0
            for k in range(1, lag + 1):
                val, _ = P.makima_in_time(v(t - lag), v(t), v(t - lag - 1), v(t), v(t - lag - 2), v(t), k / lag,
                                          float(lag), -1.0, float(lag), -2.0, float(lag))
                s += val
        out.append(int(round(P.clamp_step(base[ax] + s, base[ax], lag))))
    return tuple(out)

def cum(per_tick): return [any(per_tick[:k]) for k in KS]

def lanes(run, lag):
    hold, lin = R.PRED["hold"], R.PRED["linear_pkt_vel"]
    sc = B.V_MAX / 32767
    i0 = next(i for i, e in enumerate(run[ET[0]]) if any(e["vel"]))
    assert pred_sc(run, ET[0], lag, i0, sc) == R.pred_makima_vel(run, ET[0], lag, i0)   # control: same function
    wrap, stale, f32, rate = [], [], [], []
    for t in ET:
        row = run[t]
        ok = False                                                # silent_wrap: escape disabled, R16, hold predictor
        for i, e in enumerate(row):
            buf, _ = encode("R16", e, t, lag, hold(run, t, lag, i), wrap=True)
            if decode("R16", buf, hold(run, t, lag, i))["pos"] != tuple(e["pos"]): ok = True; break
        wrap.append(ok)
        stale.append(any(lin(run, t, lag, i) != lin(run, t, lag + 1, i) for i in range(N)))
        mv = [i for i, e in enumerate(row) if any(e["vel"])]
        f32.append(any(pred_sc(run, t, lag, i, sc) != pred_sc(run, t, lag, i, F32(sc)) for i in mv))
        rate.append(any(pred_sc(run, t, lag, i, sc) != pred_sc(run, t, lag, i, sc * 3) for i in mv))
    e0 = run[ET[0]][0]
    try: encode("R24", e0, ET[0], lag, lin(run, ET[0], lag, 0), over=1); refused = False
    except PayloadTooLong: refused = True
    grown = len(encode("R24", e0, ET[0], lag, lin(run, ET[0], lag, 0), checked=False, over=1)[0]) != 100
    over = [refused and grown] * len(ET)
    return dict(silent_wrap=cum(wrap), stale_baseline=cum(stale), float32_scale=cum(f32), rate_scale=cum(rate), payload_overrun=cum(over))

def run_job(args):
    prob, jit, lag, seed = args
    t0 = time.time(); run = R.PROBLEMS[prob](seed, jit)
    out = dict(prob=prob, jitter_um=jit, lag_ticks=lag, seed=seed, players={}, fixed_clean={})
    for name in ("A_current", "A_pad", "B_i32_local"):
        ok = True
        try:
            for t in range(R.EVAL_FROM, R.T, 17):
                for e in run[t]:
                    buf, _ = encode(name, e, t, lag); ok &= len(buf) == 100 and same(e, t, decode(name, buf))
        except struct.error:
            ok = False       # the layout cannot hold this position (Int32 um past +-2147 m): refused by name, not wrapped
        out["fixed_clean"][name] = ok
    for pn in PREDS:
        fn = R.PRED[pn]; esc = {w: 0 for w in WIDTHS}; pk = 0; kmax = {}; mov = []; clean = {w: True for w in WIDTHS}
        for t in range(R.EVAL_FROM, R.T):
            preds = [fn(run, t, lag, i) for i in range(N)]
            for i, e in enumerate(run[t]):
                r = [a - b for a, b in zip(e["pos"], preds[i])]
                m = max(abs(x) for x in r); kmax[e["kind"]] = max(kmax.get(e["kind"], 0), m)
                if e["kind"]: mov.append(m)
                pk += 1
                for w in WIDTHS:
                    if not all(fits(x, 1 << (w - 1)) for x in r): esc[w] += 1
            if t % 17 == 0:
                for w in WIDTHS:
                    for i, e in enumerate(run[t]):
                        buf, _ = encode(f"R{w}", e, t, lag, preds[i])
                        clean[w] &= len(buf) == 100 and same(e, t, decode(f"R{w}", buf, preds[i]))
        mov.sort()
        out["players"][pn] = dict(escapes=esc, packets=pk, clean=clean, kind_max_um={str(k): v for k, v in kmax.items()},
                                  p99_um=mov[int(0.99 * len(mov))] if mov else 0, p999_um=mov[int(0.999 * len(mov))] if mov else 0)
    out["lanes"] = lanes(run, lag)
    out["wall_s"] = round(time.time() - t0, 1)
    return out

def control_cubic_overflows():
    """makima_cubic at lag 30 on the 30 Hz clip must overflow R16, or the silent_wrap lane cannot see overflow."""
    res = []
    for s in SEEDS[:3]:
        run = R.PROBLEMS["clip_root_30Hz"](s, 100); fn = R.PRED["makima_cubic"]; n = 0
        for t in ET[:4]:
            n += sum(not all(fits(a - b, 1 << 15) for a, b in zip(e["pos"], fn(run, t, 30, i))) for i, e in enumerate(run[t]))
        res.append(n)
    return res

def main():
    t0 = time.time()
    jobs = [(p, j, l, s) for p, j, l in LEVELS for s in SEEDS]
    with Pool(8) as pool: rows = pool.map(run_job, jobs, chunksize=1)
    lv = {}
    for r in rows: lv.setdefault((r["prob"], r["jitter_um"], r["lag_ticks"]), []).append(r)
    report = []
    for key, rs in lv.items():
        ln, calibrated = {}, True
        for lane in ("silent_wrap", "stale_baseline", "float32_scale", "rate_scale", "payload_overrun"):
            counts = [sum(r["lanes"][lane][i] for r in rs) for i in range(len(KS))]
            cur, rate, hist = 0, counts[0], [(KS[0], counts[0])]
            for i in (1, 2):
                if rate < RUNS:
                    hist.append((KS[i], counts[i]))
                    if counts[i] > rate: cur, rate = i, counts[i]
            ln[lane] = dict(found_of_8=rate, ticks_examined=KS[cur], tried=hist, regret=(1 if rate >= 1 else 0) - rate / RUNS)
            calibrated &= rate == RUNS
        pl = {}
        for pn in PREDS:
            for w in WIDTHS:
                s = sum(r["players"][pn]["clean"][w] and r["players"][pn]["escapes"][w] <= ESC_LIMIT * r["players"][pn]["packets"] for r in rs)
                esc = sum(r["players"][pn]["escapes"][w] for r in rs); pk = sum(r["players"][pn]["packets"] for r in rs)
                pay = ((pk - esc) * PAYLOAD[f"R{w}"] + esc * KEY_PAYLOAD) / pk
                pl[f"{pn}+R{w}"] = dict(success_of_8=s, regret=(1 if s >= 1 else 0) - s / RUNS, keyframe_rate_pct=round(100 * esc / pk, 3),
                                        mean_payload_bytes=round(pay, 2))
        km = {pn: {k: max(r["players"][pn]["kind_max_um"].get(k, 0) for r in rs) for k in sorted({k for r in rs for k in r["players"][pn]["kind_max_um"]})} for pn in PREDS}
        report.append(dict(level="|".join(map(str, key)), calibrated=calibrated, lanes=ln, players=pl,
                           fixed_layouts=dict(zip(("A_current", "A_pad", "B_i32_local"),
                                                  [(PAYLOAD[n], all(r["fixed_clean"][n] for r in rs)) for n in ("A_current", "A_pad", "B_i32_local")])),
                           kind_max_residual_um=km,
                           p999_residual_um={pn: max(r["players"][pn]["p999_um"] for r in rs) for pn in PREDS},
                           p99_residual_um={pn: max(r["players"][pn]["p99_um"] for r in rs) for pn in PREDS}))
    json.dump(dict(payload_bytes=PAYLOAD, keyframe_payload_bytes=KEY_PAYLOAD, keyframe_limit_pct=100 * ESC_LIMIT, seeds=SEEDS,
                   control_makima_cubic_lag30_R16_overflow_packets=control_cubic_overflows(),
                   wall_s=round(time.time() - t0), levels=report), open("results_payload.json", "w"), indent=1)
    print("done", round(time.time() - t0), "s")

if __name__ == "__main__":
    main()
