# mm.elf recording provenance

- Artifact: `mm-elf-af39b417896df7d72a0866e7dbcfc4a4ada84e88`, id 13296907548, workflow run 41776033331, branch `main`,
  head `af39b417896df7d72a0866e7dbcfc4a4ada84e88`, created 2026-11-04T04:56:11Z, repo V-Sekai-fire/interactor-motion-matching-guest.
- `mm.elf` sha256 `f493ae7c0f1706cb6a2895354523cd601475b23fac1d81a5d8a10997d2ba6c4c` (972416 bytes, RISC-V 64, statically linked). Not committed.
- Engine: V-Sekai-fire/entities-zone release `v0.0.0-20261106225526-d632cdf2be4c`, `godot.macos.editor.double.arm64`
  sha256 `e53504dce715a0adcf0602530a38c5b10722758c5a14e3a37c2ed21e383de336` (Godot 4.8.beta double, Sandbox class built in). Run headless.
- Clips: 75 (kimodo-locomotion 54 + o3de-motionmatching 21) from the same commit af39b41; `BAKED clips 75 poses 119888 dims 30`.
- Driver: `mm_record.gd` (sandbox calls `mm_add_clip`, `mm_bake`, `mm_reset`, `mm_step`), launched by `record_mm.sh`.
- Input: seeded stick script per entity (30% idle only; else idle 1-4 s, walk 1.0-1.6 m/s, run 3-5 m/s, crouch walk 0.6-1.0 m/s;
  heading drift sd 0.01 rad per step). 256 entities, 600 controller steps at 60 Hz (dt = 1/60 s), 8 seeds 20261107 + 11k.
- Determinism: the same seed (20261107, 8 entities, 600 steps) run twice gave identical sha256 `e2ecf2f845f4acc91eea9263f81a1dabe18f0c2bbacaa1f2cd6eab42a7645b1e`.
- Sanity: walk/run entities reached 1.5 to 4.45 m/s mean-to-max speed; idle entities show up to 0.34 m/s of sway (max).
- Record format (`mm_<seed>.bin`, gitignored, regenerable): per entity, per step, 4 float32 = char_x (m), char_z (m), char_yaw (rad), clip index.
- Wire levels are decimated 60 -> 30 -> 20 Hz (every 1st, 2nd, 3rd step). Packet `vel` is the position difference per wire tick (the controller reports none).
  Jitter 100 um on moving entities. Zone offset added in double after the guest.
