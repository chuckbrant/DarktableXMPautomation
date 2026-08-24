# Context for resuming this project

Read this before doing anything else. It exists so a future session doesn't
re-derive or re-try things already settled below. Written 2026-08-23 when the
user put the project on hold mid-investigation.

## What this project is

Applies a Lightroom/ACR `.xmp` preset to a photo in darktable by driving
darktable's Lua `dt.gui.action` API (same mechanism real keyboard shortcuts
use). See `README.md` for the shipped tool's usage and full "what works /
what doesn't" tables -- that file is up to date and is the source of truth
for end-user-facing behavior. This file is about the *investigation*, not
the tool.

The shipped tool works and covers 36 of 42 typical ACR preset attributes.
**Nothing below blocks using the tool as-is.** This is about closing the
remaining gaps (`ColorGrade*`, `SplitToning*` hue/sat, `Whites2012`/
`Blacks2012`, `Texture`), which the user asked to keep chasing by patching
darktable itself.

## The confirmed, settled findings (don't re-investigate these)

1. **The `_dev_auto_save` ~20s timing gate is real and fixed.** darktable's
   autosave only re-checks its elapsed-time condition when another
   history-item event fires, and can't pass until ~20s after darkroom entry.
   Fixed via a trailing flush-trigger in `lua/apply_xmp_preset.lua`. This is
   why `toneequal`'s EV-band sliders work. Don't re-derive this -- it's
   already implemented and documented in `mapping.json`'s `parametric_note`.

2. **`colorbalancergb` wheels, `splittoning` hue/sat, and `filmicrgb`
   white/black relative exposure all share one symptom**: `dt.gui.action`
   returns real (non-nan) values, but the module NEVER appears in
   darktable's `history` table at all, even with the timing fix applied and
   verified against a byte-level `op_params` decode. This was re-tested
   multiple times, not a fluke.

3. **RULED OUT as the cause**: `DT_ACTION_TYPE_IOP_SECTION` (the pseudo-module
   `DT_IOP_SECTION_FOR_PARAMS` creates for grouping sliders into GUI
   sections). I initially thought this was the bug because
   `dt_iop_gui_changed` (`src/develop/imageop.c`) bails when
   `action->type != DT_ACTION_TYPE_IOP_INSTANCE`. **This is a dead end** --
   `dt_bauhaus_slider_from_widget` (`src/bauhaus/bauhaus.c`, called via
   `dt_bauhaus_slider_new_with_range_and_feedback` from
   `dt_bauhaus_slider_from_params`) already unwinds the section pseudo-module
   back to the real module via the existing
   `DT_IOP_SECTION_FOR_PARAMS_UNWIND` macro (`src/bauhaus/bauhaus.h:446`)
   before assigning `w->module`. So `w->module` for e.g.
   `colorbalancergb`'s `lift/hue` slider is already the real live module
   instance, not the section stand-in. Confirmed by reading the macro's
   definition and usage sites. **Do not re-chase this theory.**

4. **Also ruled out**: notebook-tab visibility. Explicitly switched
   `colorbalancergb`'s "4 ways" tab via `dt.gui.action` before setting the
   wheel sliders -- still byte-identical, no commit. And `splittoning`'s
   failing sliders aren't behind any tab at all (single page, plain
   sections), yet fail identically. So "wrong tab selected" isn't it either.

5. **Real fixes shipped and working, don't redo**: `apply-lr-preset.sh`'s
   retry-on-crash loop (darktable 5.6.0 has an intermittent, pre-existing
   GTK-internal crash under rapid automation -- unrelated to any of this,
   just a stability mitigation), and the shadhi-blend approximation for
   `ParametricShadows/Darks/Lights/Highlights` (the more accurate
   `toneequal`-based mapping exists but is disabled via
   `USE_TONEEQUAL_FOR_PARAMS = False` in `xmp_to_darktable.py` because
   enabling toneequal in the real pipeline triggers that crash more).

## Where the investigation actually stands (unresolved)

The open question: **why does `dt_iop_gui_changed` (or something it calls)
never result in a history commit for these specific sliders, given `w->module`
is confirmedly the real module?** Candidates not yet ruled out:

- `_slider_value_change`'s own guard: `if(d->is_changed && !d->timeout_handle)`
  -- never confirmed live whether `is_changed`/`timeout_handle` are in the
  expected state for these specific sliders vs. working ones.
- The `if(*f != prevf)` check right before calling `dt_iop_gui_changed` --
  never confirmed the write to `w->field` actually differs from the previous
  value at the moment of the call (should differ given our test values vs.
  defaults, but not independently verified byte-for-byte at that exact
  moment).
- Whether `dt_iop_gui_changed` is reached at all for these modules, and with
  what `action->type` -- a probe was written for this
  (`src/develop/imageop.c`, `dt_iop_gui_changed`, tagged
  `[RESEARCH_PROBE3]`) but never got a clean live confirmation (see below).
- `_dev_add_history_item_ext`'s internals (not yet read carefully) -- may
  have its own dedup/no-op logic that silently skips certain modules.

**I do not have a confirmed root cause yet.** Don't assume one from earlier
messages in a resumed conversation -- re-read this file's "ruled out" list
above instead of re-testing those theories.

## The research clone and its exact current state

Location: `/Volumes/Claude/darktable-research/darktable` (a shallow git clone
of upstream darktable, NOT part of this repo, NOT the user's real
`/Applications/darktable.app` -- fully separate). Built via
`cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$(brew --prefix)" ..`
+ `ninja -j$(sysctl -n hw.ncpu)` from a `build/` subdir. Binary at
`build/bin/darktable`.

As of the pause, it has **uncommitted, in-progress instrumentation** (`git
status --short` in that clone shows `src/bauhaus/bauhaus.c`,
`src/develop/develop.c`, `src/develop/imageop.c` modified):

- `src/develop/develop.c`: `dt_print(DT_DEBUG_ALWAYS, "[RESEARCH_PROBE] ...")`
  probes inside `_dev_add_history_item()` and `_dev_auto_save()` (from the
  earlier, already-successful toneequal timing investigation -- these are
  fine, low-volume, safe to keep).
- `src/bauhaus/bauhaus.c`: one remaining probe inside `_slider_set_normalized`
  logging `reset`/`in_gui_update`/`realized`/`mapped` (also from the earlier
  investigation, safe, low-volume). **A second, riskier probe was added and
  then reverted** inside `_slider_value_change` (it did a `strstr` on
  `((dt_iop_module_t*)a)->op` gated by `action->type` -- this consistently
  preceded a hang in live testing and was removed; if you see it again,
  remove it, it's not needed).
- `src/develop/imageop.c`: a probe inside `dt_iop_gui_changed`, tagged
  `[RESEARCH_PROBE3]`, logging
  `ENTRY op=... type=...` for ops starting with c/f/s, or `BAIL action=...
  type=...` otherwise. **This is the probe to use next.** It's low-volume
  and safe (no risky casts) -- the hangs described below happened with a
  *different, already-removed* probe, not this one, but it was never
  actually confirmed clean because the hang recurred anyway (see next
  section). Re-verify it's not itself implicated before trusting its output.

**Rebuild after any edit**: `cd /Volumes/Claude/darktable-research/darktable/build && ninja darktable`.

## The test harness problem (read this before live-testing again)

Every attempt in the final session hung deterministically at the same point
(internal darktable time ~5-7s, right after firing the test's notebook
tab-switch action, before any further probe output). This happened across
multiple different probes (including ones with no risky code), across a
fresh isolated `--configdir`, and with `--disable-opencl` -- ruling out my
own instrumentation and OpenCL kernel recompilation as the cause. The one
run that ever succeeded cleanly was the very first attempt, before this
research darktable binary had been `kill -9`'d many times in a row.

**Working hypothesis**: repeated `kill -9` against this specific darktable
process/window-server session degraded some GTK/session state, causing a
deterministic hang on later launches -- this matches a pattern already
documented for the *production* app in the main Claude memory
(`feedback_scratch_volume`-adjacent lore, not in this repo) where a normal
quit clears it but another force-kill doesn't. **Not confirmed.**

**Before resuming live tests**: try a normal, non-forced quit cycle (or a
reboot) rather than immediately going back to `pkill -9` loops. If the very
next launch of the research binary also hangs at the same point even fresh
after a reboot, that disproves the degraded-session hypothesis and points
back at something real in darktable's notebook-tab-switch handling under
headless/off-screen rendering specifically -- worth chasing at that point.

## Test harness recipe (what worked, when it worked)

```bash
RESEARCH_CFG=<some scratch dir>/dt_research_cfg
mkdir -p "$RESEARCH_CFG/lua"
cp ~/.config/darktable/lua/apply_xmp_preset.lua "$RESEARCH_CFG/lua/"
echo 'require "apply_xmp_preset"' > "$RESEARCH_CFG/luarc"
cp ~/Desktop/test.jpg <scratch dir>/research_test.jpg   # a fresh copy, not imgid 69 in the real db

DT_XMP_PRESET_ACTIONS=<actions.txt> \
  /Volumes/Claude/darktable-research/darktable/build/bin/darktable \
  --configdir "$RESEARCH_CFG" -d lua <scratch dir>/research_test.jpg \
  > <log file> 2>&1 &
DT_PID=$!
sleep 2
timeout 8 osascript -e "tell application \"System Events\" to set frontmost of first process whose unix id is $DT_PID to true" >/dev/null 2>&1
# wait, then grep the log for "[apply_xmp_preset] applying preset from" (confirms
# actions fired) and then wait ~22s more for "flush-triggered after 22s wait"
# (confirms the trailing flush ran) before checking history/op_params.
```

Actions file format (pipe-delimited, matches what `apply_xmp_preset.lua`
parses): `crs_key|path|element|effect|speed|enable_module` (enable_module
empty if not needed, only set it on one line per module).

To check what actually committed, decode the real params bytes rather than
trusting `dt.gui.action`'s return value or the Lua "N failed" count -- both
give false positives:

```bash
sqlite3 ~/.config/darktable/library.db \
  "SELECT hex(op_params) FROM history WHERE imgid=<id> AND operation='<module>';"
```
then `struct.unpack` in Python against the real params struct (read from the
module's `.c` file in the research clone -- field order and types must match
exactly, including any un-obvious trailing fields).

## Recommended next step when resuming

1. Do a clean, non-`kill -9` restart cycle first (see harness problem above).
2. Re-run the `colorbalancergb` tab-switch + wheel-slider test
   (`colorbalancergb_tabtest_actions.txt` pattern above -- switch to page
   `"4 ways"` via `iop/colorbalancergb/page`, then set `iop/colorbalancergb/lift/hue`
   and `iop/colorbalancergb/lift/chroma`) against the build with the
   `[RESEARCH_PROBE3]` probe in `dt_iop_gui_changed` already in place.
3. If `[RESEARCH_PROBE3] ENTRY op=colorbalancergb type=1` (or whatever
   `DT_ACTION_TYPE_IOP_INSTANCE`'s actual enum value is -- check
   `src/common/action.h`) appears, the bug is downstream of
   `dt_iop_gui_changed` -- move the probe into `_dev_add_history_item_ext`
   itself (`src/develop/develop.c`) and read that function fully first,
   since it hasn't been read carefully yet.
4. If a `BAIL` line appears instead, that's new information contradicting
   finding #3 above (the section-unwind theory) -- re-examine why `w->module`
   isn't `DT_ACTION_TYPE_IOP_INSTANCE` after all in this live case.
5. If it just hangs again even after a clean restart, stop trying to fix the
   harness and pivot to pure static reading of `_dev_add_history_item_ext`
   (and whatever it calls) to form a best-guess patch, to be verified
   whenever a working live-test setup becomes available.

## Do not forget

- The research clone is NOT this git repo and has no relationship to it --
  don't expect `git log`/`git status` in `darktable-workflows` to show any of
  this.
- The user's real `/Applications/darktable.app` has never been touched or
  patched. All patching so far is confined to the research clone.
- Any actual fix, once found, needs a decision from the user about whether to
  build and swap in a patched `/Applications/darktable.app` for real use, or
  upstream the fix, or something else -- don't just do it.
