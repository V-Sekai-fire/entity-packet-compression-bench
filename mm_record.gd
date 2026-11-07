extends SceneTree
# Drives the real mm.elf (interactor-motion-matching-guest) with seeded stick input and records the character.
#   godot --headless --path <proj> --script mm_record.gd ++ --elf=<mm.elf> --clips=<dirA:dirB> --out=<file> \
#         --seed=<int> --entities=<n> --ticks=<n>
# Output: per entity, per tick (60 Hz controller steps), 4 float32: char_x_m, char_z_m, char_yaw_rad, clip_index.
# Input per entity: 30% idle only; the rest draw segments: idle 1-4 s, walk 1.0-1.6 m/s, run 3-5 m/s,
# crouch walk 0.6-1.0 m/s (when the database has crouch poses); heading drifts 0.01 rad per tick (sd).
# The same seed must give the same file byte for byte (checked by the caller).

func _arg(n: String, d: String = "") -> String:
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--%s=" % n):
			return a.trim_prefix("--%s=" % n)
	return d


func _process(_d: float) -> bool:
	var sb = ClassDB.instantiate("Sandbox")
	sb.set("allocations_max", 1000000)
	sb.set("references_max", 4096)
	sb.set("memory_max", 512)
	sb.set("execution_timeout", 0)
	sb.load_buffer(FileAccess.get_file_as_bytes(_arg("elf")))
	var added := 0
	for dir in _arg("clips").split(":", false):
		var keys := DirAccess.get_directories_at(dir)
		keys.sort()
		for k in keys:
			var p_dir: String = dir.path_join(k)
			if not FileAccess.file_exists(p_dir.path_join("transforms.f32")):
				continue
			var fps := 30.0
			var names := PackedStringArray()
			for line in FileAccess.get_file_as_string(p_dir.path_join("skeleton.txt")).split("\n", false):
				if line.begins_with("frames "):
					fps = float(line.split(" ")[3])
				elif not line.begins_with("model ") and not line.begins_with("prompt "):
					names.append(line)
			var parents := PackedInt32Array()
			for p in FileAccess.get_file_as_string(p_dir.path_join("parents.txt")).split("\n", false):
				parents.append(int(p))
			var xf := FileAccess.get_file_as_bytes(p_dir.path_join("transforms.f32")).to_float32_array()
			var loop := 1 if FileAccess.file_exists(p_dir.path_join("loop.txt")) else 0
			sb.vmcall("mm_add_clip", k, "\n".join(names), parents, xf, fps, loop)
			added += 1
	print("CLIPS ", added)
	print(sb.vmcall("mm_bake"))
	var modes := str(sb.vmcall("mm_modes"))
	print(modes)
	var crouch_ok := false
	for line in modes.split("\n", false):
		var w := line.split(" ")
		if w[0] == "crouch" and int(w[1]) > 0:
			crouch_ok = true
	var seed_base := int(_arg("seed", "1"))
	var n_ent := int(_arg("entities", "256"))
	var n_ticks := int(_arg("ticks", "600"))
	var out := FileAccess.open(_arg("out"), FileAccess.WRITE)
	var dt := 1.0 / 60.0
	var t0 := Time.get_ticks_usec()
	for e in n_ent:
		var rng := RandomNumberGenerator.new()
		rng.seed = seed_base * 100003 + e
		var idle_only := rng.randf() < 0.3
		sb.vmcall("mm_reset", 0.0, 0.0, 0.0, 0)
		var heading := rng.randf() * TAU
		var left := 0
		var speed := 0.0
		var mode := 0
		var rec := PackedFloat32Array()
		rec.resize(n_ticks * 4)
		for i in n_ticks:
			if left <= 0:
				if idle_only:
					speed = 0.0
					mode = 0
					left = 240
				else:
					var r := rng.randf()
					mode = 0
					if r < 0.25:
						speed = 0.0
						left = int(rng.randf_range(1.0, 4.0) * 60.0)
					elif r < 0.70:
						speed = rng.randf_range(1.0, 1.6)
						left = int(rng.randf_range(2.0, 6.0) * 60.0)
					elif r < 0.90:
						speed = rng.randf_range(3.0, 5.0)
						left = int(rng.randf_range(1.0, 4.0) * 60.0)
					else:
						speed = rng.randf_range(0.6, 1.0)
						mode = 1 if crouch_ok else 0
						left = int(rng.randf_range(2.0, 4.0) * 60.0)
					heading += rng.randfn(0.0, 0.6)
			left -= 1
			if speed > 0.0:
				heading += rng.randfn(0.0, 0.01)
			var o: PackedFloat32Array = sb.vmcall("mm_step", dt, sin(heading) * speed, cos(heading) * speed, NAN, mode, 0)
			rec[i * 4] = o[2]
			rec[i * 4 + 1] = o[3]
			rec[i * 4 + 2] = o[4]
			rec[i * 4 + 3] = o[0]
		out.store_buffer(rec.to_byte_array())
	out.close()
	print("MM-RECORD entities=%d ticks=%d us_per_step=%.1f" % [n_ent, n_ticks, float(Time.get_ticks_usec() - t0) / (n_ent * n_ticks)])
	quit(0)
	return true
