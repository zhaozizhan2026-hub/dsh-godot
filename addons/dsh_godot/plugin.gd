@tool
extends EditorPlugin

const DockScript := preload("res://addons/dsh_godot/dsh_dock.gd")

var dock: Control = null


func _enter_tree() -> void:
	_register_settings()
	dock = DockScript.new()
	dock.plugin = self
	dock.name = "DSH Godot"
	add_control_to_dock(DOCK_SLOT_RIGHT_BL, dock)


func _exit_tree() -> void:
	if dock != null:
		dock.cleanup()
		remove_control_from_docks(dock)
		dock.queue_free()
		dock = null


func _register_settings() -> void:
	var es := EditorInterface.get_editor_settings()
	if es == null:
		return
	_seed_setting(es, "dsh_godot/auto_start", true, TYPE_BOOL)
	es.set_setting("dsh_godot/auto_start", true)
	_seed_setting(es, "dsh_godot/host", "127.0.0.1", TYPE_STRING)
	_seed_setting(es, "dsh_godot/port", 9600, TYPE_INT)
	_seed_setting(es, "dsh_godot/python_path", "", TYPE_STRING)
	es.add_property_info({
		"name": "dsh_godot/auto_start",
		"type": TYPE_BOOL,
		"hint": PROPERTY_HINT_NONE,
	})
	es.add_property_info({
		"name": "dsh_godot/host",
		"type": TYPE_STRING,
		"hint": PROPERTY_HINT_NONE,
	})
	es.add_property_info({
		"name": "dsh_godot/port",
		"type": TYPE_INT,
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1024,65535,1",
	})
	es.add_property_info({
		"name": "dsh_godot/python_path",
		"type": TYPE_STRING,
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": "*.exe",
	})


func _seed_setting(es: EditorSettings, key: String, default_value: Variant, type: int) -> void:
	if not es.has_setting(key):
		es.set_setting(key, default_value)
	es.set_initial_value(key, default_value, false)
