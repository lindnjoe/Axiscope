
import os
import ast

# Section prefix used by the AFC-Toolchanger plugin
# (https://github.com/lindnjoe/AFC-Toolchanger). Each tool unit is declared as
# [AFC_Toolchanger <name>] and exposes the same tool_numbers/tool_names/active_tool
# surface as the original viesturz klipper-toolchanger plugin.
AFC_TOOLCHANGER_PREFIX = 'AFC_Toolchanger'
AFC_EXTRUDER_PREFIX = 'AFC_extruder '

class Axiscope:
    def __init__(self, config):
        self.printer       = config.get_printer()
        self.gcode         = self.printer.lookup_object('gcode')
        self.gcode_move = self.printer.load_object(config, 'gcode_move')

        self.x_pos         = config.getfloat('zswitch_x_pos', None)
        self.y_pos         = config.getfloat('zswitch_y_pos', None)
        self.z_pos         = config.getfloat('zswitch_z_pos', None)
        self.lift_z        = config.getfloat('lift_z'       , 1)
        self.move_speed    = config.getint('move_speed'  , 60)
        self.z_move_speed  = config.getint('z_move_speed', 10)
        self.samples       = config.getint('samples'     , 10)

        # Z calibration backend:
        #   switch       -> original physical endstop workflow (PROBE_ZSWITCH
        #                   touches a fixed-position switch via tools_calibrate)
        #   cartographer -> reference-tool touch_home + per-tool touch_probe
        #                   workflow using Cartographer / scanner.
        self.z_backend = config.get('z_backend', 'switch').strip().lower()
        if self.z_backend not in ('switch', 'cartographer'):
            raise config.error(
                "[axiscope] z_backend must be 'switch' or 'cartographer'")

        # Cartographer probe-point configuration (only used when
        # z_backend == 'cartographer').
        self.probe_x = config.getfloat('probe_x_pos', None)
        self.probe_y = config.getfloat('probe_y_pos', None)
        self.reference_tool = config.getint('reference_tool', 0)
        self.touch_home_gcode = config.get(
            'touch_home_gcode', 'CARTOGRAPHER_TOUCH_HOME')
        self.touch_probe_gcode = config.get(
            'touch_probe_gcode', 'CARTOGRAPHER_TOUCH_PROBE')
        self.use_current_z_offsets = config.getboolean(
            'use_current_z_offsets', True)
        self.cartographer_touch_model_z_offset = 0.0

        self.pin              = config.get('pin'             , None)
        self.config_file_path = config.get('config_file_path', None)
        
        # Load gcode_macro module for template support
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        
        # Custom gcode macros
        self.start_gcode = self.gcode_macro.load_template(config, 'start_gcode', '')
        self.before_pickup_gcode = self.gcode_macro.load_template(config, 'before_pickup_gcode', '')
        self.after_pickup_gcode = self.gcode_macro.load_template(config, 'after_pickup_gcode', '')
        self.pre_probe_gcode = self.gcode_macro.load_template(config, 'pre_probe_gcode', '')
        self.finish_gcode = self.gcode_macro.load_template(config, 'finish_gcode', '')

        # Optional probe-prep heat-soak. When > 0, Axiscope issues
        # `M109 S<probe_temp>` after the tool change and before
        # pre_probe_gcode runs, so any nozzle-brush macro (e.g. AFC_BRUSH)
        # in pre_probe_gcode operates on a hot, consistently-tempered tip.
        self.probe_temp = config.getint('probe_temp', 0)

        self.has_cfg_data     = False
        self.probe_results = {}

        # Check for tools_calibrate conflict
        if config.has_section('tools_calibrate'):
            raise config.error(
                "Cannot use [axiscope] when [tools_calibrate] is also configured. "
                "Both modules conflict with each other. "
                "Please use only one: either [axiscope] or [tools_calibrate]."
            )

        # Only build the physical-switch probe path when using the switch
        # backend. The cartographer backend uses CARTOGRAPHER_TOUCH_* gcode and
        # doesn't need tools_calibrate at all.
        self.probe_multi_axis = None
        if self.z_backend == 'switch' and self.pin is not None:
            # tools_calibrate is shipped by the viesturz klipper-toolchanger
            # plugin and is NOT present in stock Klipper or in AFC-Toolchanger.
            # Only require it when the user actually configured a Z-probe pin.
            try:
                from . import tools_calibrate
            except ImportError as e:
                raise config.error(
                    "Axiscope: 'pin:' is configured for Z probing, but the "
                    "tools_calibrate Klipper module is missing. It ships with "
                    "klipper-toolchanger (https://github.com/viesturz/"
                    "klipper-toolchanger). Install that plugin OR remove the "
                    "'pin:' line from [axiscope]. (ImportError: %s)" % (e,)
                )
            self.probe_multi_axis = tools_calibrate.PrinterProbeMultiAxis(
                config,
                tools_calibrate.ProbeEndstopWrapper(config, 'x'),
                tools_calibrate.ProbeEndstopWrapper(config, 'y'),
                tools_calibrate.ProbeEndstopWrapper(config, 'z')
            )
            query_endstops = self.printer.load_object(config, 'query_endstops')
            query_endstops.register_endstop(self.probe_multi_axis.mcu_probe[-1].mcu_endstop, "Axiscope")

        # Toolchanger module is resolved after Klipper finishes loading objects.
        # AFC-Toolchanger registers each unit as [AFC_Toolchanger <name>] so it
        # cannot be loaded by static name; fall back to the legacy viesturz
        # `toolchanger` object name for backwards compatibility.
        self.toolchanger = None
        self.toolchanger_kind = None

        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        #register gcode commands
        self.gcode.register_command('MOVE_TO_ZSWITCH', self.cmd_MOVE_TO_ZSWITCH, desc=self.cmd_MOVE_TO_ZSWITCH_help)
        self.gcode.register_command('PROBE_ZSWITCH',   self.cmd_PROBE_ZSWITCH, desc=self.cmd_PROBE_ZSWITCH_help)
        self.gcode.register_command('CALIBRATE_ALL_Z_OFFSETS',   self.cmd_CALIBRATE_ALL_Z_OFFSETS, desc=self.cmd_CALIBRATE_ALL_Z_OFFSETS_help)

        self.gcode.register_command('AXISCOPE_START_GCODE', self.cmd_AXISCOPE_START_GCODE, desc="Execute the Axiscope start G-code macro")
        self.gcode.register_command('AXISCOPE_BEFORE_PICKUP_GCODE', self.cmd_AXISCOPE_BEFORE_PICKUP_GCODE, desc="Execute the Axiscope before pickup G-code macro")
        self.gcode.register_command('AXISCOPE_AFTER_PICKUP_GCODE', self.cmd_AXISCOPE_AFTER_PICKUP_GCODE, desc="Execute the Axiscope after pickup G-code macro")
        self.gcode.register_command('AXISCOPE_PRE_PROBE_GCODE', self.cmd_AXISCOPE_PRE_PROBE_GCODE, desc="Heat-soak to probe_temp and execute the pre_probe_gcode macro")
        self.gcode.register_command('AXISCOPE_FINISH_GCODE', self.cmd_AXISCOPE_FINISH_GCODE, desc="Execute the Axiscope finish G-code macro")
        self.gcode.register_command('AXISCOPE_SAVE_TOOL_OFFSET',          self.cmd_AXISCOPE_SAVE_TOOL_OFFSET,          desc=self.cmd_AXISCOPE_SAVE_TOOL_OFFSET_help)
        self.gcode.register_command('AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS', self.cmd_AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS, desc=self.cmd_AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS_help)
        self.gcode.register_command('AXISCOPE_SET_ENDSTOP_POSITION', self.cmd_AXISCOPE_SET_ENDSTOP_POSITION, desc=self.cmd_AXISCOPE_SET_ENDSTOP_POSITION_help)
        self.gcode.register_command('AXISCOPE_DEBUG', self.cmd_AXISCOPE_DEBUG, desc="Print discovered toolchanger and tool objects.")

    def _find_afc_toolchanger(self):
        # AFC-Toolchanger registers as either [AFC_Toolchanger] (no name) or
        # [AFC_Toolchanger <name>]. Match both.
        for name, obj in self.printer.objects.items():
            if name == AFC_TOOLCHANGER_PREFIX or name.startswith(
                    AFC_TOOLCHANGER_PREFIX + ' '):
                return name, obj
        return None, None

    def _afc_extruder_objects(self):
        # Yield every [AFC_extruder <name>] object so we can fall back to
        # building a tool list directly from extruders even if no
        # [AFC_Toolchanger] section is present (or its tools dict is empty).
        for name, obj in self.printer.objects.items():
            if name.startswith(AFC_EXTRUDER_PREFIX):
                yield name, obj

    def _load_cartographer_touch_model_z_offset(self):
        """Read the Cartographer touch_model z_offset out of the SAVE_CONFIG
        block in the active config file. Returns 0.0 on any failure."""
        if self.config_file_path is None or not os.path.exists(
                self.config_file_path):
            return 0.0

        section_prefixes = (
            '#*# [cartographer touch_model ',
            '#*# [scanner touch_model ',
        )
        in_touch_model = False
        try:
            with open(self.config_file_path, 'r') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if any(line.startswith(p) for p in section_prefixes):
                        in_touch_model = True
                        continue
                    if in_touch_model and line.startswith('#*# ['):
                        in_touch_model = False
                    if in_touch_model and line.startswith('#*# z_offset ='):
                        return float(line.split('=', 1)[1].strip())
        except Exception:
            return 0.0
        return 0.0

    def _get_last_z_result(self):
        """Look for the standard Klipper probe last_z_result on probe / scanner
        / cartographer status objects. Returns None if not found."""
        curtime = self.printer.get_reactor().monotonic()
        for obj_name in ('probe', 'scanner', 'cartographer'):
            obj = self.printer.lookup_object(obj_name, None)
            if obj is None:
                continue
            if hasattr(obj, 'get_status'):
                try:
                    status = obj.get_status(curtime)
                except Exception:
                    status = None
                if isinstance(status, dict) and \
                        status.get('last_z_result') is not None:
                    return float(status['last_z_result'])
            if hasattr(obj, 'last_z_result'):
                try:
                    return float(obj.last_z_result)
                except Exception:
                    pass
        return None

    def _get_trigger_distance(self):
        for obj_name in ('scanner', 'cartographer', 'probe'):
            obj = self.printer.lookup_object(obj_name, None)
            if obj is None:
                continue
            if hasattr(obj, 'trigger_distance'):
                try:
                    return float(obj.trigger_distance)
                except Exception:
                    pass
        return 2.0

    def _build_probe_result(self, *, source, measured_contact_z,
                            suggested_gcode_z_offset, measured_time,
                            z_trigger=None, z_offset=None, z_delta=None,
                            touch_model_z_offset=None,
                            trigger_distance=None):
        return {
            'source': source,
            'measured_contact_z':       measured_contact_z,
            'suggested_gcode_z_offset': suggested_gcode_z_offset,
            'touch_model_z_offset':     touch_model_z_offset,
            'trigger_distance':         trigger_distance,
            # legacy keys retained for the non-cartographer UI path
            'z_trigger': measured_contact_z if z_trigger is None else z_trigger,
            'z_offset':  suggested_gcode_z_offset if z_offset is None else z_offset,
            'z_delta':   measured_contact_z if z_delta is None else z_delta,
            'last_run':  measured_time,
        }

    def get_current_tool_z_offset(self, tool_no):
        """Return the running gcode_z_offset for tool `tool_no`. Works for
        both AFC-Toolchanger and viesturz klipper-toolchanger."""
        try:
            tn = int(tool_no)
        except (TypeError, ValueError):
            return 0.0
        _, tools_by_number = self._collect_tools()
        tool = tools_by_number.get(tn)
        if tool is None:
            return 0.0
        offsets = self._tool_offsets(tool)
        return float(offsets[2]) if len(offsets) >= 3 else 0.0

    def has_probe_point(self):
        return all(p is not None for p in [self.probe_x, self.probe_y])

    def _resolve_tool_section_name(self, gcmd):
        """Resolve a tool to its config section name. Accepts TOOL_NAME=...
        (full section header like 'AFC_extruder extruder1') or TOOL=<n>
        (looked up against the discovered toolchanger). Returns the section
        name string or None on failure (after responding to gcmd)."""
        explicit = gcmd.get('TOOL_NAME', None)
        if explicit:
            return explicit

        tool_no = gcmd.get_int('TOOL', None)
        if tool_no is None:
            gcmd.respond_error(
                'Axiscope: provide TOOL=<n> or TOOL_NAME="<section>".')
            return None

        _, tools_by_number = self._collect_tools()
        tool = tools_by_number.get(tool_no)
        if tool is None:
            gcmd.respond_error(
                'Axiscope: tool number %d is not registered.' % tool_no)
            return None

        section = self._tool_section_name(tool)
        if not section:
            gcmd.respond_error(
                'Axiscope: could not resolve a config section for T%d. '
                'Pass TOOL_NAME="<section>" instead.' % tool_no)
            return None
        return section

    def _afc_config_rewriter(self):
        """Return AFC's ConfigRewrite callable if AFC is loaded and ready."""
        afc_fn = self.printer.lookup_object('AFC_functions', None)
        if afc_fn is None:
            return None
        # AFC.afcFunction needs `.afc` (back-pointer to the controller) before
        # ConfigRewrite can find cfgloc. AFC sets this during its own connect.
        if getattr(afc_fn, 'afc', None) is None:
            return None
        return getattr(afc_fn, 'ConfigRewrite', None)

    def _live_apply_offsets(self, section_name, offsets):
        """Mirror new offsets onto the live AFCExtruder object so the next
        tool pickup uses them without waiting for a Klipper restart."""
        if not section_name.startswith('AFC_extruder '):
            return
        tool_obj = self.printer.lookup_object(section_name, None)
        if tool_obj is None:
            return
        axes = "xyz" if len(offsets) == 3 else "xy"
        for i, a in enumerate(axes):
            try:
                setattr(tool_obj, 'gcode_%s_offset' % a, float(offsets[i]))
            except Exception:
                pass

    def _write_tool_offsets(self, gcmd, section_name, offsets):
        """Persist offsets for `section_name`. Prefers AFC's ConfigRewrite
        when the section is an AFC_extruder; falls back to rewriting
        config_file_path. Returns True iff a write happened."""
        axes = "xyz" if len(offsets) == 3 else "xy"

        if section_name.startswith('AFC_extruder '):
            rewrite = self._afc_config_rewriter()
            if rewrite is not None:
                try:
                    for i, a in enumerate(axes):
                        rewrite(section_name,
                                'gcode_%s_offset' % a,
                                '%.3f' % float(offsets[i]),
                                'Axiscope offsets')
                except Exception as e:
                    gcmd.respond_error(
                        'Axiscope: AFC ConfigRewrite failed for %s: %s'
                        % (section_name, e))
                    return False

                self._live_apply_offsets(section_name, offsets)
                gcmd.respond_info(
                    'Axiscope: wrote %s offsets to AFC config '
                    '(restart Klipper to fully reload).' % section_name)
                return True

        # Fallback: rewrite config_file_path the old way.
        if not self.has_cfg_data:
            gcmd.respond_error(
                'Axiscope: %s is not an AFC_extruder section and '
                'config_file_path is not set, so the offsets have nowhere '
                'to be written. Either set config_file_path in [axiscope] '
                'or load AFC-Toolchanger so AFC_functions is available.'
                % section_name)
            return False

        with open(self.config_file_path, 'r') as f:
            cfg_data = f.readlines()
        cfg_data = self.update_tool_offsets(cfg_data, section_name, offsets)
        with open(self.config_file_path, 'w') as f:
            for line in cfg_data:
                f.write(line)
        gcmd.respond_info(
            'Axiscope: wrote %s offsets to %s.'
            % (section_name, self.config_file_path))
        return True

    def handle_connect(self):
        # Discover the toolchanger module.
        afc_name, afc_obj = self._find_afc_toolchanger()
        if afc_obj is not None:
            self.toolchanger = afc_obj
            self.toolchanger_kind = 'afc'
            self.gcode.respond_info(
                "Axiscope: detected AFC-Toolchanger '%s'." % afc_name)

        if self.toolchanger is None:
            self.toolchanger = self.printer.lookup_object('toolchanger', None)
            if self.toolchanger is not None:
                self.toolchanger_kind = 'viesturz'
                self.gcode.respond_info(
                    "Axiscope: detected klipper-toolchanger.")

        # If no toolchanger module is registered, fall back to scanning for
        # AFC_extruder objects directly. Each AFC_extruder carries its own
        # tool_number and gcode_*_offset values, so the UI still works as
        # long as the extruders exist.
        if self.toolchanger is None:
            extruders = [o for _, o in self._afc_extruder_objects()
                         if getattr(o, 'tool_number', -1) >= 0]
            if extruders:
                self.toolchanger_kind = 'afc-extruders-only'
                self.gcode.respond_info(
                    "Axiscope: no [AFC_Toolchanger] section found; using %d "
                    "[AFC_extruder] section(s) directly." % len(extruders))

        if self.toolchanger is None and self.toolchanger_kind is None:
            self.gcode.respond_info(
                "Axiscope: no toolchanger module found. Configure either "
                "[AFC_Toolchanger <name>] (AFC-Toolchanger) or [toolchanger] "
                "(klipper-toolchanger).")

        if self.config_file_path is not None:
            expanded_path = os.path.expanduser(self.config_file_path)
            self.config_file_path = expanded_path

            if os.path.exists(self.config_file_path):
                self.has_cfg_data = True
                if self.z_backend == 'cartographer':
                    self.cartographer_touch_model_z_offset = \
                        self._load_cartographer_touch_model_z_offset()
                self.gcode.respond_info("Axiscope config file found (%s)." % self.config_file_path)
                self.gcode.respond_info(
                    "--Axiscope Loaded-- (z_backend=%s)" % self.z_backend)
                if self.z_backend == 'cartographer':
                    self.gcode.respond_info(
                        "Axiscope Cartographer touch_model z_offset = %.5f"
                        % self.cartographer_touch_model_z_offset)
            else:
                self.gcode.respond_info("Could not find Axiscope config file (%s)" % self.config_file_path)
                self.gcode.respond_info("Note: You can use ~ for home directory, e.g., ~/printer_data/config/axiscope.offsets")

        else:
            self.gcode.respond_info("Axiscope is missing config file location (config_file_path). You will need to update your tool offsets manually.")
            self.gcode.respond_info("You can set config_file_path: ~/printer_data/config/axiscope.offsets in your [axiscope] section.")


    def _tool_section_name(self, tool):
        # Return the printer.objects key for `tool` (e.g. "AFC_extruder extruder1"
        # for AFC-Toolchanger or "tool T0" for klipper-toolchanger).
        for name, obj in self.printer.objects.items():
            if obj is tool:
                return name
        return getattr(tool, 'name', None)

    def _tool_offsets(self, tool):
        if hasattr(tool, 'get_offset'):
            try:
                offsets = tool.get_offset()
                if offsets and len(offsets) >= 3:
                    return [offsets[0], offsets[1], offsets[2]]
            except Exception:
                pass
        return [
            getattr(tool, 'gcode_x_offset', 0.0),
            getattr(tool, 'gcode_y_offset', 0.0),
            getattr(tool, 'gcode_z_offset', 0.0),
        ]

    def _active_tool_status(self, eventtime):
        tc = self.toolchanger
        if tc is None:
            return {}
        active = getattr(tc, 'active_tool', None)
        if not active:
            return {}
        # AFC-Toolchanger's AFCExtruder exposes get_tool_status for templates
        # (its get_status is reserved for filament-load state). klipper-toolchanger
        # uses get_status. Prefer get_tool_status when available.
        for attr in ('get_tool_status', 'get_status'):
            fn = getattr(active, attr, None)
            if callable(fn):
                try:
                    return fn(eventtime)
                except Exception:
                    continue
        return {}

    def _select_tool_command(self, tool, tool_number):
        """Return the gcode command string Axiscope should run to make `tool`
        the active toolhead. AFC-Toolchanger users want AFC_SELECT_TOOL because
        T<n> in AFC means "lane swap" (filament change on the current
        extruder), not a physical toolhead switch."""
        if self.toolchanger_kind in ('afc', 'afc-extruders-only'):
            name = getattr(tool, 'name', None) if tool is not None else None
            if name:
                return 'AFC_SELECT_TOOL TOOL=%s' % name
        return 'T%d' % tool_number

    def _collect_tools(self):
        tc = self.toolchanger
        tool_numbers = []
        tools_by_number = {}

        if tc is not None:
            tool_numbers = list(getattr(tc, 'tool_numbers', []) or [])
            # AFC-Toolchanger stores tools in a dict keyed by int; klipper-
            # toolchanger uses name keys parallel to tool_numbers/tool_names.
            tc_tools = getattr(tc, 'tools', None)
            if isinstance(tc_tools, dict):
                # AFC keys by number; klipper-toolchanger keys by name.
                # Normalize to int keys when possible.
                for k, v in tc_tools.items():
                    if isinstance(k, int):
                        tools_by_number[k] = v
                    else:
                        tn = getattr(v, 'tool_number', None)
                        if isinstance(tn, int) and tn >= 0:
                            tools_by_number[tn] = v
            if not tools_by_number:
                tool_names = list(getattr(tc, 'tool_names', []) or [])
                for n, name in zip(tool_numbers, tool_names):
                    obj = self.printer.lookup_object(name, None)
                    if obj is not None:
                        tools_by_number[n] = obj

        # Fallback: enumerate AFC_extruder objects directly. Useful when the
        # toolchanger object hasn't populated its tools dict yet, when no
        # [AFC_Toolchanger] section exists, or for sanity-checking what
        # tool_numbers the running config actually exposes.
        if not tools_by_number:
            for _, obj in self._afc_extruder_objects():
                tn = getattr(obj, 'tool_number', -1)
                if isinstance(tn, int) and tn >= 0:
                    tools_by_number[tn] = obj
            tool_numbers = sorted(tools_by_number.keys())
        elif not tool_numbers:
            tool_numbers = sorted(tools_by_number.keys())

        return tool_numbers, tools_by_number

    def get_status(self, eventtime):
        tool_numbers, tools_by_number = self._collect_tools()
        tools = {}
        section_names = []
        for n in tool_numbers:
            tool = tools_by_number.get(n)
            if tool is None:
                continue
            offsets = self._tool_offsets(tool)
            section = self._tool_section_name(tool)
            if section is not None:
                section_names.append(section)
            tools[str(n)] = {
                'tool_number':    n,
                'name':           getattr(tool, 'name', ''),
                'section_name':   section,
                'gcode_x_offset': offsets[0],
                'gcode_y_offset': offsets[1],
                'gcode_z_offset': offsets[2],
                'select_command': self._select_tool_command(tool, n),
            }

        active_tool = getattr(self.toolchanger, 'active_tool', None) \
            if self.toolchanger else None
        active_number = getattr(active_tool, 'tool_number', -1) \
            if active_tool else -1

        return {
            'probe_results':   self.probe_results,
            'can_save_config': self.has_cfg_data is not False,
            'endstop_x':       self.x_pos,
            'endstop_y':       self.y_pos,
            'endstop_z':       self.z_pos,
            'tools':           tools,
            'tool_numbers':    list(tool_numbers),
            'tool_names':      section_names,
            'tool_number':     active_number,
            'toolchanger_kind': self.toolchanger_kind,
            'z_backend':       self.z_backend,
            'probe_x':         self.probe_x,
            'probe_y':         self.probe_y,
            'reference_tool':  self.reference_tool,
            'use_current_z_offsets': self.use_current_z_offsets,
            'cartographer_touch_model_z_offset':
                self.cartographer_touch_model_z_offset,
            'probe_temp':       self.probe_temp,
        }

    def run_gcode(self, name, template, extra_context):
        """Run gcode with template expansion and context"""
        curtime = self.printer.get_reactor().monotonic()
        toolchanger_status = {}
        if self.toolchanger is not None and hasattr(self.toolchanger, 'get_status'):
            try:
                toolchanger_status = self.toolchanger.get_status(curtime)
            except Exception:
                toolchanger_status = {}
        context = {
            **template.create_template_context(),
            'tool':         self._active_tool_status(curtime),
            'toolchanger':  toolchanger_status,
            'axiscope':     self.get_status(curtime),
            **extra_context,
        }
        template.run_gcode_from_command(context)


    def is_homed(self):
        toolhead   = self.printer.lookup_object('toolhead')
        ctime      = self.printer.get_reactor().monotonic()
        homed_axes = toolhead.get_kinematics().get_status(ctime)['homed_axes']

        return all(x in homed_axes for x in 'xyz')


    def has_switch_pos(self):
        return all(x is not None for x in [self.x_pos, self.y_pos, self.z_pos])

    def update_tool_offsets(self, cfg_data, tool_name, offsets):
        axis          = "xyz" if len(offsets) == 3 else "xy"
        section_name  = "[%s]" % tool_name
        section_start = None
        section_end   = None
        new_section   = None

        for i, line in enumerate(cfg_data):
            stripped_line = line.lstrip()
            if stripped_line.startswith(section_name):
                section_start = i+1
            
            elif section_start is not None:
                if stripped_line.startswith('['):
                    section_end = i-1
                    break

        for i, a in enumerate(axis):
            offset_name   = "gcode_%s_offset" % a
            offset_value  = offsets[i]
            offset_string = "%s: %.3f\n" % (offset_name, offset_value)

            if section_start is not None:
                if section_end is not None:
                    section_lines = cfg_data[section_start:section_end+1]
                else:
                    section_lines = cfg_data[section_start:]

                for line in section_lines:
                    stripped_line = line.lstrip()

                    if stripped_line.startswith(offset_name):
                        cfg_index = cfg_data.index(line)
                        cfg_data[cfg_index] = offset_string

            else:
                if new_section is not None:
                    new_section.append(offset_string)
                else:
                    new_section = ["\n", section_name+"\n", offset_string]

        if new_section is not None:
            new_section.append("\n")
            no_touch_index = None

            if self.config_file_path.endswith('printer.cfg'):
                for line in cfg_data:
                    if line.lstrip().startswith('#*#'):
                        no_touch_index = cfg_data.index(line)
                        break

            if no_touch_index is not None:
                cfg_data = cfg_data[:no_touch_index] + ["\n"] + new_section + cfg_data[no_touch_index:]

            else:
                cfg_data = cfg_data + ["\n"] + new_section
        
        return cfg_data

    cmd_MOVE_TO_ZSWITCH_help = "Move the toolhead to the Z calibration point"

    def cmd_MOVE_TO_ZSWITCH(self, gcmd):
        if not self.is_homed():
            gcmd.respond_info('Must home first.')
            return

        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        current_pos = toolhead.get_position()

        if self.z_backend == 'switch':
            if not self.has_switch_pos():
                gcmd.respond_error('Z switch positions are not valid.')
                return
            gcmd.respond_info('Moving to Z switch')
            self.gcode_move.cmd_G1(self.gcode.create_gcode_command(
                "G0", "G0",
                {'X': self.x_pos, 'Y': self.y_pos, 'Z': current_pos[2],
                 'F': self.move_speed * 60}))
            toolhead.manual_move(
                [None, None, self.z_pos + self.lift_z], self.z_move_speed)
            return

        # cartographer backend
        if not self.has_probe_point():
            gcmd.respond_error('Cartographer probe point is not valid.')
            return
        gcmd.respond_info(
            'Moving to Cartographer probe point X%.3f Y%.3f'
            % (self.probe_x, self.probe_y))
        self.gcode_move.cmd_G1(self.gcode.create_gcode_command(
            "G0", "G0",
            {'X': self.probe_x, 'Y': self.probe_y,
             'Z': max(current_pos[2], self.lift_z),
             'F': self.move_speed * 60}))


    cmd_PROBE_ZSWITCH_help = "Probe the active Z calibration backend to determine offset"

    def _probe_switch_backend(self, gcmd):
        toolhead  = self.printer.lookup_object('toolhead')
        tool_no   = str(self.toolchanger.active_tool.tool_number)
        start_pos = toolhead.get_position()
        z_result  = self.probe_multi_axis.run_probe(
            "z-", gcmd, speed_ratio=0.5, max_distance=10.0,
            samples=self.samples)[2]
        measured_time = self.printer.get_reactor().monotonic()
        ref = str(self.reference_tool)

        if tool_no == ref:
            self.probe_results[tool_no] = self._build_probe_result(
                source='switch_probe_reference',
                measured_contact_z=z_result,
                suggested_gcode_z_offset=0.0,
                measured_time=measured_time,
                z_delta=0.0,
            )
        elif ref in self.probe_results:
            z_offset = z_result - self.probe_results[ref]['z_trigger']
            self.probe_results[tool_no] = self._build_probe_result(
                source='switch_probe',
                measured_contact_z=z_result,
                suggested_gcode_z_offset=z_offset,
                measured_time=measured_time,
                z_delta=z_offset,
            )
        else:
            self.probe_results[tool_no] = self._build_probe_result(
                source='switch_probe',
                measured_contact_z=z_result,
                suggested_gcode_z_offset=None,
                measured_time=measured_time,
                z_delta=None,
            )

        toolhead.move(start_pos, self.z_move_speed)
        toolhead.set_position(start_pos)
        toolhead.wait_moves()

    def _probe_cartographer_backend(self, gcmd):
        if not self.has_probe_point():
            gcmd.respond_error('Cartographer probe point is not valid.')
            return

        toolhead = self.printer.lookup_object('toolhead')
        tool_no = int(self.toolchanger.active_tool.tool_number)
        start_pos = toolhead.get_position()
        measured_time = self.printer.get_reactor().monotonic()
        trigger_distance = self._get_trigger_distance()

        if tool_no == self.reference_tool:
            gcmd.respond_info(
                'Cartographer reference touch-home with T%i' % tool_no)
            self.gcode.run_script_from_command(self.touch_home_gcode)
            self.probe_results[str(tool_no)] = self._build_probe_result(
                source='cartographer_touch_reference',
                measured_contact_z=0.0,
                suggested_gcode_z_offset=0.0,
                measured_time=measured_time,
                z_delta=0.0,
                touch_model_z_offset=self.cartographer_touch_model_z_offset,
                trigger_distance=trigger_distance,
            )
        else:
            if str(self.reference_tool) not in self.probe_results:
                gcmd.respond_error(
                    'Reference tool T%i must be measured first.'
                    % self.reference_tool)
                return

            gcmd.respond_info(
                'Cartographer touch-probe with T%i' % tool_no)
            self.gcode.run_script_from_command(self.touch_probe_gcode)

            measured_z = self._get_last_z_result()
            if measured_z is None or abs(measured_z) < 1e-9:
                measured_z = (
                    float(toolhead.get_position()[2])
                    - trigger_distance
                    - self.cartographer_touch_model_z_offset
                )
            if measured_z is None:
                raise gcmd.error(
                    'Unable to read a Cartographer touch result after %s. '
                    'Tried last_z_result and toolhead Z minus '
                    'trigger_distance minus touch_model z_offset.'
                    % self.touch_probe_gcode)

            current_offset = self.get_current_tool_z_offset(tool_no)
            suggested_offset = (
                current_offset + measured_z
                if self.use_current_z_offsets else measured_z
            )
            self.probe_results[str(tool_no)] = self._build_probe_result(
                source='cartographer_touch',
                measured_contact_z=measured_z,
                suggested_gcode_z_offset=suggested_offset,
                measured_time=measured_time,
                touch_model_z_offset=self.cartographer_touch_model_z_offset,
                trigger_distance=trigger_distance,
            )

        # Lift away from the bed before the next tool change.
        safe_return_z = max(start_pos[2], self.lift_z)
        toolhead.manual_move([None, None, safe_return_z], self.z_move_speed)
        toolhead.wait_moves()

    def cmd_PROBE_ZSWITCH(self, gcmd):
        if self.toolchanger is None or self.toolchanger.active_tool is None:
            gcmd.respond_error('No active tool reported by toolchanger.')
            return

        if self.z_backend == 'switch':
            if self.probe_multi_axis is None:
                gcmd.respond_error(
                    'Switch backend selected but no pin/probe is configured.')
                return
            self._probe_switch_backend(gcmd)
            return

        self._probe_cartographer_backend(gcmd)


    cmd_CALIBRATE_ALL_Z_OFFSETS_help = "Probe the Z switch for each tool to determine offset."

    def cmd_CALIBRATE_ALL_Z_OFFSETS(self, gcmd):

        if not self.is_homed():
            gcmd.respond_info('Must home first.')
            return

        tool_numbers, tools_by_number = self._collect_tools()
        if not tool_numbers:
            gcmd.respond_error(
                'Axiscope: no tools detected. Run AXISCOPE_DEBUG to inspect '
                'the discovered toolchanger and extruder objects.')
            return

        # Run start_gcode at the beginning of calibration
        self.cmd_AXISCOPE_START_GCODE(gcmd)

        # Cartographer needs the reference tool first so subsequent tools have
        # a baseline to subtract; the switch backend benefits from the same
        # ordering and is harmless when reference_tool == 0.
        ordered_tools = list(tool_numbers)
        if self.reference_tool in ordered_tools:
            ordered_tools = [self.reference_tool] + [
                t for t in ordered_tools if t != self.reference_tool]

        for tool_no in ordered_tools:
            tool = tools_by_number.get(tool_no)
            select_cmd = self._select_tool_command(tool, tool_no)
            self.cmd_AXISCOPE_BEFORE_PICKUP_GCODE(gcmd)
            self.gcode.run_script_from_command(select_cmd)
            self.cmd_AXISCOPE_AFTER_PICKUP_GCODE(gcmd)

            # Heat-soak + nozzle-brush hook before each probe.
            self.cmd_AXISCOPE_PRE_PROBE_GCODE(gcmd)

            self.gcode.run_script_from_command('MOVE_TO_ZSWITCH')
            self.gcode.run_script_from_command(
                'PROBE_ZSWITCH SAMPLES=%i' % self.samples)

        # Return to the reference tool using the right command flavor.
        ref_tool = tools_by_number.get(self.reference_tool)
        self.gcode.run_script_from_command(
            self._select_tool_command(ref_tool, self.reference_tool))

        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()

        for tool_no in ordered_tools:
            tool_key = str(tool_no)
            result = self.probe_results.get(tool_key)
            if result is None or tool_no == self.reference_tool:
                continue
            if self.z_backend == 'cartographer':
                gcmd.respond_info(
                    'T%s measured_contact_z: %.3f | '
                    'suggested_gcode_z_offset: %.3f' % (
                        tool_no,
                        result.get('measured_contact_z', float('nan')),
                        result.get('suggested_gcode_z_offset', float('nan'))))
            else:
                gcmd.respond_info(
                    'T%s gcode_z_offset: %.3f' % (
                        tool_no, result.get('z_offset', 0.0)))

        # Run finish_gcode after calibration is complete
        self.cmd_AXISCOPE_FINISH_GCODE(gcmd)
    
    # Command handlers for custom macro G-code commands
    def cmd_AXISCOPE_START_GCODE(self, gcmd):
        """Execute the Axiscope start G-code macro"""
        if self.start_gcode:
            self.run_gcode('start_gcode', self.start_gcode, {})
        else:
            gcmd.respond_info("No start_gcode configured for Axiscope")

    def cmd_AXISCOPE_BEFORE_PICKUP_GCODE(self, gcmd):
        """Execute the Axiscope before pickup G-code macro"""
        if self.before_pickup_gcode:
            self.run_gcode('before_pickup_gcode', self.before_pickup_gcode, {})
        else:
            gcmd.respond_info("No before_pickup_gcode configured for Axiscope")

    def cmd_AXISCOPE_AFTER_PICKUP_GCODE(self, gcmd):
        """Execute the Axiscope after pickup G-code macro"""
        if self.after_pickup_gcode:
            self.run_gcode('after_pickup_gcode', self.after_pickup_gcode, {})
        else:
            gcmd.respond_info("No after_pickup_gcode configured for Axiscope")

    def cmd_AXISCOPE_PRE_PROBE_GCODE(self, gcmd):
        """Heat-soak to probe_temp then run pre_probe_gcode.

        Runs after the tool change has completed (so M109 targets the
        already-active extruder) and before MOVE_TO_ZSWITCH, giving
        AFC_BRUSH or any other prep macro a chance to clean a hot tip
        before the cartographer touch."""
        if self.probe_temp and int(self.probe_temp) > 0:
            gcmd.respond_info(
                'Axiscope: heating active tool to %dC for probe.' % self.probe_temp)
            self.gcode.run_script_from_command('M109 S%d' % int(self.probe_temp))
        if self.pre_probe_gcode:
            self.run_gcode('pre_probe_gcode', self.pre_probe_gcode, {})
        elif not self.probe_temp:
            gcmd.respond_info("No probe_temp or pre_probe_gcode configured for Axiscope")

    def cmd_AXISCOPE_FINISH_GCODE(self, gcmd):
        """Execute the Axiscope finish G-code macro"""
        if self.finish_gcode:
            self.run_gcode('finish_gcode', self.finish_gcode, {})
        else:
            gcmd.respond_info("No finish_gcode configured for Axiscope")

    cmd_AXISCOPE_SAVE_TOOL_OFFSET_help = "Save a tool offset to its config section."

    def cmd_AXISCOPE_SAVE_TOOL_OFFSET(self, gcmd):
        """
        Save offsets for a single tool. AFC users get the values written
        directly into the matching [AFC_extruder <name>] section via AFC's
        ConfigRewrite (which scans every .cfg in the AFC config dir).
        Non-AFC setups continue to write into config_file_path.

        Usage
        -----
        AXISCOPE_SAVE_TOOL_OFFSET (TOOL=<n> | TOOL_NAME="<section>") OFFSETS=<offsets>

        Examples
        --------
        AXISCOPE_SAVE_TOOL_OFFSET TOOL=1 OFFSETS="[1.091, -0.928, -0.080]"
        AXISCOPE_SAVE_TOOL_OFFSET TOOL_NAME="AFC_extruder extruder1" \
            OFFSETS="[1.091, -0.928, -0.080]"
        AXISCOPE_SAVE_TOOL_OFFSET TOOL_NAME="tool T0" \
            OFFSETS="[-0.01, 0.03, 0.01]"
        """
        section_name = self._resolve_tool_section_name(gcmd)
        if section_name is None:
            return

        try:
            offsets = ast.literal_eval(gcmd.get('OFFSETS'))
        except Exception as e:
            gcmd.respond_error('Axiscope: bad OFFSETS literal: %s' % e)
            return

        self._write_tool_offsets(gcmd, section_name, offsets)


    cmd_AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS_help = "Save multiple tool offsets to their config sections."

    def cmd_AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS(self, gcmd):
        """
        Save offsets for several tools at once. AFC users get each value
        written directly to the corresponding [AFC_extruder <name>] section.

        Usage
        -----
        AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS (TOOLS=<tool_numbers>|TOOL_NAMES=<sections>)
                                            OFFSETS=<list-of-lists>

        Examples
        --------
        AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS TOOLS="[1, 2]" \
            OFFSETS="[[1.09,-0.92,-0.08], [1.58,0.15,-0.07]]"
        AXISCOPE_SAVE_MULTIPLE_TOOL_OFFSETS \
            TOOL_NAMES="['tool T0','tool T1']" \
            OFFSETS="[[-0.01,0.03,0.01],[0.02,0.02,-0.06]]"
        """
        try:
            offsets = ast.literal_eval(gcmd.get('OFFSETS'))
        except Exception as e:
            gcmd.respond_error('Axiscope: bad OFFSETS literal: %s' % e)
            return

        section_names = []
        names_arg = gcmd.get('TOOL_NAMES', None)
        if names_arg:
            try:
                section_names = list(ast.literal_eval(names_arg))
            except Exception as e:
                gcmd.respond_error('Axiscope: bad TOOL_NAMES literal: %s' % e)
                return
        else:
            tools_arg = gcmd.get('TOOLS', None)
            if not tools_arg:
                gcmd.respond_error(
                    'Axiscope: provide TOOLS=[...] or TOOL_NAMES=[...].')
                return
            try:
                tool_numbers = list(ast.literal_eval(tools_arg))
            except Exception as e:
                gcmd.respond_error('Axiscope: bad TOOLS literal: %s' % e)
                return
            _, tools_by_number = self._collect_tools()
            for n in tool_numbers:
                tool = tools_by_number.get(int(n))
                if tool is None:
                    gcmd.respond_error(
                        'Axiscope: tool number %s is not registered.' % n)
                    return
                section = self._tool_section_name(tool)
                if not section:
                    gcmd.respond_error(
                        'Axiscope: could not resolve a config section for '
                        'T%s.' % n)
                    return
                section_names.append(section)

        if len(section_names) != len(offsets):
            gcmd.respond_error(
                'Axiscope: TOOLS/TOOL_NAMES and OFFSETS length mismatch (%d vs %d).'
                % (len(section_names), len(offsets)))
            return

        for section, off in zip(section_names, offsets):
            if not self._write_tool_offsets(gcmd, section, off):
                return

    def cmd_AXISCOPE_DEBUG(self, gcmd):
        """Print every AFC_Toolchanger / AFC_extruder / toolchanger object
        Axiscope can see, the active toolchanger, and the resolved tool list.
        Run from the Klipper console when the UI shows no tools."""
        lines = []
        lines.append("toolchanger_kind = %r" % self.toolchanger_kind)
        lines.append("toolchanger object = %r" % self.toolchanger)

        afc_tcs = []
        afc_exts = []
        for name, obj in self.printer.objects.items():
            if name == AFC_TOOLCHANGER_PREFIX or name.startswith(
                    AFC_TOOLCHANGER_PREFIX + ' '):
                afc_tcs.append((name, obj))
            elif name.startswith(AFC_EXTRUDER_PREFIX):
                afc_exts.append((name, obj))

        lines.append("AFC_Toolchanger sections: %d" % len(afc_tcs))
        for name, obj in afc_tcs:
            tn = list(getattr(obj, 'tool_numbers', []) or [])
            tools_dict = getattr(obj, 'tools', None)
            tools_count = len(tools_dict) if isinstance(tools_dict, dict) else 'n/a'
            lines.append("  - %s  tool_numbers=%s  tools_dict_size=%s"
                         % (name, tn, tools_count))

        lines.append("AFC_extruder sections: %d" % len(afc_exts))
        for name, obj in afc_exts:
            lines.append("  - %s  tool_number=%s  offsets=(%s, %s, %s)" % (
                name,
                getattr(obj, 'tool_number', '?'),
                getattr(obj, 'gcode_x_offset', '?'),
                getattr(obj, 'gcode_y_offset', '?'),
                getattr(obj, 'gcode_z_offset', '?'),
            ))

        tool_numbers, tools_by_number = self._collect_tools()
        lines.append("Resolved tool_numbers: %s" % list(tool_numbers))
        for n in tool_numbers:
            t = tools_by_number.get(n)
            lines.append("  T%s -> section=%r offsets=%s select_command=%r" % (
                n, self._tool_section_name(t),
                self._tool_offsets(t) if t else None,
                self._select_tool_command(t, n) if t else None))

        gcmd.respond_info("\n".join(lines))

    cmd_AXISCOPE_SET_ENDSTOP_POSITION_help = "Set kinematic position for X, Y, and/or Z axes"
    
    def cmd_AXISCOPE_SET_ENDSTOP_POSITION(self, gcmd):
        """
        Set axiscope endstop positions for specified axes. Can receive X, Y, and/or Z optionally.
        
        Usage
        -----
        AXISCOPE_SET_ENDSTOP_POSITION [X=<x_pos>] [Y=<y_pos>] [Z=<z_pos>] [CURRENT=<1|0>]
        
        Examples
        --------
        AXISCOPE_SET_ENDSTOP_POSITION X=150.0          # Set only X endstop position
        AXISCOPE_SET_ENDSTOP_POSITION Y=200.0          # Set only Y endstop position  
        AXISCOPE_SET_ENDSTOP_POSITION Z=0.0            # Set only Z endstop position
        AXISCOPE_SET_ENDSTOP_POSITION X=150.0 Y=200.0  # Set X and Y endstop positions
        AXISCOPE_SET_ENDSTOP_POSITION X=150.0 Y=200.0 Z=0.0  # Set all endstop positions
        AXISCOPE_SET_ENDSTOP_POSITION CURRENT=1        # Set all positions to current position
        AXISCOPE_SET_ENDSTOP_POSITION X=150.0 CURRENT=1  # Set X to 150.0, Y and Z to current
        """
        # Get current position
        toolhead = self.printer.lookup_object('toolhead')
        current_pos = toolhead.get_position()
        
        # Check if CURRENT parameter is set
        use_current = gcmd.get_int('CURRENT', 0)
        
        # Get optional parameters
        x_pos = gcmd.get_float('X', None)
        y_pos = gcmd.get_float('Y', None) 
        z_pos = gcmd.get_float('Z', None)
        
        # If CURRENT=1, use current position for unspecified axes
        if use_current:
            if x_pos is None:
                x_pos = current_pos[0]
            if y_pos is None:
                y_pos = current_pos[1]
            if z_pos is None:
                z_pos = current_pos[2]
        
        # Update axiscope's internal position variables. On the cartographer
        # backend X/Y belong to the touch probe point; Z still goes to the
        # switch position (it is meaningless on cartographer but harmless).
        set_axes = []
        if x_pos is not None:
            if self.z_backend == 'cartographer':
                self.probe_x = x_pos
            else:
                self.x_pos = x_pos
            set_axes.append(f"X={x_pos:.3f}")
        if y_pos is not None:
            if self.z_backend == 'cartographer':
                self.probe_y = y_pos
            else:
                self.y_pos = y_pos
            set_axes.append(f"Y={y_pos:.3f}")
        if z_pos is not None:
            self.z_pos = z_pos
            set_axes.append(f"Z={z_pos:.3f}")

        if set_axes:
            if use_current:
                gcmd.respond_info(f"Set axiscope calibration positions (using current): {' '.join(set_axes)}")
            else:
                gcmd.respond_info(f"Set axiscope calibration positions: {' '.join(set_axes)}")
        else:
            gcmd.respond_info("No axes specified. Use X=, Y=, Z=, and/or CURRENT=1 parameters.")

def load_config(config):
    return Axiscope(config)