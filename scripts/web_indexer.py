# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pintu masuk Web Indexer. Logika ada di scripts/webindex/ (memakai inti scripts/radar/)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webindex.main import main  # noqa: E402

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # jangan menunggu thread pekerja yang masih menggantung
