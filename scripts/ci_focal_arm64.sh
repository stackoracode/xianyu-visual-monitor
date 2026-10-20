#!/usr/bin/env bash
# Runs inside an arm64 ubuntu:20.04 container (glibc 2.31) to approximate the target board:
# system packages from focal, Python 3.10 from Conda (Miniforge), pinned wheels from PyPI.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p artifacts
uname -m | tee artifacts/platform.txt
# Consume the complete stream: head can close early and trip pipefail with SIGPIPE (141).
ldd --version | sed -n '1p' | tee -a artifacts/platform.txt
grep PRETTY_NAME /etc/os-release | tee -a artifacts/platform.txt

apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl bzip2 git libgl1 libglib2.0-0 \
  tesseract-ocr tesseract-ocr-chi-sim fonts-wqy-microhei scrot xdotool xclip xvfb mysql-server >/dev/null
tesseract --version 2>&1 | sed -n '1p' | tee -a artifacts/platform.txt

curl -fsSL -o /tmp/miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash /tmp/miniforge.sh -b -p /opt/conda >/dev/null
/opt/conda/bin/conda create -y -q -p /opt/xvm python=3.10 >/dev/null
PY=/opt/xvm/bin/python
$PY --version | tee -a artifacts/platform.txt
$PY -m pip install -q --upgrade pip
$PY -m pip install -q -r requirements/ocr.txt -r requirements/dev.txt
$PY -m pip install -q --no-deps -e .
$PY -m pip freeze > artifacts/pip-freeze-focal-arm64-py310.txt
$PY -c "import onnxruntime, rapidocr_onnxruntime, cv2, numpy; print('onnxruntime', onnxruntime.__version__, 'cv2', cv2.__version__, 'numpy', numpy.__version__)" | tee -a artifacts/platform.txt

# Live X11 smoke test: a 1280x720 depth-16 Xvfb display stands in for the board's Xvnc.
Xvfb :10 -screen 0 1280x720x16 -nolisten tcp >/dev/null 2>&1 &
sleep 2
export XVM_TEST_X_DISPLAY=:10

# MySQL 8 from focal: run the full suite against it as well.
MYSQL_OK=0
if (service mysql start >/dev/null 2>&1 || (mysqld_safe >/dev/null 2>&1 &) ); then
  for _ in $(seq 1 30); do mysqladmin ping >/dev/null 2>&1 && MYSQL_OK=1 && break; sleep 1; done
fi
if [ "$MYSQL_OK" = 1 ]; then
  mysql -e "CREATE DATABASE xvm_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER 'xvm'@'127.0.0.1' IDENTIFIED BY 'xvm-test-pw'; GRANT ALL ON xvm_test.* TO 'xvm'@'127.0.0.1';"
  mysql --version | tee -a artifacts/platform.txt
fi

$PY -m pytest -q -rs -p no:cacheprovider --junitxml=artifacts/junit-focal-sqlite.xml 2>&1 | tee artifacts/pytest-focal-sqlite.txt
if [ "$MYSQL_OK" = 1 ]; then
  XVM_TEST_MYSQL_URL='mysql+aiomysql://xvm:xvm-test-pw@127.0.0.1:3306/xvm_test?charset=utf8mb4' \
    $PY -m pytest -q -rs -p no:cacheprovider --junitxml=artifacts/junit-focal-mysql.xml 2>&1 | tee artifacts/pytest-focal-mysql.txt
else
  echo "MySQL could not be started in the container; MySQL tests ran only in the x86-64 job" | tee -a artifacts/platform.txt
fi
# Real-OCR output details for the record
$PY -m pytest -q -s -m real_ocr -p no:cacheprovider tests/test_real_ocr.py 2>&1 | tee artifacts/real-ocr-focal.txt
