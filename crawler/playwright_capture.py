"""Medi25 공개 공고 페이지 Playwright 캡처 시험기.

로그인이나 CAPTCHA를 우회하지 않는다. 접근 가능한 공개 페이지의 렌더링 결과와
전체 페이지 스크린샷을 저장해 이후 Bedrock Vision 입력으로 사용할 수 있게 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    "https://www.medi25.com/html/odition_1/"
    "odition_detail_read_step1_m.php?seq=12204"
)
ALLOWED_HOSTS = {
    "medi25.com",
    "www.medi25.com",
    "koreaclinicaltrials.org",
    "www.koreaclinicaltrials.org",
    "trialforme.konect.or.kr",
}


def _validated_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("허용된 임상시험 포털의 공개 HTTPS URL만 캡처할 수 있습니다.")
    return value


def capture(
    url: str,
    output_dir: Path,
    timeout_ms: int = 30_000,
    *,
    s3_bucket: str | None = None,
    s3_prefix: str = "trials/screenshots/",
) -> dict:
    url = _validated_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    page_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    source = "KoreaClinicalTrials" if "koreaclinicaltrials.org" in url else "Medi25"
    prefix = "kct" if source == "KoreaClinicalTrials" else "medi25"
    screenshot_path = output_dir / f"{prefix}_{page_id}_{captured_at}.png"
    metadata_path = output_dir / f"{prefix}_{page_id}_{captured_at}.json"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="ko-KR",
            device_scale_factor=1,
        )
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            # 광고/분석 요청이 계속되어도 DOM 캡처는 진행한다.
            pass
        title = page.title().strip()
        final_url = page.url
        if urlparse(final_url).hostname not in ALLOWED_HOSTS:
            browser.close()
            raise ValueError(f"허용되지 않은 도메인으로 이동했습니다: {final_url}")
        redaction_count = page.evaluate(r"""
            () => {
              const email = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
              const phone = /(?:\+?82[- ]?)?0(?:2|\d{2})[- ]?\d{3,4}[- ]?\d{4}/g;
              const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT
              );
              let count = 0;
              while (walker.nextNode()) {
                const node = walker.currentNode;
                const before = node.nodeValue || '';
                const after = before
                  .replace(email, () => { count += 1; return '[이메일 삭제]'; })
                  .replace(phone, () => { count += 1; return '[전화번호 삭제]'; });
                if (after !== before) node.nodeValue = after;
              }
              return count;
            }
        """)
        body_text = page.locator("body").inner_text(timeout=timeout_ms).strip()
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    # 본문 원문은 로그인 사용자 정보가 섞일 수 있어 파일로 저장하지 않는다.
    # Bedrock 전달 시에도 화면에 계정 정보가 없는 공개 공고인지 먼저 검토해야 한다.
    login_gate = (
        "/login/" in final_url
        or "회원로그인" in title
        or any(
            marker in body_text
            for marker in ("로그인 후", "로그인이 필요", "접근 권한")
        )
    )
    metadata = {
        "source": source,
        "requested_url": url,
        "final_url": final_url,
        "http_status": response.status if response else None,
        "title": title,
        "captured_at": captured_at,
        "screenshot": str(screenshot_path),
        "screenshot_sha256": hashlib.sha256(screenshot_path.read_bytes()).hexdigest(),
        "body_character_count": len(body_text),
        "redaction_count": redaction_count,
        "login_or_access_gate_detected": login_gate,
    }
    if s3_bucket:
        if login_gate:
            raise ValueError("로그인/접근 제한 화면은 공고 파이프라인에 업로드하지 않습니다.")
        import boto3

        s3_key = f"{s3_prefix.rstrip('/')}/{screenshot_path.name}"
        boto3.client("s3").upload_file(
            str(screenshot_path),
            s3_bucket,
            s3_key,
            ExtraArgs={
                "ContentType": "image/png",
                "Metadata": {
                    "source-url": quote(final_url, safe=":/?&=%#"),
                    "source-url-sha256": page_id,
                    "screenshot-sha256": metadata["screenshot_sha256"],
                    "source": prefix,
                },
            },
        )
        metadata["s3_uri"] = f"s3://{s3_bucket}/{s3_key}"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {**metadata, "metadata": str(metadata_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
    )
    parser.add_argument("--s3-bucket", help="설정하면 캡처를 trials/ 아래에 업로드")
    parser.add_argument("--s3-prefix", default="trials/screenshots/")
    args = parser.parse_args()
    print(json.dumps(capture(
        args.url,
        args.output_dir,
        s3_bucket=args.s3_bucket,
        s3_prefix=args.s3_prefix,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

