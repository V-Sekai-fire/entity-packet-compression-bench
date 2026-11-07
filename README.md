# entity-packet-compression-bench

Does Int64 position cost more than Int32 once zstd and a dictionary have seen it, and does a delta against an acked snapshot beat both?

The wire layout is the 100-byte entity packet from
[`EntityPacket/Codec.lean`](https://github.com/V-Sekai-fire/transport-iouring-feed-server/blob/dev/entity-server/datasources/entity-packet/EntityPacket/Codec.lean)
(little-endian; gid@0, pos 3×i64@4, vel 3×i16@28, hlc@40, class/owner@44, sub@48, rot 3×i16@52, 42-byte payload@58).

**The data is synthetic.** Real payload contents, real motion and real ack behaviour are not measured here. Treat the numbers as a comparison between encodings under stated conditions, not as production sizes.

## Reproduce

```
uv venv .venv && uv pip install --python .venv/bin/python zstandard
.venv/bin/python bench.py      # about 6 s on the machine below; writes results.csv and results.json
```

Every variant is decoded back and compared with the generated truth (`verify()`), so a broken encoder fails the run instead of producing a number.

## Conditions

- macOS 26.6.2 arm64, Python 3.14.7, python-zstandard 0.25.0 (zstd 1.5.7), level 3.
- 256 entities, all sent every tick, 60 Hz. 70% static, 20% walkers at 1.4 m/s, 10% at 10 m/s. Zone ±1 km, so Int32 µm fits.
- Test: 260 ticks (seed 20261107). Dictionary training: a different population (seed +1), 200 ticks. No evaluation data is used for training.
- Jitter is Gaussian noise on the reported position of moving entities only: 0, 100 or 1000 µm.
- Payload policy: `zero` (42 zero bytes) or `const` (per-entity random constant). `hlc` advances every tick in both.
- Zstd frames carry no content size, checksum or dictionary id.
- Batch means one frame per tick holding all 256 records. Packet means one frame per record, sampled every 5th tick.
- Trained dictionaries: 64 KB for batch, 8 KB for packet.
- Delta baseline: the snapshot 6 ticks earlier. The bench assumes 60 Hz (6 ticks = 100 ms), but `V_MAX_UM_PER_TICK` = 500000 µm per tick is 10 m/s at 20 Hz, where 6 ticks = 300 ms. The tick rate is unconfirmed. The lag sweep is 1, 3, 6, 12 and 30 ticks.
- See `ROUNDTABLE.md` for the predictor x container roundtable over motion-matching clip root motion (`bench_predict.py`, `bench_roundtable.py`).

## Variants

- `A_i64_far`: the current wire, with the zone at a far world offset so the high bytes are constant, not sign extension.
- `A_i64_origin0`: the same wire with the zone around 0.
- `B_i32_local`: Int32 µm, zone-local, 88 bytes.
- `C_f64_m_far`: doubles in metres at the far offset.
- `D_i32_delta_slot`: fixed-slot Int32 delta against the acked snapshot, 88 bytes.
- `E_varint_delta`: zigzag-varint delta against the acked snapshot.
- `F_varint_deadreckon`: zigzag-varint residual against `base + vel × lag`.
- `G_i64_far_prefix_prev_ack`: the unchanged Int64 wire, with the previous acked snapshot (or the same entity's previous packet) as a zstd raw-content dictionary.

## Results

Bytes per entity update. Jitter 100 µm, zero payload, lag 6. Raw is before zstd.

| variant | raw | batch | batch + 64k dict | packet | packet + 8k dict |
|---|---|---|---|---|---|
| A_i64_far | 100 | 21.17 | 20.77 | 52.24 | 40.15 |
| A_i64_origin0 | 100 | 23.04 | 22.43 | 51.73 | 41.72 |
| B_i32_local | 88 | 20.08 | 19.86 | 48.04 | 41.78 |
| C_f64_m_far | 100 | 25.65 | 25.20 | 56.46 | 45.78 |
| D_i32_delta_slot | 88 | 11.53 | 12.07 | 38.07 | 30.35 |
| E_varint_delta | 80.3 | 11.44 | 12.14 | 36.52 | 30.76 |
| F_varint_deadreckon | 79.8 | 11.05 | 11.54 | 36.00 | 30.36 |
| G_i64_far_prefix_prev_ack | 100 | 7.88 (prefix) | | 27.37 (prefix, one packet) | |

With a per-entity constant payload every number rises to about 53–64 in batch mode, because 42 random-looking bytes per entity cost about that much to send. G is the exception: it stays at about 8 (batch) because the previous snapshot already contains the payload. Full tables for every jitter, payload and lag are in `results.csv`.

### What the numbers say

- **Int64 vs Int32 is a small difference after zstd:** 21.2 vs 20.1 bytes in batch (about 5%). The raw 12-byte saving mostly disappears, because the high bytes are constant or sign extension and zstd already removes them. Rescaling the world for this is not justified by these numbers.
- **Doubles are worse than Int64** here (25.7 vs 21.2), not equal. This is the opposite of "same compressibility" for moving entities.
- **A delta against an acked snapshot roughly halves the size** (about 11 bytes). Dead reckoning adds only about 0.4 byte over a plain delta in this mix, because 70% of entities do not move.
- **The best result keeps the Int64 wire unchanged**: use the previous acked snapshot as a zstd prefix, 7.9 bytes in batch. It needs no varints and no layout change. The cost is the same as for deltas, per-client state and an ack channel.
- **A trained dictionary does not help batch frames** (it is slightly worse for the delta variants) and helps single packets (52 → 40). A dictionary matters when you compress records one at a time, not when you batch a tick.
- **The ack lag matters little here:** in batch mode, E goes from 11.35 (lag 1) to 11.64 (lag 30). The static majority hides it. A scene with more movers would show more.
- `A_i64_origin0` is worse than `A_i64_far` by about 2 bytes in batch. I did not investigate why.
- The earlier estimate of "1–2 bytes per axis" for moving entities was too optimistic; the walker case needs about 3 bytes per axis for a plain delta.

## Not measured

- Real payload contents and real trajectories.
- Packet loss, keyframes after a missed ack, and CPU cost.
- Per-client state memory for deltas and prefixes, and what broadcasting the same bytes to many clients loses.
- Zstd levels above 3 and other dictionary sizes.

## Licence

MIT. See `LICENSE`.
