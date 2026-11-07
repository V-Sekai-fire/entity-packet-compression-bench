#!/usr/bin/env python3
"""Keyframe rates of the Lean-emitted integer Makima kernel (entity_predict.h, called through ctypes)
against the float port makima_vel_int, on the recorded mm.elf motion. Same rule as bench_payload:
a residual that does not fit the slot is a keyframe."""
import ctypes, os
import bench as B, bench_payload as F, bench_roundtable as R
lib = ctypes.CDLL(os.path.abspath("libintkernel.dylib"))
lib.wrap_disp.restype = ctypes.c_int64
lib.wrap_disp.argtypes = [ctypes.c_int32] * 5 + [ctypes.c_int64]
for hz in (60, 30, 20):
    for lag in (6, 30):
        tot = {"int": [0, 0, 0], "float": [0, 0, 0]}; mx = 0; n = 0
        for s in F.SEEDS:
            run = R.PROBLEMS[f"mm_elf_{hz}Hz"](s, 100)
            for t in range(R.EVAL_FROM, R.T):
                for i, e in enumerate(run[t]):
                    L = t - lag
                    pi = []
                    for ax in range(3):
                        d = lib.wrap_disp(run[L-2][i]["vel"][ax], run[L-1][i]["vel"][ax], run[L][i]["vel"][ax], e["vel"][ax], lag, B.V_MAX)
                        pi.append(run[L][i]["pos"][ax] + d)
                    pf = R.PRED["makima_vel_int"](run, t, lag, i)
                    mx = max(mx, max(abs(a - b) for a, b in zip(pi, pf)))
                    n += 1
                    for k, p in (("int", pi), ("float", pf)):
                        r = [a - b for a, b in zip(e["pos"], p)]
                        tot[k][0] += any(not -32768 <= x < 32768 for x in r)
                        tot[k][1] += any(not -(1 << 23) <= x < (1 << 23) for x in r)
        print(f"mm_elf_{hz}Hz lag {lag} ticks: R16 keyframes int {100*tot['int'][0]/n:.3f}% float {100*tot['float'][0]/n:.3f}% | R24 int {100*tot['int'][1]/n:.3f}% | max |int-float| position {mx} um over {n} packets")
