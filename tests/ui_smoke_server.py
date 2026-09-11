from pathlib import Path
import os
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="liebiao-ui-smoke-"))
os.environ.setdefault("LIEBIAO_DATA_DIR", str(TEMP_ROOT / "data"))
os.environ.setdefault("LIEBIAO_CONFIG_DIR", str(TEMP_ROOT / "config"))
os.environ.setdefault("LIEBIAO_PORT", "8192")
os.environ.setdefault("LIEBIAO_UPDATE_ENABLED", "0")

import uvicorn


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8192, log_level="warning")
