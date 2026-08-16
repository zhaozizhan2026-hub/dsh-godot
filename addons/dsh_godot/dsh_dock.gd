@tool
extends VBoxContainer

## In-editor frontend for the dsh-godot Python service.
##
## The dock starts the local service (project .venv), connects over
## WebSocket, displays chat / screenshots / tool activity, and rescans the
## Godot filesystem whenever the agent writes project files.

var plugin: EditorPlugin = null

var _ws := WebSocketPeer.new()
var _service_pid: int = 0
var _retry_msec: int = 1000
var _last_attempt_msec: int = 0
var _busy: bool = false
var _stream_content := ""
var _stream_reasoning := ""
var _stream_content_label: Label = null
var _stream_reasoning_box: VBoxContainer = null
var _stream_reasoning_content: Label = null

var _api_key_edit: LineEdit
var _save_key_button: Button
var _status_label: Label
var _mode_label: Label
var _messages_box: VBoxContainer
var _scroll: ScrollContainer
var _prompt_edit: LineEdit
var _send_button: Button
var _stop_button: Button
var _start_button: Button
var _full_button: Button
var _eco_button: Button
var _model_option: OptionButton
var _screenshot_button: Button
var _clear_button: Button


func _ready() -> void:
	_build_ui()
	if _auto_start():
		start_service()
		_connect_to_service()


func _build_ui() -> void:
	custom_minimum_size = Vector2(380, 520)
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL

	var title := Label.new()
	title.text = "DSH Godot — DeepSeek coding dock"
	title.add_theme_font_size_override("font_size", 15)
	add_child(title)

	_status_label = Label.new()
	_status_label.text = "service: not started"
	_status_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(_status_label)

	_mode_label = Label.new()
	_mode_label.text = "Mode: Default (Eco)"
	_mode_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(_mode_label)

	var key_row := HBoxContainer.new()
	key_row.add_theme_constant_override("separation", 6)
	add_child(key_row)

	var key_label := Label.new()
	key_label.text = "API Key"
	key_row.add_child(key_label)

	_api_key_edit = LineEdit.new()
	_api_key_edit.secret = true
	_api_key_edit.placeholder_text = "sk-... (saved to .env)"
	_api_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	key_row.add_child(_api_key_edit)

	_save_key_button = Button.new()
	_save_key_button.text = "Save Key"
	_save_key_button.pressed.connect(_save_api_key)
	key_row.add_child(_save_key_button)

	var controls := HBoxContainer.new()
	controls.add_theme_constant_override("separation", 6)
	add_child(controls)

	_start_button = Button.new()
	_start_button.text = "Start dsh"
	_start_button.pressed.connect(_on_start_pressed)
	controls.add_child(_start_button)

	_full_button = Button.new()
	_full_button.text = "Full Power"
	_full_button.tooltip_text = "Long thinking + web + no hard limits"
	_full_button.pressed.connect(_on_full_power_pressed)
	controls.add_child(_full_button)

	_eco_button = Button.new()
	_eco_button.text = "Eco"
	_eco_button.tooltip_text = "Disable thinking/web for speed and lower cost"
	_eco_button.pressed.connect(_on_eco_pressed)
	controls.add_child(_eco_button)

	_model_option = OptionButton.new()
	_model_option.add_item("deepseek-v4-pro")
	_model_option.add_item("deepseek-v4-flash")
	_model_option.tooltip_text = "Choose DeepSeek model"
	_model_option.item_selected.connect(_on_model_selected)
	controls.add_child(_model_option)

	_screenshot_button = Button.new()
	_screenshot_button.text = "Screenshot"
	_screenshot_button.pressed.connect(_on_screenshot_pressed)
	controls.add_child(_screenshot_button)

	_clear_button = Button.new()
	_clear_button.text = "Clear"
	_clear_button.pressed.connect(_on_clear_pressed)
	controls.add_child(_clear_button)

	_scroll = ScrollContainer.new()
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(_scroll)

	_messages_box = VBoxContainer.new()
	_messages_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_messages_box.add_theme_constant_override("separation", 8)
	_scroll.add_child(_messages_box)

	var input_row := HBoxContainer.new()
	input_row.add_theme_constant_override("separation", 6)
	add_child(input_row)

	_prompt_edit = LineEdit.new()
	_prompt_edit.placeholder_text = "Ask dsh to write code, e.g. create scripts/player.gd"
	_prompt_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_prompt_edit.text_submitted.connect(func(_text: String) -> void: _send_prompt())
	input_row.add_child(_prompt_edit)

	_send_button = Button.new()
	_send_button.text = "Send"
	_send_button.pressed.connect(_send_prompt)
	input_row.add_child(_send_button)

	_stop_button = Button.new()
	_stop_button.text = "Stop"
	_stop_button.tooltip_text = "Stop the current dsh turn and tool execution"
	_stop_button.disabled = true
	_stop_button.pressed.connect(_on_stop_pressed)
	input_row.add_child(_stop_button)


func cleanup() -> void:
	_ws.close()
	if _service_pid != 0:
		OS.kill(_service_pid)
		_service_pid = 0


func _process(_delta: float) -> void:
	if _service_pid != 0 and not OS.is_process_running(_service_pid):
		_service_pid = 0
		_set_status("service stopped", Color(1.0, 0.6, 0.3))

	_ws.poll()
	var state := _ws.get_ready_state()
	match state:
		WebSocketPeer.STATE_OPEN:
			_drain_ws()
		WebSocketPeer.STATE_CLOSED:
			_maybe_retry_connect()
		_:
			pass


func _send_prompt() -> void:
	var prompt := _prompt_edit.text.strip_edges()
	if prompt.is_empty() or _busy:
		return
	if _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		_append_text("System", "Service not connected. Click \"Start dsh\" first.", Color(1.0, 0.5, 0.4))
		return
	_busy = true
	_send_button.disabled = true
	_stop_button.disabled = false
	_append_text("You", prompt, Color(0.55, 0.82, 1.0))
	_send_json({"type": "chat", "prompt": prompt})
	_prompt_edit.clear()


func _on_start_pressed() -> void:
	if _service_pid == 0 or not OS.is_process_running(_service_pid):
		start_service()
	_connect_to_service()


func _save_api_key() -> void:
	var key := _api_key_edit.text.strip_edges()
	if key.is_empty():
		_append_text("System", "Paste your DEEPSEEK_API_KEY first.", Color(1.0, 0.55, 0.45))
		return
	var path := "res://.env"
	var new_lines := PackedStringArray()
	var replaced := false
	if FileAccess.file_exists(path):
		var file := FileAccess.open(path, FileAccess.READ)
		if file != null:
			for line in file.get_as_text().split("\n"):
				if line.strip_edges().begins_with("DEEPSEEK_API_KEY="):
					new_lines.append("DEEPSEEK_API_KEY=%s" % key)
					replaced = true
				else:
					new_lines.append(line)
			file.close()
	if not replaced:
		new_lines.append("DEEPSEEK_API_KEY=%s" % key)
	var out := FileAccess.open(path, FileAccess.WRITE)
	if out == null:
		_append_text("System", "Could not write .env: error %d" % FileAccess.get_open_error(), Color(1.0, 0.4, 0.4))
		return
	out.store_string("\n".join(new_lines))
	out.close()
	_api_key_edit.clear()
	_append_text("System", "API key saved to .env. Restarting dsh service...", Color(0.6, 1.0, 0.6))
	_restart_service()


func _restart_service() -> void:
	_ws.close()
	if _service_pid != 0:
		OS.kill(_service_pid)
		_service_pid = 0
	start_service()
	_connect_to_service()


func _on_full_power_pressed() -> void:
	_send_json({
		"type": "mode",
		"thinking": true,
		"web": true,
		"max_tokens": 0,
		"stream": true,
		"max_tool_turns": 0,
		"parallel_tools": true,
	})
	_mode_label.text = "Mode: Full Power PTC (thinking + web + parallel + unlimited)"
	_set_status("Full Power requested: live reasoning enabled", Color(1.0, 0.75, 0.55))


func _on_eco_pressed() -> void:
	_send_json({
		"type": "mode",
		"thinking": false,
		"web": false,
		"max_tokens": 4096,
		"stream": false,
		"max_tool_turns": 20,
		"parallel_tools": false,
	})
	_mode_label.text = "Mode: Eco (serial tools + 4096 tokens)"
	_set_status("Eco mode requested", Color(0.7, 1.0, 0.7))


func _on_model_selected(index: int) -> void:
	if _model_option == null or index < 0 or index >= _model_option.item_count:
		return
	_send_json({"type": "mode", "model": _model_option.get_item_text(index)})
	_set_status("Model switched to " + _model_option.get_item_text(index), Color(0.7, 1.0, 0.7))


func _on_stop_pressed() -> void:
	_send_json({"type": "stop"})
	_append_text("System", "Stop requested...", Color(1.0, 0.55, 0.45))
	_stop_button.disabled = true


func _on_screenshot_pressed() -> void:
	if _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		_append_text("System", "Connect the dsh service before taking a screenshot.", Color(1.0, 0.5, 0.4))
		return
	var viewport := get_viewport()
	if viewport == null:
		return
	var image := viewport.get_texture().get_image()
	var png_bytes := image.save_png_to_buffer()
	if png_bytes.is_empty():
		_set_status("screenshot capture failed", Color(1.0, 0.4, 0.4))
		return
	_send_json({
		"type": "screenshot",
		"image_base64": Marshalls.raw_to_base64(png_bytes),
		"mime": "image/png",
	})
	_set_status("screenshot sent to dsh service", Color(0.6, 1.0, 0.6))


func _on_clear_pressed() -> void:
	_send_json({"type": "clear"})
	for child in _messages_box.get_children():
		child.queue_free()
	_stream_content = ""
	_stream_reasoning = ""
	_stream_content_label = null
	_stream_reasoning_box = null
	_stream_reasoning_content = null
	_set_status("conversation cleared", Color(0.7, 0.7, 0.7))


# ---------------------------------------------------------------------------
# Service process + WebSocket
# ---------------------------------------------------------------------------

func start_service() -> void:
	if _service_pid != 0 and OS.is_process_running(_service_pid):
		return
	var root := ProjectSettings.globalize_path("res://")
	var python_path := _python_path()
	if python_path.is_empty() or not FileAccess.file_exists(python_path):
		_set_status(
			"Python not found. Set .venv/Scripts/python.exe in Editor Settings > DSH Godot > python_path",
			Color(1.0, 0.4, 0.4),
		)
		return
	var launcher := root.path_join("run_dsh_godot.py")
	if not FileAccess.file_exists(launcher):
		_set_status("run_dsh_godot.py not found", Color(1.0, 0.4, 0.4))
		return
	var args := PackedStringArray([
		launcher,
		"serve",
		"--project-root",
		root,
		"--host",
		_host(),
		"--port",
		str(_port()),
	])
	_service_pid = OS.create_process(python_path, args, false)
	if _service_pid <= 0:
		_set_status("Could not start dsh service", Color(1.0, 0.4, 0.4))
		return
	_set_status("Starting dsh service (pid %d)..." % _service_pid, Color(0.9, 0.75, 0.4))


func _connect_to_service() -> void:
	var url := "ws://%s:%d" % [_host(), _port()]
	if _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		return
	_ws.close()
	_ws = WebSocketPeer.new()
	var err := _ws.connect_to_url(url)
	if err != OK:
		_set_status("WebSocket connect failed: %s" % url, Color(1.0, 0.4, 0.4))
		return
	_last_attempt_msec = Time.get_ticks_msec()
	_set_status("Connecting %s ..." % url, Color(0.9, 0.75, 0.4))


func _maybe_retry_connect() -> void:
	var now := Time.get_ticks_msec()
	if now - _last_attempt_msec < _retry_msec:
		return
	if _service_pid != 0 and OS.is_process_running(_service_pid):
		_connect_to_service()
	elif _service_pid == 0:
		_set_status("service offline - click \"Start dsh\"", Color(1.0, 0.5, 0.4))


func _drain_ws() -> void:
	while _ws.get_available_packet_count() > 0:
		var packet := _ws.get_packet()
		var raw := packet.get_string_from_utf8()
		var parsed = JSON.parse_string(raw)
		if parsed is Dictionary:
			_on_event(parsed)


func _send_json(data: Dictionary) -> void:
	if _ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_ws.send_text(JSON.stringify(data))


# ---------------------------------------------------------------------------
# Server events
# ---------------------------------------------------------------------------

func _on_event(event: Dictionary) -> void:
	var kind := str(event.get("type", ""))
	match kind:
		"status":
			_set_status(str(event.get("message", "")), _level_color(str(event.get("level", ""))))
		"user":
			_append_text("You", str(event.get("text", "")), Color(0.55, 0.82, 1.0))
		"assistant":
			_append_text("dsh", str(event.get("text", "")), Color(0.85, 0.85, 0.85))
		"turn_start":
			_finish_stream_block()
			_append_text("Turn", str(event.get("text", "")), Color(0.55, 0.65, 0.7), true)
		"reasoning":
			_stream_reasoning += str(event.get("text", ""))
			_update_stream_labels()
		"content":
			_stream_content += str(event.get("text", ""))
			_update_stream_labels()
		"tool_call":
			_append_text("Tool", str(event.get("text", "")), Color(0.75, 0.9, 0.7), true)
		"tool_result":
			_append_collapsible("Result (click to expand)", str(event.get("text", "")), Color(0.65, 0.7, 0.6))
		"usage":
			_append_text("Usage", str(event.get("text", "")), Color(0.55, 0.7, 0.75), true)
		"salvage":
			_append_text("Salvage", str(event.get("text", "")), Color(0.95, 0.75, 0.55), true)
		"image":
			_display_image(str(event.get("path", "")))
		"files_changed":
			_on_files_changed(event.get("paths", []))
		"write_file":
			_on_write_file(event)
		"vision":
			_append_text("Vision", str(event.get("text", "")), Color(0.9, 0.7, 1.0))
		"mode":
			var mode_thinking := bool(event.get("thinking", false))
			var mode_web := bool(event.get("web", false))
			var mode_tokens := int(event.get("max_tokens", 0))
			var mode_turns := int(event.get("max_tool_turns", 0))
			var mode_parallel := bool(event.get("parallel_tools", true))
			var mode_model := str(event.get("model", "deepseek-v4-pro"))
			_mode_label.text = "Mode: %s (thinking %s, web %s, %s, %s, tools %s)" % [
				"Full Power PTC" if mode_thinking and mode_web and mode_parallel else ("Full Power" if mode_thinking and mode_web else ("Thinking" if mode_thinking else "Eco")),
				"on" if mode_thinking else "off",
				"on" if mode_web else "off",
				"no token limit" if mode_tokens == 0 else ("%d tokens" % mode_tokens),
				"no turn limit" if mode_turns == 0 else ("%d turns" % mode_turns),
				"parallel" if mode_parallel else "serial",
			]
			for index in range(_model_option.item_count):
				if _model_option.get_item_text(index) == mode_model:
					_model_option.select(index)
			_set_status("Runtime mode changed: " + mode_model, Color(0.7, 1.0, 0.7))
		"done":
			var text := str(event.get("text", ""))
			if _stream_content_label != null:
				_stream_content_label.text = "[dsh]\n%s" % text if not text.is_empty() else _stream_content_label.text
			elif not text.is_empty():
				_append_text("dsh", text, Color(0.85, 0.85, 0.85))
			_finish_stream_block()
			_set_status("Done: %d tool call(s)" % int(event.get("tool_calls", 0)), Color(0.6, 1.0, 0.6))
			_busy = false
			_send_button.disabled = false
			_stop_button.disabled = true
		"stopped":
			_finish_stream_block()
			_append_text("System", "Conversation stopped.", Color(1.0, 0.55, 0.45))
			_set_status("stopped", Color(1.0, 0.55, 0.45))
			_busy = false
			_send_button.disabled = false
			_stop_button.disabled = true
		"turn_timeout":
			_finish_stream_block()
			_append_text("Timeout", str(event.get("message", "")), Color(1.0, 0.6, 0.35))
			_set_status("turn timeout", Color(1.0, 0.6, 0.35))
			_busy = false
			_send_button.disabled = false
			_stop_button.disabled = true
		"error":
			_append_text("Error", str(event.get("message", "")), Color(1.0, 0.4, 0.4))
			_set_status("error", Color(1.0, 0.4, 0.4))
			_busy = false
			_send_button.disabled = false
			_stop_button.disabled = true


func _on_files_changed(paths: Variant) -> void:
	var fs := EditorInterface.get_resource_filesystem()
	if fs == null:
		return
	fs.scan()
	if not (paths is Array):
		return
	var root := ProjectSettings.globalize_path("res://")
	for raw_path in paths:
		var path := str(raw_path).replace("\\", "/")
		if path.begins_with(root):
			path = "res://" + path.substr(root.length()).lstrip("/")
		var res := ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_REPLACE)
		if res is Script:
			EditorInterface.edit_script(res)
	_append_text(
		"File",
		"Wrote %d file(s). Godot filesystem refreshed." % int((paths as Array).size()),
		Color(0.6, 0.9, 1.0),
		true,
	)


func _on_write_file(event: Dictionary) -> void:
	var raw_path := str(event.get("path", "")).replace("\\", "/")
	var content := str(event.get("content", ""))
	if raw_path.is_empty():
		return
	var root := ProjectSettings.globalize_path("res://").replace("\\", "/")
	var abs_path := raw_path
	if abs_path.begins_with(root):
		pass
	else:
		abs_path = root.path_join(abs_path)
	var dir := abs_path.get_base_dir()
	DirAccess.make_dir_recursive_absolute(dir)
	var file := FileAccess.open(abs_path, FileAccess.WRITE)
	if file == null:
		_append_text(
			"FileError",
			"Godot could not write %s, error code %d" % [abs_path, FileAccess.get_open_error()],
			Color(1.0, 0.4, 0.4),
			true,
		)
		return
	file.store_string(content)
	file.close()
	var fs := EditorInterface.get_resource_filesystem()
	if fs != null:
		fs.scan()
	if abs_path.ends_with(".gd"):
		var res := ResourceLoader.load("res://" + abs_path.trim_prefix(root).lstrip("/"), "", ResourceLoader.CACHE_MODE_REPLACE)
		if res is Script:
			EditorInterface.edit_script(res)
	_append_text(
		"File",
		"Godot wrote: %s (%d chars)" % [abs_path, content.length()],
		Color(0.6, 0.9, 1.0),
		true,
	)


func _display_image(path: String) -> void:
	if path.is_empty():
		return
	var image := Image.load_from_file(path)
	if image == null:
		return
	var texture := ImageTexture.create_from_image(image)
	var rect := TextureRect.new()
	rect.texture = texture
	rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	var width := minf(float(image.get_width()), 460.0)
	var height := maxf(width * float(image.get_height()) / float(image.get_width()), 180.0)
	rect.custom_minimum_size = Vector2(width, minf(height, 360.0))
	_messages_box.add_child(rect)
	_scroll_to_bottom.call_deferred()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

func _update_stream_labels() -> void:
	if not _stream_reasoning.is_empty():
		if _stream_reasoning_box == null:
			_stream_reasoning_box = _make_collapsible("Thinking (click to expand)", Color(0.6, 0.65, 0.75), true)
			_stream_reasoning_content = _stream_reasoning_box.get_node("Content") as Label
		_stream_reasoning_content.text = _stream_reasoning
	if not _stream_content.is_empty():
		if _stream_content_label == null:
			_stream_content_label = Label.new()
			_stream_content_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
			_stream_content_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
			_stream_content_label.add_theme_color_override("font_color", Color(0.85, 0.85, 0.85))
			_messages_box.add_child(_stream_content_label)
		_stream_content_label.text = "[dsh]\n" + _stream_content
	_scroll_to_bottom.call_deferred()


func _finish_stream_block() -> void:
	_stream_content = ""
	_stream_reasoning = ""
	_stream_content_label = null
	_stream_reasoning_box = null
	_stream_reasoning_content = null


func _make_collapsible(title: String, color: Color, mono: bool = false) -> VBoxContainer:
	var box := VBoxContainer.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var header := Button.new()
	header.text = title
	header.alignment = HORIZONTAL_ALIGNMENT_LEFT
	header.toggle_mode = true
	header.button_pressed = false
	header.flat = true
	header.add_theme_color_override("font_color", color)
	if mono:
		header.add_theme_font_size_override("font_size", 12)
	box.add_child(header)
	var content := Label.new()
	content.name = "Content"
	content.visible = false
	content.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_color_override("font_color", color)
	if mono:
		content.add_theme_font_size_override("font_size", 12)
	box.add_child(content)
	header.toggled.connect(func(expanded: bool) -> void: content.visible = expanded)
	_messages_box.add_child(box)
	return box


func _append_collapsible(title: String, text: String, color: Color) -> void:
	if text.strip_edges().is_empty():
		return
	var box := _make_collapsible(title, color, true)
	var content := box.get_node("Content") as Label
	content.text = text
	_scroll_to_bottom.call_deferred()


func _append_text(sender: String, text: String, color: Color, mono: bool = false) -> void:
	if text.strip_edges().is_empty():
		return
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	label.text = "[%s]\n%s" % [sender, text]
	label.add_theme_color_override("font_color", color)
	if mono:
		label.add_theme_font_size_override("font_size", 12)
	_messages_box.add_child(label)
	_scroll_to_bottom.call_deferred()


func _set_status(text: String, color: Color) -> void:
	if _status_label == null:
		return
	_status_label.text = "service: " + text
	_status_label.add_theme_color_override("font_color", color)


func _level_color(level: String) -> Color:
	match level:
		"ok":
			return Color(0.6, 1.0, 0.6)
		"warn":
			return Color(1.0, 0.85, 0.45)
		"error":
			return Color(1.0, 0.4, 0.4)
	return Color(0.8, 0.8, 0.8)


func _scroll_to_bottom() -> void:
	if _scroll == null:
		return
	var bar := _scroll.get_v_scroll_bar()
	_scroll.scroll_vertical = int(bar.max_value)


func _setting(key: String, default_value: Variant) -> Variant:
	var settings := EditorInterface.get_editor_settings()
	if settings == null:
		return default_value
	var value: Variant = settings.get_setting(key)
	return default_value if value == null else value


func _python_path() -> String:
	var override := str(_setting("dsh_godot/python_path", "")).strip_edges()
	if not override.is_empty():
		return override
	var root := ProjectSettings.globalize_path("res://")
	if OS.get_name() == "Windows":
		var candidate := root.path_join(".venv/Scripts/python.exe")
		if FileAccess.file_exists(candidate):
			return candidate
		return "python"
	var candidate := root.path_join(".venv/bin/python3")
	if FileAccess.file_exists(candidate):
		return candidate
	return "python3"


func _host() -> String:
	return str(_setting("dsh_godot/host", "127.0.0.1"))


func _port() -> int:
	return int(_setting("dsh_godot/port", 9610))


func _auto_start() -> bool:
	return bool(_setting("dsh_godot/auto_start", true))
