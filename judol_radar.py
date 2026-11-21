# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pintu masuk Radar Judol v3 (path lama tetap dipakai workflow). Logika ada di scripts/radar/."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.main import main  # noqa: E402

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # jangan menunggu thread DNS yang menggantung
