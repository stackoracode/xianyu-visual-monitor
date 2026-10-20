# Compatibility

## Target

| Item | Measured on the board |
|---|---|
| CPU / arch | RK3588, aarch64 |
| OS | Ubuntu 20.04.6 LTS (focal) |
| glibc | 2.31 |
| System Python | 3.8.10 (**not used**) |
| Desktop | XFCE on Xvnc, 1280x720, depth 16, `-nolisten tcp` |
| Tools present | `scrot`, `xdotool`, Google Chrome |
| Memory / disk | 16 GB RAM, no swap; ~100 GB free |

## Runtime choice

* **Python 3.10 from Conda (Miniforge)**, installed into its own prefix. Conda-forge builds need
  glibc ≥ 2.17, so the system glibc stays untouched. Supported range: 3.10–3.12.
* All pinned wheels were checked for `aarch64` builds whose manylinux tag is ≤ glibc 2.31:

| Package | Version | aarch64 wheel tag |
|---|---|---|
| onnxruntime | 1.20.1 | manylinux_2_27 / 2_28 |
| rapidocr-onnxruntime | 1.3.24 | pure Python (PP-OCR ONNX models bundled) |
| opencv-python | 4.10.0.84 | manylinux2014 (needs `libgl1`, `libglib2.0-0`) |
| numpy | 1.26.4 | manylinux2014 |
| shapely / pyclipper | 2.0.6 / 1.3.0.post6 | manylinux2014 |
| pillow | 11.0.0 | manylinux2014 / 2_28 |
| pydantic-core | 2.27.2 | manylinux2014 |
| greenlet / SQLAlchemy | 3.1.1 / 2.0.36 | manylinux2014 |
| cryptography | 43.0.3 | manylinux2014 / 2_28 |
| fastapi, starlette, uvicorn, alembic, aiomysql, PyMySQL, jinja2 | see `requirements/base.txt` | pure Python |

* OCR alternative: `tesseract-ocr` 4.1.1 + `tesseract-ocr-chi-sim` from focal apt.
* MySQL: `mysql-server` 8.0 from focal apt (utf8mb4).

## What CI verifies

The `arm64-focal` job runs on a GitHub-hosted ARM64 runner inside the `arm64v8/ubuntu:20.04`
container (glibc 2.31). It installs Miniforge Python 3.10 and the pinned requirements, then:

1. records the platform, glibc, Python, onnxruntime/OpenCV/numpy versions and the full `pip freeze`
   (artifact `arm64-focal-artifacts`);
2. runs the whole test suite on SQLite and against MySQL 8.0 from focal apt;
3. runs **real RapidOCR and real Tesseract** on synthetic frames drawn with a CJK font;
4. captures a **live 1280x720, depth-16 Xvfb display with the production `scrot` backend** and runs
   `xdotool`/`xclip` input commands against it.

The x86-64 job additionally runs the suite on Python 3.10 and 3.12 against SQLite and the
official `mysql:8.0` image.

## What CI cannot verify (open items)

* Running on the physical RK3588 (CPU time per OCR batch, memory, thermal behavior). The
  container is the same architecture, distribution and glibc, but different hardware.
* Capture of the real Xvnc session (root-owned Xauthority, xrdp/sesman specifics) and real
  Chrome rendering.
* OCR accuracy on the real Xianyu UI fonts, sizes and colors; calibration coordinates and
  colors on the actual screen after any resize or zoom.
* `xdotool`/clipboard input into the real Chrome page (CJK paste via `xclip`), and the reply
  verification on the real conversation pane.
* SMTP delivery through the owner's provider.

These require the owner's controlled test window on the board; see
[DEPLOYMENT.md](DEPLOYMENT.md) and [LIMITATIONS.md](LIMITATIONS.md).
