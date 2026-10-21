# Performance on the RK3588

The owner's production measurements (issue #19) put a text reply at about 80 s and a text and
image reply at 2.5–3 minutes on a 1280x1440 profile. This page lists what changed to reduce that,
why none of it loosens a verification, and how to measure the result on the board.

## Rule: speed without weaker checks

Every reply step still captures a fresh frame and makes the same decisions with the same
thresholds: title, latest incoming bubble, exact draft, safety probe, send/failure watch for
the whole `reply_verify_timeout_seconds`, and image evidence. The changes only avoid doing
*identical work twice* or replace a slow implementation with one that returns the same answer.

| Change | Where | Why accuracy is unchanged |
| --- | --- | --- |
| Exact-pixel OCR result cache (`XVM_OCR_CACHE_SIZE`) | `ocr/pool.py` | The key is the crop's exact bytes plus OCR mode. The OCR engines are deterministic for identical input, so a hit is the answer a fresh read would give. Any changed pixel (caret blink, new bubble, dialog) is a new key. `benchmark` checks the determinism assumption on your frames (`fresh_reads_identical`). |
| Incremental safety-probe OCR | `vision/analyzer.py` | After a full read with no hits, later frames read only the full-width bands that changed, widened by 40 px. Every text line is then either unchanged since a read without hits or fully inside a band read now. Positive states, keyword or size changes, very large changes and every 20th read use a full read. Tests compare it with fresh full reads. |
| Raw pixels to the OCR process | `ocr/pool.py` | PNG encode/decode is lossless, so sending raw pixels gives byte-identical input with less CPU. |
| OCR process pinned to the A76 cores (`XVM_OCR_CPU_AFFINITY=auto`), one ONNX Runtime thread per core | `cpu.py`, `ocr/pool.py` | Scheduling only. By default ONNX Runtime splits each operator across all 8 cores, and the slower A55 cores hold back every step. |
| Connected components via OpenCV | `vision/pane.py` | Same 4-connected components, boxes and pixel counts as the pure-Python flood fill (kept as fallback). Parity tests cover both. |
| Outgoing-image matcher: half-resolution screen, then a climb on the exact full-resolution score from the strongest coarse peaks, thumbnails cached | `vision/reply_image.py` | Every evaluated width goes through the original LANCZOS/normalized correlation/color check and selection rule unchanged, so acceptance is never looser. If a correlation peak fails the color test, all promising widths are evaluated, as the exhaustive search would. Parity tests compare it with the exhaustive search, and `benchmark --reference` repeats that on your frames. |
| Fewer full-frame RGB copies | `vision/analyzer.py` | Crop-then-convert equals convert-then-crop. |

Proposals from #19 that were **not** adopted, because they would weaken a check:

* **Searching only the lower 40–50 % of the message pane for the image.** The calibrated pane
  is only 272 px tall, and a sent thumbnail is usually taller than that band, so a real
  image could no longer be found.
* **Stopping send verification at the first matching sample.** A failed-send icon can
  appear after the bubble does, so watching for it until the end of the timeout is the
  point. With the caches each sample is much cheaper, so the window is close to the
  configured timeout instead of three or four slow samples. Shortening
  `reply_verify_timeout_seconds` is the owner's decision.
* **Composer-only draft verification.** The draft recheck also confirms the title and the
  unchanged latest incoming bubble (issue #16). With the cache those crops are not re-read
  unless their pixels changed, so the check stays complete.
* **Chrome DevTools/DOM inspection.** Out of scope: the project reads screenshots only.

## Measured on the board (#25)

LubanCat-4 RK3588, profile 4 (1280x1440, 18 rows), RapidOCR in a process pinned to CPUs 4–7, 4 threads:

| Stage | Uncached | With #20 |
| --- | --- | --- |
| Full list analysis | 8.8–12.9 s | 43–55 ms (identical pixels) |
| Pane analysis | 1.3–3.0 s | 12–19 ms (identical pixels) |
| Image matcher, image absent | 15.4 s | 0.5 s |
| Image matcher, image present | 15.4 s | 12.9–13.4 s with the first screen version; replaced by the exact-score climb, to be re-measured |
| Full safety-probe read | 8.4–12.7 s | unchanged when it must run (first read, large change); see #23 |

All agreement checks were true (`fresh_reads_identical`, `cached_matches_fresh`,
`image_match_identical`, `probe_incremental_matches_full`). The first read of changed content
remains expensive: the detector input sizes in #23 and an NPU backend (#24) are the next steps.

## Measuring on the board

Save a few screenshots locally (they stay private), for example the list, an open chat
before and after pasting a draft, and after sending. Then run:

```bash
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor \
  benchmark --profile profile.json --processes before.png after.png [--reference /path/reply.png]
```

The JSON output contains only timings and booleans:

* `analyze_uncached_ms` and `pane_uncached_ms`, compared with `analyze_cached_ms` and `pane_cached_ms`.
* `fresh_reads_identical`: OCR reads of the same pixels agree.
* `probe_full_ms` and `probe_incremental_ms`, plus `probe_incremental_matches_full`.
* `image_match_exhaustive_ms` and `image_match_fast_*_ms`, plus `image_match_identical`.

Repeat with `--cpus none` to compare pinning. Every `phase_seconds` entry of a reply's
verification JSON (Replies page) shows where time is spent, and the heartbeat reports OCR cache
hit ratio and probe full/incremental/reused counts.

If `fresh_reads_identical` is ever `false`, set `XVM_OCR_CACHE_SIZE=0` and report it.

## NPU (RKNPU2): feasible, and opt-in only after measuring

The RK3588 NPU (6 TOPS) can run PP-OCR. Rockchip's
[rknn_model_zoo](https://github.com/airockchip/rknn_model_zoo) ships PP-OCRv4 detection
(`ppocrv4_det`, INT8 or FP) and recognition (`ppocrv4_rec`) conversions for the RK3588. The
board side needs the `rknpu` kernel driver, `librknnrt.so` and `rknn-toolkit-lite2`. Before
using it in this project:

1. **Check the board.** Run `cat /sys/kernel/debug/rknpu/version` (or `dmesg | grep -i rknpu`)
   and confirm a `rknn-toolkit-lite2` wheel exists for the Conda Python version.
2. **Precision.** Recognition must stay FP16. INT8 recognition loses accuracy on CJK text.
   Detection INT8 needs calibration images, which must stay private.
3. **Static shapes.** RKNN models use fixed input sizes, while the CPU path uses dynamic widths.
   Recognition inputs must be padded or bucketed (e.g. 48x320, 48x640), and long previews split.
   This changes the model input, so results may differ from the CPU path.
4. **Shadow mode before switching.** Run NPU and CPU on the same crops, record disagreement
   rate and latency, and switch only after a private sample shows no disagreement on
   names, previews, bubbles, composer and probe keywords. Keep CPU as the fallback.

The CPU changes above remove most repeated OCR. Measure the remaining OCR time with `benchmark`
first. If it still dominates, the NPU work is tracked as a separate issue.
