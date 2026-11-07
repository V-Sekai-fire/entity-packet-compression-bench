# Roundtable: predictor × container, frontier rounds

Unit of every score: zstd bytes per entity update (lower wins). Mean over 5 seeds ± standard deviation over seeds.
Reproduce: `.venv/bin/python bench_roundtable.py` (about 150 s on 8 workers; needs the clip data, see Conditions).

## Conditions

- 256 entities, 200 ticks per run, first 70 ticks not scored. Ack lag 1, 6 and 30 ticks. Jitter 0 or 100 µm on moving entities.
- Tick rate: the synthetic rows assume 60 Hz. The clip rows are 30 Hz (the original Kimodo samples) and 60 Hz.
  `Codec.lean` has `V_MAX_UM_PER_TICK` = 500000, which is 10 m/s at **20 Hz**. Nothing here ran at 20 Hz, so the lag in
  milliseconds is not the 100 ms the first README claimed for lag 6.
- Clip rows are the **root trajectory of the motion-matching clip library** (joint 0 origin, metres, converted to µm) from
  V-Sekai-fire/interactor-motion-matching-guest `data/kimodo-locomotion`, commit af39b41, strung together by a state graph
  (idle, start_walk, walk, stop_walk, start_run, run, run_circle_left/right, stop_run). It is clip root motion, **not** mm.elf controller output.
  The clip data is not vendored (166 MB); set `CLIPS` to a sparse checkout of that directory.
- The 60 Hz clips were upsampled from 30 Hz **with makima**, so a makima predictor on them is circular. The 30 Hz rows are the primary clip result.
- Zero payload (best case). Dictionaries trained on a different seed, but the same clip library (not split by clip).

## Rule, written before the run

Winner of a cell: lowest mean over seeds. Any player within 2 × the seed standard deviation of the best (floor 0.05 bytes)
ties, and the simpler player wins. Round 1 ran the baseline family on every cell. A cell is on the frontier when the champion
is more than 5% above the lower bound (oracle residuals in a fixed slot with a prefix). Round 2 ran the new players on frontier cells only.

## Result

Round 1 put **all 18 cells** on the frontier, so round 2 ran everywhere (2160 scores). Winner per cell:

| problem | jitter µm | lag ticks | winner | bytes per update | abs batch (rung 0) | abs + previous-snapshot prefix | lower bound |
|---|---|---|---|---|---|---|---|
| clip_root_30Hz | 0 | 1 | linear_pkt_vel + varint_prefix | 11.90 ± 0.07 | 23.97 | 19.42 | 9.59 |
| clip_root_30Hz | 0 | 6 | linear_pkt_vel + varint_prefix | 17.31 ± 0.17 | 23.97 | 21.14 | 9.89 |
| clip_root_30Hz | 0 | 30 | hold + varint_prefix | 19.32 ± 0.14 | 23.97 | 21.82 | 10.17 |
| clip_root_30Hz | 100 | 1 | linear_pkt_vel + varint_prefix | 15.55 ± 0.06 | 25.12 | 18.72 | 9.59 |
| clip_root_30Hz | 100 | 6 | makima_vel_int + varint_prefix | 17.40 ± 0.12 | 25.12 | 20.85 | 9.89 |
| clip_root_30Hz | 100 | 30 | hold + varint_prefix | 19.63 ± 0.14 | 25.12 | 22.25 | 10.17 |
| clip_root_60Hz_makima_upsampled | 0 | 1 | linear_pkt_vel + varint_prefix | 11.30 ± 0.06 | 23.71 | 18.25 | 8.92 |
| clip_root_60Hz_makima_upsampled | 0 | 6 | makima_vel_int + varint_prefix | 16.10 ± 0.06 | 23.71 | 20.74 | 9.59 |
| clip_root_60Hz_makima_upsampled | 0 | 30 | makima_vel_int + varint_prefix | 18.34 ± 0.09 | 23.71 | 21.72 | 9.87 |
| clip_root_60Hz_makima_upsampled | 100 | 1 | linear_pkt_vel + varint_prefix | 15.09 ± 0.10 | 24.95 | 17.38 | 8.92 |
| clip_root_60Hz_makima_upsampled | 100 | 6 | makima_vel_int + varint_prefix | 16.56 ± 0.07 | 24.96 | 20.16 | 9.59 |
| clip_root_60Hz_makima_upsampled | 100 | 30 | makima_vel_int + varint_prefix | 18.70 ± 0.10 | 24.95 | 21.84 | 9.87 |
| synth_mover_heavy_60Hz | 100 | 1 | linear_pkt_vel + varint_prefix | 14.23 ± 0.34 | 26.87 | 17.72 | 7.45 |
| synth_mover_heavy_60Hz | 100 | 6 | makima_vel_int + varint_prefix | 14.73 ± 0.34 | 26.87 | 18.60 | 7.76 |
| synth_mover_heavy_60Hz | 100 | 30 | hold + varint_prefix | 17.56 ± 0.42 | 26.87 | 19.35 | 8.18 |
| synth_original_60Hz | 100 | 1 | hold + varint_prefix | 6.44 ± 0.34 | 21.13 | 7.34 | 3.68 |
| synth_original_60Hz | 100 | 6 | hold + varint_prefix | 6.78 ± 0.36 | 21.13 | 7.82 | 3.86 |
| synth_original_60Hz | 100 | 30 | hold + varint_prefix | 7.08 ± 0.38 | 21.13 | 8.02 | 4.00 |

- **The container decided the result, not the predictor.** The winner is a varint residual with the previous acked snapshot as a zstd
  raw-content prefix in all 18 cells. It beats the same residual batched without a prefix by 2 to 4 bytes per update, and beats the
  absolute-position prefix by 0.5 to 4 bytes (clip rows, lag 6: 17.10 against 21.14).
- **Makima velocity integration** (a makima curve through the velocity samples at ticks L−2, L−1, L and the packet's own velocity at the target tick,
  summed over the lag) wins at lag 6 on all four clip rows and at lag 30 on the 60 Hz clip rows. The margin over the linear predictor is small:
  0.2 to 0.8 bytes (1 to 5%). At lag 1 it is identical to linear by construction. At lag 30 on the 30 Hz clip rows and on the synthetic rows it ties
  with `hold` inside the seed spread, so the simpler player wins the cell.
- **Makima end-tangent extrapolation** never wins. The cubic evaluated past its segment (`makima_cubic`) is an invalid rung beyond lag 1:
  weight 1 + lag lies outside [0, 1], and its residual reached 1.3 m at lag 30 in the earlier run. It stays as a negative control only.
- The winners sit well above the lower bound on the clip rows (for example 17.10 against 9.89 bytes at lag 6, jitter 0 µm). The prediction
  residual is not the whole gap; I did not separate the other causes.

## Planted defect: a client holds another dictionary version

256 records, compressed with dictionary A and decoded with dictionary B:

- Frame header carries the dictionary id: 256 of 256 refused, 0 decoded wrong.
- Dictionary id off (what `bench.py` uses): 254 of 256 refused, **2 decoded wrong without an error**.

So the wire needs a dictionary id or a version field. The prefix container has the same exposure through the baseline snapshot, which needs a tick id (`hlc` could carry it).

## Not established

- The Python makima is a port, not the Lean-generated kernel. The encoder and decoder must compute the identical prediction, so a floating-point
  difference of 1 µm would compound through the baselines. A winner needs integer or fixed-point kernels and a cross-language test before shipping.
- No 20 Hz run, no constant-payload run in the roundtable, no CPU cost, no packet loss, no per-client state cost.
- Dictionary leakage across clips is not controlled (same clip library in training and test).
