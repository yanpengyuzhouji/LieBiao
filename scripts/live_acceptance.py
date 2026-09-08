from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters import make_adapter


SAMPLES = {
    "ecp": "https://ecp.sgcc.com.cn/ecp2.0/portal/#/doc/doci-bid/2609030023176732_2018032900295987",
    "sgcc": "https://sgccetp.com.cn/portal/#/doc/doci-bid/2026083128890530_2018032700291334_2026083128893500/old",
    "csg": "http://www.bidding.csg.cn/zbgg/1200437842.jhtml",
}
BASES = {
    "ecp": "https://ecp.sgcc.com.cn/ecp2.0/portal/#/",
    "sgcc": "https://sgccetp.com.cn/portal/#/",
    "csg": "https://www.bidding.csg.cn/",
}


def check_platform(code: str, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        adapter = make_adapter(code, BASES[code])
        try:
            summaries = adapter.list_notices(max_pages=1, max_notices=2)
            if not summaries:
                raise AssertionError("列表为空")
            notice = adapter.fetch_notice(SAMPLES[code])
            if not notice.title or len(notice.body_text) < 100:
                raise AssertionError("详情内容不完整")
            if notice.attachments:
                with adapter.client.stream("GET", notice.attachments[0].url) as response:
                    response.raise_for_status()
                    first_chunk = next(response.iter_bytes(16), b"")
                    if code in {"ecp", "sgcc"} and not first_chunk.startswith(b"PK"):
                        raise AssertionError("公告附件不是 ZIP")
            return f"PASS {code}: list={len(summaries)}, id={notice.external_id}, body={len(notice.body_text)}, files={len(notice.attachments)}"
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
        finally:
            adapter.close()
    return f"FAIL {code}: {last_error}"


def main() -> None:
    results = [check_platform(code) for code in SAMPLES]
    for result in results:
        print(result)
    if any(result.startswith("FAIL") for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
