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
# same way. toneequal's "simple" EV-band sliders looked like the ideal match
# (confirmed real dt_bauhaus_slider_from_params-bound sliders, not a curve
# widget -- unlike colorzones/colorbalancergb/splittoning's actual dead
# ends), but reading darktable's C source (src/iop/toneequal.c) plus
# extensive empirical testing (correct widget names, page-then-value
# ordering, expanding the module panel, waiting well past any debounce,
# forcing enable in isolation, testing against fresh images with zero prior
# history to rule out stale-data artifacts) found that its value-changed
# callback never calls dt_dev_add_history_item when driven via
# dt.gui.action: the slider visibly updates but nothing commits. filmicrgb's
# contrast/latitude/balance sliders (also genuine dt_bauhaus_slider_from_params
# bindings) were tried next on the same theory and hit the identical wall.
# Both failing modules share one thing every working module doesn't: they're
# already enabled by darktable's own auto-applied default workflow (enable
# query returns 1 before any interaction), vs. every module we successfully
# drive being off by default -- the strongest available signal for why,
# though not confirmed as the root cause.
#
# Fallback: blend into shadhi's two sliders, which are proven to commit
# reliably. Coarser (2 zones, not 4) but real.
SHADHI_DARKS_WEIGHT = 0.5   # Darks' partial contribution into shadhi's "shadows"
SHADHI_LIGHTS_WEIGHT = 0.5  # Lights' partial contribution into shadhi's "highlights"


def shadhi_blend_actions(highlights2012, shadows2012,
                          parametric_shadows, parametric_darks,
                          parametric_lights, parametric_highlights):
    """Combines ACR's Highlights2012/Shadows2012 sliders with its 4-zone
    parametric tone curve (Shadows/Darks/Lights/Highlights) into shadhi's two
    real sliders. All inputs in ACR's -100..100 range; shadhi's scale is
    0.01 (matching the existing Highlights2012/Shadows2012 mapping)."""
    combined_shadows = shadows2012 + parametric_shadows + SHADHI_DARKS_WEIGHT * parametric_darks
    combined_highlights = highlights2012 + parametric_highlights + SHADHI_LIGHTS_WEIGHT * parametric_lights
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

    shadhi_keys = ["Highlights2012", "Shadows2012",
                   "ParametricShadows", "ParametricDarks", "ParametricLights", "ParametricHighlights"]
    shadhi_present = {k: to_number(crs_values.get(k)) for k in shadhi_keys}
    if any(v is not None for v in shadhi_present.values()):
        for k in shadhi_keys:
            used_keys.add(k)
        highlights2012 = (shadhi_present["Highlights2012"] or 0.0) * pct
        shadows2012 = (shadhi_present["Shadows2012"] or 0.0) * pct
        p_shadows = (shadhi_present["ParametricShadows"] or 0.0) * pct
        p_darks = (shadhi_present["ParametricDarks"] or 0.0) * pct
        p_lights = (shadhi_present["ParametricLights"] or 0.0) * pct
        p_highlights = (shadhi_present["ParametricHighlights"] or 0.0) * pct

        for i, a in enumerate(shadhi_blend_actions(highlights2012, shadows2012,
                                                    p_shadows, p_darks, p_lights, p_highlights)):
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
                         "tone curve into shadhi's 2 sliders -- coarser than a real 4-zone system, "
                         "but shadhi is proven to commit reliably (see mapping.json shadhi_blend_note "
                         "for why toneequal and filmicrgb, the closer matches, don't)"),
            })

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
