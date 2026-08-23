#!/usr/bin/env python3
"""
Parse an Adobe Camera Raw / Lightroom XMP preset (crs: namespace) and produce
a darktable action list (JSON) that the apply_xmp_preset.lua script can play
back inside darktable's darkroom via dt.gui.action().

Usage:
    xmp_to_darktable.py <preset.xmp> <output_actions.json> [--mapping mapping.json]
"""
import sys
import json
import argparse
import xml.etree.ElementTree as ET

CRS_NS = "http://ns.adobe.com/camera-raw-settings/1.0/"

# colorzones "graph" widget calibration (darktable 5.6.0, confirmed empirically
# 2026-08-23 against the default/active curve tab -- see mapping.json note on
# COLORZONES_HUE_CHANNELS for the caveat about which curve that actually is):
#   dt.gui.action("iop/colorzones/graph", <channel>, "down", 1000) reliably
#   clamps to the floor (2.0). From there, dt.gui.action(..., "up", speed)
#   adds speed * 0.01, and is clamped at the ceiling (3.0). Neutral/untouched
#   is exactly the midpoint, 2.5.
COLORZONES_FLOOR = 2.0
COLORZONES_CEILING = 3.0
COLORZONES_NEUTRAL = 2.5

# colorzones has 3 curve tabs, each independently switchable via the widget
# named "channel" (confirmed 2026-08-23 by reading darktable's actual C source,
# src/iop/colorzones.c: dt_action_define_iop(self, NULL, N_("channel"), ...)
# with pages named "lightness", "chroma", "hue" -- NOT "saturation", and NOT
# reachable via a "page" widget, which is what every earlier guess assumed).
# All three tabs share identical floor/neutral/ceiling calibration (confirmed
# empirically by testing each tab directly).
#
# ACR HueAdjustment<Channel> / SaturationAdjustment<Channel> /
# LuminanceAdjustment<Channel> -> (colorzones tab, channel name). Channel
# names match darktable's regardless of tab; darktable additionally supports
# "red" and "magenta" for all three ACR families.
COLORZONES_GROUPS = {
    "hue": {
        "tab": "hue",
        "keys": {
            "HueAdjustmentRed": "red",
            "HueAdjustmentOrange": "orange",
            "HueAdjustmentYellow": "yellow",
            "HueAdjustmentGreen": "green",
            "HueAdjustmentAqua": "aqua",
            "HueAdjustmentBlue": "blue",
            "HueAdjustmentPurple": "purple",
            "HueAdjustmentMagenta": "magenta",
        },
    },
    "chroma": {
        "tab": "chroma",
        "keys": {
            "SaturationAdjustmentRed": "red",
            "SaturationAdjustmentOrange": "orange",
            "SaturationAdjustmentYellow": "yellow",
            "SaturationAdjustmentGreen": "green",
            "SaturationAdjustmentAqua": "aqua",
            "SaturationAdjustmentBlue": "blue",
            "SaturationAdjustmentPurple": "purple",
            "SaturationAdjustmentMagenta": "magenta",
        },
    },
    "lightness": {
        "tab": "lightness",
        "keys": {
            "LuminanceAdjustmentRed": "red",
            "LuminanceAdjustmentOrange": "orange",
            "LuminanceAdjustmentYellow": "yellow",
            "LuminanceAdjustmentGreen": "green",
            "LuminanceAdjustmentAqua": "aqua",
            "LuminanceAdjustmentBlue": "blue",
            "LuminanceAdjustmentPurple": "purple",
            "LuminanceAdjustmentMagenta": "magenta",
        },
    },
}


def colorzones_node_actions(acr_value, channel):
    """Two dt.gui.action calls that deterministically land a colorzones graph
    node (on whichever tab is currently active) at the position corresponding
    to an ACR adjustment value (-100..100), regardless of the node's current
    position: force to the floor, then move up by exactly enough to reach
    the target."""
    target = COLORZONES_NEUTRAL + (acr_value / 100.0) * (COLORZONES_CEILING - COLORZONES_NEUTRAL)
    speed_up = (target - COLORZONES_FLOOR) * 100.0
    return [
        {"path": "iop/colorzones/graph", "element": channel, "effect": "down", "speed": 1000.0},
        {"path": "iop/colorzones/graph", "element": channel, "effect": "up", "speed": speed_up},
    ]


# ACR's parametric tone curve has 4 zone sliders (-100..100, Shadows/Darks/
# Lights/Highlights) and 3 split points, with no darktable widget shaped the
# same way. toneequal's "simple" mode has 9 plain EV-band sliders (-8 EV
# through +0 EV) that ARE genuine dt_bauhaus_slider_from_params bindings
# (confirmed via darktable's C source, src/iop/toneequal.c) -- close enough
# to a real match. They initially looked like a dead end: dt.gui.action
# accepted them (real, non-nan return values) but nothing ever landed in
# darktable's history table. Root-caused 2026-08-23 by instrumenting
# darktable's own C source and rebuilding it locally: _dev_auto_save (the
# function that flushes darktable's in-memory history to the database AND
# the .xmp sidecar) is NOT on an independent timer -- it's checked reactively,
# only when another history-item event fires, and its elapsed-time condition
# can't pass until ~20s after darkroom entry (autosave_time is initialized to
# now+10s, and the check itself requires another +10s past that). Every
# action in a preset normally fires within the first few seconds, so without
# one more, genuinely-different history event after the ~20s mark, NOTHING
# ever gets flushed -- confirmed by decoding the actual committed op_params
# bytes both before and after adding a trailing "flush trigger" to
# apply_xmp_preset.lua. This is why HueAdjustment*/SaturationAdjustment*/etc.
# via colorzones worked all along (that pipeline runs long enough, waiting
# for the .xmp sidecar to settle, to incidentally cross the 20s mark) while
# a fast, isolated single-action test never would. No darktable patch
# needed -- entirely a Lua-side timing fix.
#
# filmicrgb's contrast/latitude/balance sliders were tried on the same
# "maybe it just needs a page switch" theory before this was root-caused and
# also failed; not retested against the timing fix since Whites2012/
# Blacks2012 (its candidate use) aren't mapped to it (see mapping.json).
#
# toneequal's 9 bands share one floor/ceiling/neutral calibration
# (confirmed via source: each field is $MIN: -2.0 $MAX: 2.0 $DEFAULT: 0.0),
# and unlike colorzones this is a real absolute "set", not a force-floor/
# nudge trick -- no page switch needed either, these are reachable directly.
#
# NOT USED BY DEFAULT (see USE_TONEEQUAL_FOR_PARAMETRIC below): confirmed
# working via byte-level decode of the committed op_params (noise/-8 EV
# landed exactly on the target value), but running the real production -o
# pipeline with toneequal's actions included crashed darktable 3 times in a
# row (a GTK-internal memory-corruption abort in gtk_css_node/widget-path
# handling, inside darktable's module-group panel re-layout code -- a
# pre-existing darktable/GTK stability bug, not something in our control,
# probabilistically triggered by enabling/switching many modules in fast
# succession). Manual invocations without going through apply-lr-preset.sh
# were stable, so something about that script's flow (or simply having more
# actions in one run) makes the crash noticeably more likely. Kept here,
# fully implemented and documented, as the correct fix to switch back to
# once the crash trigger is understood -- see shadhi_blend_actions below for
# the safe default in the meantime.
TONEEQUAL_BANDS = ["-8 EV", "-7 EV", "-6 EV", "-5 EV", "-4 EV", "-3 EV", "-2 EV", "-1 EV", "+0 EV"]
TONEEQUAL_FLOOR = -2.0
TONEEQUAL_CEILING = 2.0
PARAMETRIC_KERNEL_SPREAD = 0.4
USE_TONEEQUAL_FOR_PARAMETRIC = False  # crash risk -- see note above. shadhi_blend_actions is the safe default.


def parametric_tone_actions(shadows, darks, lights, highlights,
                             shadow_split=25.0, midtone_split=50.0, highlight_split=75.0):
    """actions for toneequal's 9 EV-band sliders approximating ACR's 4-zone
    parametric tone curve (still a look-alike, not an algorithm match -- ACR's
    parametric curve and darktable's zone system work on different math).
    All 4 zone values in ACR's -100..100 range; splits in ACR's 0..100
    percentile range. Each EV band gets a triangular-kernel-weighted blend of
    the 4 ACR zone values, so the effect fades smoothly across bands rather
    than stepping abruptly at each split point."""
    shadow_center = (0.0 + shadow_split) / 2.0 / 100.0
    darks_center = (shadow_split + midtone_split) / 2.0 / 100.0
    lights_center = (midtone_split + highlight_split) / 2.0 / 100.0
    highlight_center = (highlight_split + 100.0) / 2.0 / 100.0
    zones = [
        (shadow_center, shadows),
        (darks_center, darks),
        (lights_center, lights),
        (highlight_center, highlights),
    ]

    actions = []
    for i, band in enumerate(TONEEQUAL_BANDS):
        band_pos = i / (len(TONEEQUAL_BANDS) - 1)  # 0 (deep shadow) .. 1 (highlight)
        weights = [max(0.0, 1.0 - abs(band_pos - center) / PARAMETRIC_KERNEL_SPREAD)
                   for center, _ in zones]
        total_weight = sum(weights)
        if total_weight == 0.0:
            continue
        blended_acr = sum(w * value for w, (_, value) in zip(weights, zones)) / total_weight
        band_value = (blended_acr / 100.0) * (TONEEQUAL_CEILING - TONEEQUAL_FLOOR) / 2.0
        band_value = max(TONEEQUAL_FLOOR, min(TONEEQUAL_CEILING, band_value))
        actions.append({
            "path": f"iop/toneequal/simple/{band}",
            "element": "value",
            "effect": "set",
            "speed": band_value,
        })
    return actions


# Safe default: blend into shadhi's two sliders, which are proven to commit
# reliably with no crash risk. Coarser (2 zones, not 4) but stable.
SHADHI_DARKS_WEIGHT = 0.5   # Darks' partial contribution into shadhi's "shadows"
SHADHI_LIGHTS_WEIGHT = 0.5  # Lights' partial contribution into shadhi's "highlights"


def shadhi_blend_actions(shadows, darks, lights, highlights):
    """Combines ACR's 4-zone parametric tone curve (Shadows/Darks/Lights/
    Highlights) into shadhi's two real sliders (its own Highlights2012/
    Shadows2012 mapping lives separately in mapping.json's entries table).
    All inputs in ACR's -100..100 range; shadhi's scale is 0.01."""
    combined_shadows = shadows + SHADHI_DARKS_WEIGHT * darks
    combined_highlights = highlights + SHADHI_LIGHTS_WEIGHT * lights
    return [
        {"path": "iop/shadhi/shadows", "element": "value", "effect": "set", "speed": combined_shadows * 0.01},
        {"path": "iop/shadhi/highlights", "element": "value", "effect": "set", "speed": combined_highlights * 0.01},
    ]


def parse_crs_attributes(xmp_path):
    tree = ET.parse(xmp_path)
    root = tree.getroot()
    values = {}
    for desc in root.iter("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description"):
        for key, val in desc.attrib.items():
            if key.startswith(f"{{{CRS_NS}}}"):
                local = key.split("}", 1)[1]
                values[local] = val
    return values


def to_number(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def build_actions(crs_values, mapping, percent=100.0):
    """percent mirrors Lightroom's preset-Amount slider: 100 applies the preset
    exactly as authored, 0 would leave the photo untouched, and anything in
    between linearly blends toward each attribute's neutral/untouched value.
    Scaling the raw ACR value by percent/100 before computing the darktable
    target works for every action type here, because in each case the
    neutral/untouched darktable value is exactly what you get when the raw
    ACR value is 0 (scale.b for plain sliders, COLORZONES_NEUTRAL for
    colorzones nodes) -- so scaling the ACR value scales the *deviation*
    from neutral proportionally, which is exactly what an Amount slider does.
    """
    actions = []
    skipped = []
    used_keys = set()
    pct = percent / 100.0

    for entry in mapping["entries"]:
        key = entry["crs_key"]
        raw = crs_values.get(key)
        num = to_number(raw)
        if num is None:
            continue
        used_keys.add(key)
        num *= pct
        scale = entry.get("scale", {"a": 1.0, "b": 0.0})
        value = num * scale["a"] + scale["b"]
        actions.append({
            "crs_key": key,
            "raw_value": num,
            "element": "value",
            "effect": "set",
            "speed": value,
            "path": entry["path"],
            "enable_module": entry.get("enable_module"),
            "verified": entry.get("verified", False),
            "note": entry.get("note", ""),
        })

    colorzones_enabled_yet = False
    for group_name, group in COLORZONES_GROUPS.items():
        group_actions = []
        for key, channel in group["keys"].items():
            raw = crs_values.get(key)
            num = to_number(raw)
            if num is None:
                continue
            used_keys.add(key)
            num *= pct
            for a in colorzones_node_actions(num, channel):
                group_actions.append({"crs_key": key, "raw_value": num, **a})

        if not group_actions:
            continue

        # One tab switch covers every channel in this group.
        actions.append({
            "crs_key": f"(colorzones tab switch: {group_name})",
            "raw_value": None,
            "element": group["tab"],
            "effect": "activate",
            "speed": 1.0,
            "path": "iop/colorzones/channel",
            "enable_module": "iop/colorzones" if not colorzones_enabled_yet else None,
            "verified": True,
            "note": "switches colorzones' active curve tab (widget name 'channel', confirmed via source)",
        })
        colorzones_enabled_yet = True

        for a in group_actions:
            actions.append({
                **a,
                "enable_module": None,
                "verified": True,
                "note": f"colorzones {group_name}-graph node, force-floor then land-on-target",
            })

    parametric_keys = ["ParametricShadows", "ParametricDarks", "ParametricLights", "ParametricHighlights"]
    parametric_present = {k: to_number(crs_values.get(k)) for k in parametric_keys}
    shadows_input = to_number(crs_values.get("Shadows2012"))
    highlights_input = to_number(crs_values.get("Highlights2012"))
    if any(v is not None for v in parametric_present.values()) or shadows_input is not None or highlights_input is not None:
        for k in parametric_keys:
            used_keys.add(k)
        used_keys.update({"Shadows2012", "Highlights2012"})
        p_shadows = (parametric_present["ParametricShadows"] or 0.0) * pct
        p_darks = (parametric_present["ParametricDarks"] or 0.0) * pct
        p_lights = (parametric_present["ParametricLights"] or 0.0) * pct
        p_highlights = (parametric_present["ParametricHighlights"] or 0.0) * pct
        shadows2012 = (shadows_input or 0.0) * pct
        highlights2012 = (highlights_input or 0.0) * pct
        # Split points define zone *positions*, not amount -- not scaled by percent.
        shadow_split = to_number(crs_values.get("ParametricShadowSplit")) or 25.0
        midtone_split = to_number(crs_values.get("ParametricMidtoneSplit")) or 50.0
        highlight_split = to_number(crs_values.get("ParametricHighlightSplit")) or 75.0
        used_keys.update({"ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit"})

        if USE_TONEEQUAL_FOR_PARAMETRIC:
            # toneequal's 9 EV bands stand alone; Highlights2012/Shadows2012
            # still go to shadhi separately (see mapping.json entries) since
            # they're not part of the parametric-curve approximation itself.
            tone_actions = parametric_tone_actions(p_shadows, p_darks, p_lights, p_highlights,
                                                    shadow_split, midtone_split, highlight_split)
            for i, a in enumerate(tone_actions):
                actions.append({
                    "crs_key": "Parametric*" if i == 0 else f"Parametric* ({a['path'].rsplit('/', 1)[-1]})",
                    "raw_value": a["speed"],
                    "element": a["element"],
                    "effect": a["effect"],
                    "speed": a["speed"],
                    "path": a["path"],
                    "enable_module": "iop/toneequal" if i == 0 else None,
                    "verified": True,
                    "note": ("approximates ACR's 4-zone parametric tone curve as a smoothed blend "
                             "across toneequal's EV bands -- a look-alike, not an algorithm match. "
                             "Requires the trailing flush-trigger in apply_xmp_preset.lua to actually "
                             "commit (see mapping.json parametric_note)."),
                })
            for crs_key, shadhi_path in (("Highlights2012", "iop/shadhi/highlights"),
                                          ("Shadows2012", "iop/shadhi/shadows")):
                num = to_number(crs_values.get(crs_key))
                if num is None:
                    continue
                actions.append({
                    "crs_key": crs_key, "raw_value": num * pct, "element": "value", "effect": "set",
                    "speed": num * pct * 0.01, "path": shadhi_path,
                    "enable_module": "iop/shadhi", "verified": True,
                    "note": "Internal op name is 'shadhi'. Different algorithm than ACR's -- approximate look match only.",
                })
        else:
            # Merge all 6 ACR values into ONE combined value per shadhi slider,
            # touched exactly once each. Touching the same shadhi slider twice
            # in one run (once for Highlights2012/Shadows2012, again for a
            # separate Parametric* blend) crashed darktable -- confirmed
            # empirically 2026-08-23, a GTK-internal memory-corruption abort.
            for i, a in enumerate(shadhi_blend_actions(shadows2012 + p_shadows, p_darks,
                                                        p_lights, highlights2012 + p_highlights)):
                actions.append({
                    "crs_key": "Highlights2012+Shadows2012+Parametric*",
                    "raw_value": a["speed"],
                    "element": a["element"],
                    "effect": a["effect"],
                    "speed": a["speed"],
                    "path": a["path"],
                    "enable_module": "iop/shadhi" if i == 0 else None,
                    "verified": True,
                    "note": ("combines ACR's Highlights2012/Shadows2012 with its 4-zone parametric "
                             "tone curve into shadhi's 2 sliders, each touched exactly once -- coarser "
                             "than a real 4-zone system (see mapping.json parametric_note for the more "
                             "accurate toneequal-based alternative, currently disabled by default due "
                             "to a darktable crash risk)."),
                })

    # Only the first action touching a given module should carry its
    # enable_module (module groups built independently -- e.g. shadhi's own
    # Highlights2012/Shadows2012 entry plus the Parametric* shadhi-blend --
    # can each set enable_module for the same iop path). Repeating the
    # enable query/set on an already-enabled module crashed darktable
    # (confirmed empirically 2026-08-23: a GTK-internal memory-corruption
    # abort, likely from redundant toggle-button style/CSS recalculation).
    seen_enable_modules = set()
    for a in actions:
        if a["enable_module"]:
            if a["enable_module"] in seen_enable_modules:
                a["enable_module"] = None
            else:
                seen_enable_modules.add(a["enable_module"])

    present_keys = set(crs_values.keys())
    unused = present_keys - used_keys
    boilerplate = {"PresetType", "Cluster", "UUID", "SupportsAmount", "SupportsColor",
                   "SupportsMonochrome", "SupportsHighDynamicRange", "SupportsNormalDynamicRange",
                   "SupportsSceneReferred", "SupportsOutputReferred", "RequiresRGBTables",
                   "CameraModelRestriction", "Copyright", "ContactInfo", "Version",
                   "ProcessVersion", "ConvertToGrayscale", "HasSettings",
                   "ColorGradeBlending", "SplitToningBalance"}
    for key in sorted(unused - boilerplate):
        skipped.append({"crs_key": key, "raw_value": crs_values[key]})

    return actions, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xmp_path")
    ap.add_argument("output_json")
    ap.add_argument("--mapping", default=None,
                     help="Path to mapping.json (default: mapping.json next to this script)")
    ap.add_argument("--percent", type=float, default=100.0,
                     help="Preset strength 1-100, like Lightroom's Amount slider (default: 100)")
    args = ap.parse_args()

    if not (1.0 <= args.percent <= 100.0):
        print(f"--percent must be 1-100, got {args.percent}", file=sys.stderr)
        sys.exit(1)

    mapping_path = args.mapping
    if mapping_path is None:
        import os
        mapping_path = str(__import__("pathlib").Path(__file__).parent / "mapping.json")

    with open(mapping_path) as f:
        mapping = json.load(f)

    crs_values = parse_crs_attributes(args.xmp_path)
    actions, skipped = build_actions(crs_values, mapping, percent=args.percent)

    out = {"actions": actions, "skipped_crs_keys": skipped}
    with open(args.output_json, "w") as f:
        json.dump(out, f, indent=2)

    # Plain pipe-delimited sidecar for the Lua side (no JSON lib available
    # inside darktable's bundled Lua environment).
    # Fields: crs_key|path|element|effect|speed|enable_module
    actions_txt_path = args.output_json.rsplit(".", 1)[0] + ".txt"
    with open(actions_txt_path, "w") as f:
        for a in actions:
            enable = a["enable_module"] or ""
            f.write(f"{a['crs_key']}|{a['path']}|{a['element']}|{a['effect']}|{a['speed']}|{enable}\n")

    print(f"Wrote {len(actions)} action(s) to {args.output_json} and {actions_txt_path} (strength: {args.percent:g}%)")
    if skipped:
        print(f"Skipped {len(skipped)} unmapped crs: key(s) (no darktable equivalent wired up yet):")
        for s in skipped:
            print(f"  - {s['crs_key']} = {s['raw_value']}")
    unverified = [a for a in actions if not a["verified"]]
    if unverified:
        print(f"\n{len(unverified)} action path(s) are best-guess and unverified for this darktable version:")
        for a in unverified:
            print(f"  - {a['crs_key']} -> {a['path']}  ({a['note']})")
        print("If apply_xmp_preset.lua logs a failure for one of these, look up the real path via")
        print("darktable Preferences > Shortcuts (search the module name) and fix mapping.json.")


if __name__ == "__main__":
    main()
