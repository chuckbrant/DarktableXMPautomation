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


def build_actions(crs_values, mapping):
    actions = []
    skipped = []
    used_keys = set()

    for entry in mapping["entries"]:
        key = entry["crs_key"]
        raw = crs_values.get(key)
        num = to_number(raw)
        if num is None:
            continue
        used_keys.add(key)
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

    present_keys = set(crs_values.keys())
    unused = present_keys - used_keys
    boilerplate = {"PresetType", "Cluster", "UUID", "SupportsAmount", "SupportsColor",
                   "SupportsMonochrome", "SupportsHighDynamicRange", "SupportsNormalDynamicRange",
                   "SupportsSceneReferred", "SupportsOutputReferred", "RequiresRGBTables",
                   "CameraModelRestriction", "Copyright", "ContactInfo", "Version",
                   "ProcessVersion", "ConvertToGrayscale", "HasSettings",
                   "ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit",
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
    args = ap.parse_args()

    mapping_path = args.mapping
    if mapping_path is None:
        import os
        mapping_path = str(__import__("pathlib").Path(__file__).parent / "mapping.json")

    with open(mapping_path) as f:
        mapping = json.load(f)

    crs_values = parse_crs_attributes(args.xmp_path)
    actions, skipped = build_actions(crs_values, mapping)

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

    print(f"Wrote {len(actions)} action(s) to {args.output_json} and {actions_txt_path}")
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
