--[[
apply_xmp_preset

Reads a pipe-delimited action list (produced by xmp_to_darktable.py from a
Lightroom/Camera Raw XMP preset) from the path in the DT_XMP_PRESET_ACTIONS
environment variable, and plays it back as darktable module slider actions
the first time an image is loaded into the darkroom in this session.

INSTALLATION
  Already placed in $CONFIGDIR/lua/apply_xmp_preset.lua
  Add this line to $CONFIGDIR/luarc (create the file if missing):
    require "apply_xmp_preset"

USAGE
  DT_XMP_PRESET_ACTIONS=/path/to/actions.txt /Applications/darktable.app/Contents/MacOS/darktable /path/to/photo.RAF
]]

local darktable = require "darktable"

local applied = false

local function log(msg)
  darktable.print_log("[apply_xmp_preset] " .. msg)
end

-- Parses the pipe-delimited sidecar written by xmp_to_darktable.py:
--   crs_key|path|element|effect|speed|enable_module
local function read_actions(path)
  local f = io.open(path, "r")
  if not f then
    log("could not open actions file: " .. path)
    return nil
  end
  local actions = {}
  for line in f:lines() do
    if line ~= "" then
      local crs_key, action_path, element, effect, speed_str, enable_module =
        line:match("^(.-)|(.-)|(.-)|(.-)|(.-)|(.*)$")
      if crs_key then
        table.insert(actions, {
          crs_key = crs_key,
          path = action_path,
          element = element,
          effect = effect,
          speed = tonumber(speed_str),
          enable_module = (enable_module ~= "" and enable_module or nil),
        })
      else
        log("could not parse line: " .. line)
      end
    end
  end
  f:close()
  return actions
end

local function apply_actions(actions_path)
  local actions = read_actions(actions_path)
  if not actions then
    log("no actions found in " .. tostring(actions_path))
    return
  end

  local ok_count, fail_count = 0, 0

  for _, action in ipairs(actions) do
    if action.enable_module then
      -- Query current state with speed=0 (the documented "just query, don't
      -- act" value), not speed="" (an empty string -- not a documented value,
      -- and likely why toneequal silently failed to enable in an earlier run:
      -- 0 dt.gui.action failures reported, sliders set fine, but the module
      -- never appeared in the history stack at all). Forcing enable
      -- unconditionally instead of querying first is NOT safe: it crashed
      -- darktable outright, most likely because it toggles an
      -- already-enabled module (several are auto-enabled by default) OFF
      -- instead of being a no-op.
      local ok_e, enabled = pcall(function()
        return darktable.gui.action(action.enable_module, "enable", "", 0)
      end)
      if ok_e and enabled == 0.0 then
        pcall(function()
          darktable.gui.action(action.enable_module, "enable", "", 1.0)
        end)
      end
    end

    local ok, result = pcall(function()
      return darktable.gui.action(action.path, action.element, action.effect, action.speed)
    end)

    -- A bad action path doesn't error or return nil; dt.gui.action returns
    -- NaN instead (confirmed empirically 2026-08-23). NaN is the only Lua
    -- value that is not equal to itself, hence the self-comparison below.
    local failed = (not ok) or (result == nil) or (result ~= result)

    if failed then
      fail_count = fail_count + 1
      log(string.format("FAILED: %s -> %s (element=%s, effect=%s, speed=%s, return=%s). Verify " ..
        "the real action path via Preferences > Shortcuts and fix mapping.json.", action.crs_key,
        action.path, tostring(action.element), tostring(action.effect), tostring(action.speed),
        tostring(result)))
    else
      ok_count = ok_count + 1
    end
  end

  log(string.format("applied %d action(s), %d failed", ok_count, fail_count))
end

darktable.register_event("apply_xmp_preset", "darkroom-image-loaded",
  function(event, clean, image)
    if applied then return end
    if not clean then return end
    local actions_path = os.getenv("DT_XMP_PRESET_ACTIONS")
    if not actions_path then
      return
    end
    applied = true
    log("applying preset from " .. actions_path .. " to " .. image.filename)
    local ok, err = pcall(apply_actions, actions_path)
    if not ok then
      log("UNCAUGHT ERROR while applying preset: " .. tostring(err))
    end

    -- darktable's own autosave (history -> database + .xmp sidecar) is
    -- reactive, not on an independent timer: it re-checks its elapsed-time
    -- condition only when another history-item event fires, and that
    -- condition can't pass until ~20s after darkroom entry (autosave_time
    -- is initialized to now+10s, and the check itself requires another
    -- +10s past that -- confirmed empirically 2026-08-23 by instrumenting
    -- darktable's own C source). If every action in actions_path fires
    -- within the first few seconds (the normal case), NONE of them ever
    -- lands in the database or sidecar without one more, genuinely-different
    -- history event after that ~20s mark to trigger the check again -- when
    -- that happens, the ENTIRE accumulated in-memory history (everything
    -- applied above) gets flushed at once, not just the triggering action.
    -- darktable.control.sleep() deep within a dispatched function (not
    -- directly in this event handler -- doing it there deadlocks the
    -- process, confirmed empirically) safely yields without blocking the
    -- GTK main loop, so this fires as a background continuation.
    darktable.control.dispatch(function()
      darktable.control.sleep(22000)
      local ok2, err2 = pcall(function()
        -- iop/exposure always exists regardless of preset content, so this
        -- is a safe universal flush trigger. Nudge away from neutral (0.0)
        -- then back, so the final state is unaffected: the nudge is what
        -- triggers the flush, the reset keeps black level correction clean.
        darktable.gui.action("iop/exposure/black level correction", "value", "set", 0.00001)
        darktable.gui.action("iop/exposure/black level correction", "value", "set", 0.0)
      end)
      if not ok2 then
        log("UNCAUGHT ERROR while flush-triggering: " .. tostring(err2))
      else
        log("flush-triggered after 22s wait")
      end
    end)
  end
)

log("loaded, waiting for darkroom-image-loaded event")
