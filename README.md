# DarktableXMPautomation

Apply a Lightroom / Adobe Camera Raw `.xmp` preset to a photo in [darktable](https://www.darktable.org/), automatically — either opened live in darktable's darkroom for review, or rendered straight to an output file with no GUI interaction needed.

darktable has no native support for Lightroom/ACR presets. This tool parses the ACR preset's `crs:` attributes and replays the closest equivalent edit in darktable, by driving darktable's own Lua scripting API (`dt.gui.action`) — the same mechanism darktable's own bundled example scripts and MIDI-controller integrations use. It does **not** hand-craft darktable's binary style/history format; every edit is applied through darktable's real UI code, so it's exactly as correct as if you'd dragged the slider yourself.

Built and verified against darktable 5.6.0 on macOS.

## What we have

These ACR preset attributes are mapped, tested, and confirmed landing correctly in darktable's actual history stack (not just "the call didn't error" — verified via the written `.xmp` sidecar):

| ACR attribute | darktable module | Notes |
|---|---|---|
| `Exposure2012` | `exposure` | Same units (EV), direct slider set |
| `Vibrance` | `velvia` | Direct slider set |
| `Saturation` | `colorbalancergb` (global saturation) | Direct slider set |
| `Highlights2012` / `Shadows2012` / `ParametricShadows/Darks/Lights/Highlights` | `shadhi` (highlights + shadows) | All 6 blended into shadhi's 2 sliders — see below |
| `Dehaze` | `hazeremoval` (strength) | Direct slider set |
| `Clarity2012` | `bilat` ("local contrast") | Not a 1:1 algorithm match with ACR's Clarity |
| `HueAdjustment*` / `SaturationAdjustment*` / `LuminanceAdjustment*` (per-color-band, up to 8 bands each: Red/Orange/Yellow/Green/Aqua/Blue/Purple/Magenta) | `colorzones` (hue / chroma / lightness curves) | Not a slider — see below |

The `colorzones` channels use a curve-tab switch plus a 2-call trick rather than a simple slider `set`, because colorzones is a curve-editor widget with three separate curves (hue, saturation, lightness), not sliders:

1. **Switch curve tab**: `dt.gui.action("iop/colorzones/channel", <tab>, "activate", 1.0)`, where `<tab>` is `"hue"`, `"chroma"` (= ACR Saturation), or `"lightness"` (= ACR Luminance). The widget is named `channel`, not `page` — found by reading darktable's actual C source (`src/iop/colorzones.c`), which also revealed the real tab names (`"chroma"`, not `"saturation"` — every earlier name-guess against a `page` widget had failed because of both the wrong widget name *and* the wrong page name).
2. **Force to floor**: `dt.gui.action("iop/colorzones/graph", <channel>, "down", 1000)` — a large nudge reliably clamps to the floor regardless of starting position.
3. **Land on target**: a single `up` call with a calibrated speed lands exactly on the target position.

The calibration (floor=2.0, neutral=2.5, ceiling=3.0, each unit of `speed` moves the node by 0.01) is identical across all three tabs and was derived + confirmed empirically against this darktable build; documented in `xmp_to_darktable.py`.

### ACR's parametric tone curve: solved, then shelved for stability

`ParametricShadows/Darks/Lights/Highlights` are ACR's 4-zone parametric tone curve — no darktable widget is shaped the same way. `toneequal` ("tone equalizer") looked like the ideal match: its "simple" mode has 9 plain EV-band sliders (`-8 EV` through `+0 EV`), confirmed to be genuine `dt_bauhaus_slider_from_params`-bound sliders by reading darktable's actual C source (`src/iop/toneequal.c`) — not a curve/graph widget like colorzones or colorbalancergb.

It initially looked dead: `dt.gui.action` accepted the calls (real, non-nan return values) but nothing ever landed in darktable's history table, no matter how the ordering, timing, or enable state was varied. **Root-caused by cloning darktable's source and instrumenting it directly** (adding `dt_print` probes to `_dev_add_history_item`/`_dev_auto_save` in `src/develop/develop.c` and rebuilding locally): darktable's autosave (the function that flushes in-memory history to the database *and* the `.xmp` sidecar) is not on an independent timer — it's checked reactively, only when another history-item event fires, and its elapsed-time condition can't pass until **~20 seconds** after darkroom entry (`autosave_time` is initialized to `now + 10s`, and the check itself needs another `+10s` past that). A script where every action fires in the first few seconds never crosses that threshold on its own, so nothing ever gets flushed — not a toneequal-specific bug, and not something requiring a darktable patch. Fixed with a trailing "flush trigger" in `apply_xmp_preset.lua`: wait ~22s, then nudge any always-present slider to fire one more history event, which flushes everything accumulated so far. Verified by decoding the actual committed parameter bytes for toneequal's `-8 EV` band and confirming it matched the target exactly. This also explains, in hindsight, why `colorzones` always worked: that pipeline runs long enough (waiting for the sidecar to settle) to incidentally cross the 20s mark, while an isolated single-action test never would.

**Currently disabled by default anyway** (`USE_TONEEQUAL_FOR_PARAMETRIC = False` in `xmp_to_darktable.py`): running the real production pipeline with toneequal's actions included crashed darktable — a GTK-internal memory-corruption abort, most likely inside its module-group panel re-layout code when several modules get enabled/switched in quick succession. This is a pre-existing darktable/GTK stability issue, not something introduced by our mapping (see the stability section below) — but it's real, so the accurate 9-band mapping is implemented and documented, ready to flip on, while the *default* path uses the safer fallback: blend all 6 values (`Highlights2012`, `Shadows2012`, and the 4 parametric zones) into `shadhi`'s two proven-reliable sliders, each touched exactly once. Coarser than a real 4-zone system (2 zones, not 4; different algorithm than ACR's), but stable.

## What we don't have, and why

| ACR attribute(s) | Why it's not supported |
|---|---|
| `ColorGrade*` (color-grading wheels: shadow/midtone/highlight/global hue+sat+lum) | Not actually a graphical-only widget — reading `colorbalancergb.c` found genuine sliders (`iop/colorbalancergb/lift/hue`, `.../gain/chroma`, etc. — sections `offset`/`lift`/`gain`/`power` = global/shadows/highlights/midtones), and `dt.gui.action` resolves all 12 of them with real values. But it's a confirmed dead end anyway: a byte-level comparison of the actual committed params blob (not just the call's return value) showed neither a direct `set` nor the `up`/`down` nudge trick that saved `colorzones` changes anything that actually gets saved — re-tested with the ~20s timing fix in place, still byte-identical. Same category as `toneequal` initially looked, but genuinely different: sliders on a module's non-default notebook page, as opposed to `colorzones`' custom graph widget, which has its own working commit path regardless of tab. The module's actual default page (`master`) works fine — that's why ACR's `Saturation` is already mapped there. |
| `SplitToningShadowHue/Saturation`, `SplitToningHighlightHue/Saturation` | Looked like a strong candidate on closer inspection: `splittoning.c` shows the shadow/highlight hue+saturation ARE genuine sliders (the color-swatch button next to them is just a visual companion, not the real control) on a single page with no notebook tabs at all — ruling out the "hidden tab" theory that explains `ColorGrade*`. Re-tested with the ~20s timing fix in place anyway: still a confirmed dead end, identical failure signature (real, non-nan `dt.gui.action` calls, nothing ever lands in history). So tab-visibility isn't the actual distinguishing factor between working and broken sliders — the real mechanism is still unidentified. Only `splittoning`'s plain `balance` slider is directly settable. |
| `Whites2012`, `Blacks2012` | `filmicrgb`'s `white relative exposure`/`black relative exposure` sliders (on its default `scene` page) looked like a real candidate — genuine sliders, real return values — but re-tested with the ~20s timing fix in place and still don't commit. Genuinely dead, not a timing issue. |
| `Texture` | No comparable darktable control. |

**Net coverage: 36 of 42 attributes** in a typical ACR "Look" preset (based on the reference preset used during development) — everything except the color-grading wheels, split-toning's color pickers, and a handful of fields with no darktable equivalent at all.

What's left of the color-grading/split-toning gap is genuinely graphical (2D color wheels, color-swatch buttons), not just unmapped. A possible way to close some of that: clicking the actual widget via screen coordinates (`cliclick`/`osascript`) instead of `dt.gui.action` — viable for a small fixed button (a color swatch), less so for a continuous 2D drag target (a color wheel). Not implemented.

## A note on stability

darktable 5.6.0 has an intermittent, pre-existing crash (a GTK-internal memory-corruption abort) that can be triggered by rapid `dt.gui.action` automation — confirmed not to be deterministically tied to any specific action, mapping, or module we could isolate: the *exact same* action file sometimes ran clean and sometimes crashed against a freshly-restarted darktable. This is a darktable/GTK stability issue, not a bug in this tool's code. `apply-lr-preset.sh` retries automatically (up to 3 attempts) if darktable crashes before writing the sidecar — a retry after a crash has consistently succeeded in testing, so this doesn't normally surface to you, but it's worth knowing about if a run ever takes longer than expected.

## How it works

1. **`xmp_to_darktable.py`** parses the ACR `crs:` attributes out of the `.xmp` preset file, maps each one through `mapping.json`, and writes a plain pipe-delimited action list (`element|effect|speed` per darktable's `dt.gui.action` signature).
2. **`lua/apply_xmp_preset.lua`** runs inside darktable itself. It watches for the `darkroom-image-loaded` event, reads the action list from an env var, and replays each action via `dt.gui.action` the moment the target photo opens in the darkroom.
3. **`apply-lr-preset.sh`** is the entry point — it drives both of the above, and optionally headlessly renders the result via `darktable-cli`.

## Setup

Requires darktable installed at `/Applications/darktable.app` (macOS). If your install path differs, edit `DARKTABLE_BIN` / `DARKTABLE_CLI_BIN` at the top of `apply-lr-preset.sh`.

No manual install step needed otherwise — the first run of `apply-lr-preset.sh` copies `lua/apply_xmp_preset.lua` into `~/.config/darktable/lua/` and registers it in `~/.config/darktable/luarc` automatically.

## Usage

**Open interactively in darktable's darkroom**, edits pre-applied, for review before you commit to anything:

```bash
./apply-lr-preset.sh <preset.xmp> <photo>
```

**Render headlessly** straight to an output file, no window interaction needed:

```bash
./apply-lr-preset.sh -o <output.jpg> <preset.xmp> <photo>
```

**Control preset strength** with `-p 1-100`, mirroring Lightroom's Amount slider — 100 applies the preset exactly as authored, and anything lower blends proportionally toward the untouched photo. Defaults to 50 if omitted:

```bash
./apply-lr-preset.sh -p 75 <preset.xmp> <photo>
./apply-lr-preset.sh -p 100 -o <output.jpg> <preset.xmp> <photo>
```

Run `./apply-lr-preset.sh -h` for full usage.

The headless flow: launches darktable in the background with the photo, lets the Lua script apply the edits, waits for darktable to auto-write the `.xmp` sidecar (it does this on its own, a few seconds after a history change — confirmed empirically, no explicit "save" call exists in darktable's Lua API), then force-quits that darktable instance and calls `darktable-cli` to render the final image from the sidecar.

**Important:** darktable only allows one running instance. Every `-o` run force-quits *any* darktable process currently running on your machine before starting, to guarantee a clean slate — including a window you have open for unrelated manual editing. There's no unsaved-work check; if you're actively using darktable, don't run this at the same time.

### Extending / fixing a mapping

If a mapping in `mapping.json` stops working on a newer darktable version (its internal action-path strings are version-specific), `apply_xmp_preset.lua` logs the exact failure to darktable's console (run with `-d lua` to see it): it reports the path, element, effect, and speed that failed. Look up the real path via darktable's **Preferences → Shortcuts** search (type the module name, the shown action string is the same one `dt.gui.action` uses), then fix the entry in `mapping.json`.

## Files

- `apply-lr-preset.sh` — entry point (interactive or `-o` headless)
- `xmp_to_darktable.py` — ACR XMP parser + action-list generator
- `mapping.json` — ACR attribute → darktable action mapping table, with per-entry notes on what's verified vs. best-guess
- `lua/apply_xmp_preset.lua` — runs inside darktable, replays the action list

## AI assisted

Claude
