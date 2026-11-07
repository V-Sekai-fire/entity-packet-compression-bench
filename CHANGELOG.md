# Changelog

- 2026-11-07: fixed 100 byte packet objective (payload bytes). Frontier rule restated from RFD 2427, 2436-2440 and logbook 4.41 (RFD 2309 and 2323 are deleted). mm.elf driven headless; 16 levels calibrated; R24 (61 B payload) held everywhere, R16 (64 B) only at lag 6 ticks.
- 2026-11-07: loader fix: run_circle_left loop range lies outside its clip, so the whole clip is used; roundtable cell clip_root_30Hz, 100 um, lag 6, makima_vel_int + varint_prefix reproduces at 17.41 B per update against 17.40 committed. Clip jitter is now seeded.
- 2026-11-07: far-origin levels (29 AU, Int64 edge): R24 held 8 of 8 in all 8 levels; B_i32_local invalid (zone exceeds +-2147 m).
