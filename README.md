# vhs-vapourizer
Lossless VHS restoration with VapourSynth → Y4M piping → FFV1/ProRes, featuring QTGMC deinterlace, NNEDI3 upscaling, rich progress UI, profiling, and safe shutdown.

## VHS Y4M Pipeline

Lossless, reproducible VHS processing that routes VapourSynth frames over **Y4M** into FFmpeg for **FFV1 (10-bit 4:2:2)** or **ProRes 422 HQ** output. Includes robust progress/ETA UI, index caching, graceful teardown, and optional GPU and performance profiling.

> Core design: eliminate raw-pipe pixel-format mismatches by standardizing on Y4M, keep VHS field order correct (TFF), and preserve 10-bit 4:2:2 throughout.

---

### Highlights

* **Y4M piping** end-to-end (no raw-frame format mismatches)
* **FFV1 (default) or ProRes 422 HQ** output
* **QTGMC** deinterlacing (TFF) edimode=weave (not bobbed)
* **NNEDI3** upscaling 2x
* **bt601 → bt709** color conversion retained in encode flags
* **Rich** terminal UI (progress bar, ETA, FPS) + detailed logging
* **Index caching** (FFIndex/LWI) for fast restarts
* **Graceful shutdown** and cleanup on SIGINT/SIGTERM
* **Optional profiling** (cProfile) - generates performance profile
* **Vapoursynth logging** - generates a detailed log for Vapoursynth
* **GPU memory** check via `nvidia-smi`
* **Test mode**: process the first *N* frames for quick validation

---

### Requirements

**System**

* Python 3.8+ (venv recommended)
* FFmpeg (with `ffv1`, `prores_ks`)
* VapourSynth + `vspipe`

**VapourSynth plugins**

* `ffms2` (FFmpegSource2)
* `QTGMC` (and its deps, e.g., `mvtools`, `fmtconv`)
* `nnedi3` (or `znedi3`)

**Python packages**

* `tqdm`, `rich` (UI), `psutil` (optional), standard library modules
  *(All imported in `vs_pipeline.py`; optional modules degrade gracefully.)*

---

### Quick Start

1. **Place your input** somewhere accessible and set an output directory you can write to.

2. **Edit config** at the top of `vs_pipeline.py`:

   ```python
   INPUT_FILE_PATH = r"input\path\to\your\source\file.mkv"
   OUTPUT_DIRECTORY_PATH = r"output\path\to\final\file.mkv"

   TEST_MODE = True            # True = quick test run
   TEST_FRAME_COUNT = 1000     # frames to process in test mode
   USE_PRORES_OUTPUT = False   # False = FFV1 (default), True = ProRes 422 HQ
   ```

3. **Ensure `upscale.vpy` is in the same directory** as `vs_pipeline.py`.
   The pipeline sets `VS_INPUT_FILE` for the script, so the `.vpy` should read:

   ```python
   import os
   src_path = os.environ['VS_INPUT_FILE']
   # load source with ffms2, perform QTGMC/NNEDI3, etc...
   ```

4. **Run test mode** (fast validation):

   ```bash
   python vs_pipeline.py
   ```

   You’ll see a Rich progress UI, FPS, and an ETA. On success, an output file is written inside a timestamped subfolder like:

   ```
   OUTPUT_DIRECTORY_PATH/
     vapoursynth_run_YYYYMMDD_HHMMSS/
       <input_basename>_VHS_Y4M_YYYYMMDD_HHMMSS.mkv
   ```

5. **Full run**:

   ```python
   TEST_MODE = False
   ```

   and rerun `python vs_pipeline.py`.

---

### How It Works

**Stages**

1. Source load via **FFmpegSource2** (index cached as `.ffindex` or `.lwi`)
2. **QTGMC** deinterlace (TFF assumed for VHS; checks field order with `ffprobe`)
3. **bt601 → bt709** color conversion
4. **NNEDI3** upscaling (2×; configurable in `upscale.vpy`)
5. **vspipe → Y4M → FFmpeg**
6. Encode as **FFV1** (default, lossless 10-bit 4:2:2) or **ProRes 422 HQ**

**Why Y4M?**
Y4M carries width/height/fps/pix-fmt metadata in the stream header, which avoids the garbled outputs that often appear when raw YUV pipes disagree on format.

---

### Configuration Reference

In `vs_pipeline.py`:

* `TEST_MODE`, `TEST_FRAME_COUNT` — quick validation passes
* `USE_PRORES_OUTPUT` — switch between FFV1 (`.mkv`) and ProRes (`.mov`)
* Developer toggles:

  * `DEBUG_MODE`, `ENABLE_PERFORMANCE_PROFILING`, `ENABLE_GPU_MONITORING`, `ENABLE_DETAILED_TIMING`
  * Optional `PROCESS_DURATION` (seconds) to time-cap runs
* The script:

  * Detects NVIDIA via `nvidia-smi`
  * Creates `vapoursynth_run_<timestamp>/` for logs, output, and profile
  * Traps `CTRL+C` and SIGTERM to finalize/cleanup safely

---

### Output Controls

* **FFV1 (default)**

  * `-c:v ffv1 -level 3 -pix_fmt yuv422p10le`
  * `-color_range tv -colorspace bt709 -aspect 4:3`
* **ProRes 422 HQ**

  * `-c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le`
  * Same color/aspect flags as above

---

### Logging, Indexing, and Performance

* **Logs**: `vapoursynth_log_<timestamp>.txt` in the run folder
* **Indexing**: detects `.ffindex` or `.lwi` and skips re-indexing for fast restarts
* **Profiling**: `performance_profile.txt` with cumulative timings and average FPS

---

### Typical Commands (under the hood)

* **Script probe**:

  ```bash
  vspipe --info "upscale.vpy" -
  ```
* **Main pipeline (test mode)**:

  ```bash
  vspipe -c y4m "upscale.vpy" - | ffmpeg -f yuv4mpegpipe -i - \
    -c:v ffv1 -level 3 -pix_fmt yuv422p10le -aspect 4:3 \
    -color_range tv -colorspace bt709 -frames:v <TEST_FRAME_COUNT> \
    -y "<output_file>.mkv"
  ```

---

### Troubleshooting

* **“No frames processed”**
  Run `vspipe --info upscale.vpy -` to confirm the script loads. Check that `VS_INPUT_FILE` is set by the pipeline and that `upscale.vpy` reads it.

* **Garbled output / format mismatch**
  Ensure you are using **Y4M** piping (this repo does!). If you’ve modified commands, avoid raw YUV pipes unless every format flag is identical across tools.

* **Indexing takes ages**
  First run creates an index file; subsequent runs detect `.ffindex`/`.lwi` and skip indexing.

* **Wrong field order**
  The pipeline probes `field_order`. VHS is typically **TFF**; adjust QTGMC settings in `upscale.vpy` if your capture is different.

* **Small output file**
  A file under \~1 MB likely indicates early failure. Check logs for errors around `vspipe` startup, plugin loads, or FFmpeg encode.

---

### Roadmap

* Config file support (YAML) for per-project presets
* Audio ingest/pass-through with automatic delay handling
* Optional denoise/deband stages with toggles
* Linux/Windows path helpers and environment setup script

---

### Acknowledgements / Source

Vapoursynth community!
