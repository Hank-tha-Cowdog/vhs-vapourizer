#!/usr/bin/env python3
import vapoursynth as vs
from vapoursynth import core
import havsfunc as haf
import sys
import os

# Set thread count
core.num_threads = 10

def log_format(clip, stage_name):
    """Log format information for debugging"""
    if hasattr(clip, 'format') and clip.format is not None:
        info = f"[FORMAT] {stage_name}: {clip.width}x{clip.height}, {clip.format.name}, {clip.fps.numerator}/{clip.fps.denominator} fps"
        print(info, file=sys.stderr)
    else:
        print(f"[FORMAT] {stage_name}: Format information not available", file=sys.stderr)
    return clip

# ============================================================================
# INPUT FILE DETECTION
# ============================================================================
print("[VHS_PROCESSING] VapourSynth script starting...", file=sys.stderr)
input_file = os.environ.get('VS_INPUT_FILE')
if not input_file:
    raise ValueError("ERROR: No input file provided via VS_INPUT_FILE environment variable!")

if not os.path.exists(input_file):
    raise FileNotFoundError(f"ERROR: Input file does not exist: {input_file}")

print(f"[VHS_PROCESSING] Processing file: {input_file}", file=sys.stderr)

# ============================================================================
# LOAD SOURCE (PRESERVING NATIVE RESOLUTION)
# ============================================================================
print("[VHS_PROCESSING] Loading source with FFmpegSource2 to preserve original resolution...", file=sys.stderr)
try:
    # --- FIX: Use FFmpegSource2 to prevent automatic PAR correction and align with pipeline ---
    clip = core.ffms2.Source(source=input_file)
    log_format(clip, "Source loaded (Native Resolution)")

except Exception as e:
    raise RuntimeError(f"Could not load source with FFmpegSource2: {str(e)}")

# ============================================================================
# DEINTERLACING (WEAVE to 29.97fps)
# ============================================================================
print("[VHS_PROCESSING] Applying QTGMC deinterlacing (TFF, 29.97fps)...", file=sys.stderr)
try:
    # --- FIX: Corrected QTGMC settings for 29.97fps progressive output ---
    deinterlaced = haf.QTGMC(
        clip,
        Preset="Slower",
        TFF=True,
        FPSDivisor=2      # Ensures 29.97fps output from a 59.94fps source
    )
    deinterlaced = log_format(deinterlaced, "After QTGMC")
except Exception as e:
    raise RuntimeError(f"QTGMC failed: {e}")

# ============================================================================
# COLOR SPACE CONVERSION
# ============================================================================
print("[VHS_PROCESSING] Converting color space (bt601 → bt709)...", file=sys.stderr)
try:
    processed = core.resize.Spline36(
        deinterlaced,
        matrix_in_s="170m",    # bt601 (SD)
        matrix_s="709"         # bt709 (HD)
    )
    processed = log_format(processed, "After color space conversion")
except Exception as e:
    raise RuntimeError(f"Color space conversion failed: {e}")

# ============================================================================
# NEURAL UPSCALING (to 1520x976)
# ============================================================================
print("[VHS_PROCESSING] Applying NNEDI3 2x neural upscaling...", file=sys.stderr)
try:
    # Height doubling (488 -> 976)
    up = core.nnedi3.nnedi3(
        processed,
        field=0,     # Progressive frames from QTGMC
        dh=True,     # Double height
        nsize=4,
        nns=4,
        qual=2
    )
    log_format(up, "After NNEDI3 height doubling")

    # Width doubling (760 -> 1520)
    up = up.std.Transpose()
    up = core.nnedi3.nnedi3(
        up,
        field=0,     # Progressive frames
        dh=True,     # Double width (after transpose)
        nsize=4,
        nns=4,
        qual=2
    )
    up = up.std.Transpose()
    final = log_format(up, "Final output (2x Upscaled)")
except Exception as e:
    raise RuntimeError(f"NNEDI3 upscaling failed: {e}")

# ============================================================================
# OUTPUT
# ============================================================================
print("[VHS_PROCESSING] Pipeline complete. Setting output for Y4M piping...", file=sys.stderr)
final.set_output()
print("[VHS_PROCESSING] ✅ Script execution completed successfully", file=sys.stderr)
