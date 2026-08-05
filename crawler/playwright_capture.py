"""Medi25 공개 공고 페이지 Playwright 캡처 시험기.

로그인이나 CAPTCHA를 우회하지 않는다. 접근 가능한 공개 페이지의 렌더링 결과와
전체 페이지 스크린샷을 저장해 이후 Bedrock Vision 입력으로 사용할 수 있게 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    "https://www.medi25.com/html/odition_1/"
    "odition_detail_read_step1_m.php?seq=12204"
)
DEFAULT_LIST_URL = "https://trialforme.konect.or.kr/clnctest/list.do"
DETAIL_PATH_MARKERS = ("/clnctest/view.do", "/clnctrial/clncView.do")
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
    source = (
        "KoreaClinicalTrials"
        if any(host in url for host in ("koreaclinicaltrials.org", "trialforme.konect.or.kr"))
        else "Medi25"
    )
    prefix = "kct" if source == "KoreaClinicalTrials" else "medi25"
    screenshot_path = output_dir / f"{prefix}_{page_id}_{captured_at}.png"
    metadata_path = output_dir / f"{prefix}_{page_id}_{captured_at}.json"
    document_path = output_dir / f"{prefix}_{page_id}_{captured_at}.txt"

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
        sections = [page.locator("body").inner_text(timeout=timeout_ms).strip()]
        # KONECT 상세는 한 URL 안의 탭으로 선정·제외 기준을 숨긴다. full_page
        # screenshot도 현재 탭만 담으므로 각 공개 탭을 눌러 텍스트를 합친다.
        for label in (
            "대상자 선정기준",
            "대상자 제외기준",
            "연구설계 및 수행방법",
        ):
            target = page.get_by_text(label, exact=True)
            if not target.count():
                continue
            try:
                target.last.click(timeout=3_000)
                page.wait_for_timeout(300)
                visible = page.locator("body").inner_text(timeout=timeout_ms).strip()
                sections.append(f"\n[{label}]\n{visible}")
            except PlaywrightTimeoutError:
                continue
        body_text = "\n".join(dict.fromkeys(sections))
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    # 공개 페이지이고 연락처 마스킹을 마친 경우에만 파서 입력 문서를 저장한다.
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

        # 로컬 파일에는 수집 시각을 남기되 S3 키는 URL 해시로 고정한다. 같은
        # 공고를 정기 수집할 때 timestamp 키를 계속 만들면 CriteriaStore에도
        # 중복 버전이 끝없이 쌓인다. 고정 키를 덮어쓰면 원문 최신화는 유지하면서
        # 공고별 파이프라인 입력 키는 안정적으로 재사용할 수 있다.
        document_path.write_text(body_text, encoding="utf-8")
        evidence_key = f"raw/trials/screenshots/{prefix}_{page_id}.png"
        s3_key = f"trials/documents/{prefix}_{page_id}.txt"
        canonical_trial_id = ""
        try:
            from boto3.dynamodb.conditions import Attr

            criteria_table = boto3.resource("dynamodb").Table("CriteriaStore")
            existing: list[dict] = []
            scan_args: dict = {
                "FilterExpression": Attr("source_key").contains(page_id),
                "ProjectionExpression": "trial_id",
            }
            while not existing:
                response = criteria_table.scan(**scan_args)
                existing = response.get("Items", [])
                last_key = response.get("LastEvaluatedKey")
                if existing or not last_key:
                    break
                scan_args["ExclusiveStartKey"] = last_key
            if existing:
                canonical_trial_id = str(existing[0].get("trial_id") or "")
        except Exception:
            # 로컬 파일 캡처는 AWS 조회 없이도 가능해야 한다.
            canonical_trial_id = ""
        if not canonical_trial_id:
            canonical_trial_id = "SRC-" + hashlib.sha256(
                f"{s3_prefix.rstrip('/')}/{prefix}_{page_id}.png".encode("utf-8")
            ).hexdigest()[:16]
        boto3.client("s3").upload_file(
            str(screenshot_path),
            s3_bucket,
            evidence_key,
            ExtraArgs={
                "ContentType": "image/png",
                "Metadata": {
                    "source-url": quote(final_url, safe=":/?&=%#"),
                    "source-url-sha256": page_id,
                    "screenshot-sha256": metadata["screenshot_sha256"],
                    "source": prefix,
                    "captured-at": captured_at,
                },
            },
        )
        boto3.client("s3").upload_file(
            str(document_path),
            s3_bucket,
            s3_key,
            ExtraArgs={
                "ContentType": "text/plain; charset=utf-8",
                "Metadata": {
                    "source-url": quote(final_url, safe=":/?&=%#"),
                    "source-url-sha256": page_id,
                    "canonical-trial-id": canonical_trial_id,
                    "source": prefix,
                    "captured-at": captured_at,
                },
            },
        )
        metadata["s3_uri"] = f"s3://{s3_bucket}/{s3_key}"
        metadata["screenshot_s3_uri"] = f"s3://{s3_bucket}/{evidence_key}"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {**metadata, "metadata": str(metadata_path)}


def discover_detail_urls(
    list_url: str,
    *,
    limit: int,
    timeout_ms: int = 30_000,
) -> list[str]:
    """공개 목록에서 서로 다른 임상시험 상세 URL을 발견한다.

    검색 결과의 현재 페이지만 읽고, 로그인·CAPTCHA·페이지 제한을 우회하지 않는다.
    URL fragment는 제거하고 DOM 순서를 유지한 채 중복을 제거한다.
    """

    list_url = _validated_url(list_url)
    if limit < 1 or limit > 50:
        raise ValueError("한 번에 수집할 공고 수는 1~50이어야 합니다.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="ko-KR",
        )
        page.goto(list_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass
        hrefs = page.locator("a[href]").evaluate_all(
            "elements => elements.map(element => element.getAttribute('href') || '')"
        )
        browser.close()

    discovered: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        # 현재 KCT 목록은 일반 링크 대신 javascript:goView('202600593')를 쓴다.
        # 사이트 함수를 실행하지 않고 공개 상세 URL로만 안전하게 변환한다.
        match = re.fullmatch(r"javascript:goView\(['\"]([0-9]+)['\"]\);?", href.strip())
        if match:
            absolute = urljoin(
                list_url,
                f"/clnctest/view.do?clncTestSn={match.group(1)}",
            )
        else:
            absolute = urljoin(list_url, href).split("#", 1)[0]
        if not any(marker in urlparse(absolute).path for marker in DETAIL_PATH_MARKERS):
            continue
        try:
            absolute = _validated_url(absolute)
        except ValueError:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        discovered.append(absolute)
        if len(discovered) >= limit:
            break
    return discovered


def capture_batch(
    list_url: str,
    output_dir: Path,
    *,
    limit: int,
    delay_seconds: float,
    s3_bucket: str | None = None,
    s3_prefix: str = "trials/screenshots/",
) -> dict:
    """목록에서 발견한 공개 공고를 저속으로 캡처하고 선택적으로 S3에 올린다."""

    if delay_seconds < 1:
        raise ValueError("공개 사이트 부하를 줄이기 위해 요청 간격은 최소 1초입니다.")
    urls = discover_detail_urls(list_url, limit=limit)
    captured: list[dict] = []
    failed: list[dict[str, str]] = []
    for index, url in enumerate(urls):
        try:
            captured.append(
                capture(
                    url,
                    output_dir,
                    s3_bucket=s3_bucket,
                    s3_prefix=s3_prefix,
                )
            )
        except Exception as exc:  # 한 공고 실패가 전체 배치를 중단하지 않게 한다.
            failed.append({"url": url, "error": str(exc)})
        if index + 1 < len(urls):
            time.sleep(delay_seconds)
    return {
        "list_url": list_url,
        "discovered": len(urls),
        "captured": captured,
        "failed": failed,
    }


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
    parser.add_argument(
        "--list-url",
        help="지정하면 공개 목록에서 서로 다른 상세 URL을 찾아 배치 캡처",
    )
    parser.add_argument("--limit", type=int, default=10, help="배치 최대 공고 수 (1~50)")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.5,
        help="공고별 요청 간격, 최소 1초",
    )
    args = parser.parse_args()
    result = (
        capture_batch(
            args.list_url or DEFAULT_LIST_URL,
            args.output_dir,
            limit=args.limit,
            delay_seconds=args.delay_seconds,
            s3_bucket=args.s3_bucket,
            s3_prefix=args.s3_prefix,
        )
        if args.list_url
        else capture(
            args.url,
            args.output_dir,
            s3_bucket=args.s3_bucket,
            s3_prefix=args.s3_prefix,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
