#!/bin/sh
# Record the real mm.elf for 8 seeds in parallel (256 entities, 600 controller steps at 60 Hz each).
#   ENGINE=<dir with godot.macos.editor.double.arm64 and proj/> ELF=<mm.elf> CLIPS=<dirA:dirB> ./record_mm.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$here/mm_data"
cd "$ENGINE"
for k in 0 1 2 3 4 5 6 7; do
  seed=$((20261107 + 11 * k))
  ./godot.macos.editor.double.arm64 --headless --path proj --script mm_record.gd ++ \
    --elf="$ELF" --clips="$CLIPS" --out="$here/mm_data/mm_$seed.bin" --seed=$seed --entities=256 --ticks=600 \
    2>&1 | grep -E 'MM-RECORD' | sed "s/^/seed $seed: /" &
done
wait
