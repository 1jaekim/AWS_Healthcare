"""공개 임상시험 공고 수집 Lambda.

한국임상시험참여포털(KONECT)의 공개 목록에서 상세 공고를 찾아 본문을 모아
S3 에 올린다. EventBridge 스케줄이 주기적으로 깨운다.

브라우저를 쓰지 않는다
──────────────────────
이 사이트는 선정·제외 기준을 탭 안에 숨기고 `sub/tab.do` AJAX 로 채운다.
예전에는 Playwright 로 Chromium 을 띄워 탭을 클릭했지만, 같은 내용을 평범한
GET 요청으로 받을 수 있다. 실측으로 탭 3개 합계 5,262자를 받았고 Chromium
캡처(4,629자)보다 오히려 많다.

그래서 이 Lambda 는 표준 라이브러리 + boto3 만 쓴다. 컨테이너 이미지도, ECR
도, Chromium 도 필요 없다. 사이트가 탭을 클라이언트 렌더링으로 바꾸면
`_fetch_tabs` 가 빈 문자열을 돌려주므로 `body_character_count` 로 감지된다.

산출물
──────
| S3 키 | 역할 |
|-------|------|
| `trials/documents/{prefix}_{page_id}.txt` | 기준 추출 입력. EventBridge 가 감지해 파이프라인을 시작한다 |
| `raw/trials/html/{prefix}_{page_id}.html` | 감사 원본. 우리가 실제로 파싱한 바이트 그대로 |

키는 원본 URL 해시로 고정한다. 같은 공고를 다시 수집하면 덮어쓰므로 타임스탬프
키가 끝없이 쌓이지 않는다. `trial_id` 는 여기서 정하지 않는다 —
`protocol_parser` 가 실제로 파싱한 `source_key` 로 만든다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BUCKET = os.environ.get("S3_BUCKET_NAME", "")
LIST_URL = os.environ.get(
    "CRAWL_LIST_URL", "https://trialforme.konect.or.kr/clnctest/list.do"
)
LIMIT = int(os.environ.get("CRAWL_LIMIT", "10"))
DELAY_SECONDS = float(os.environ.get("CRAWL_DELAY_SECONDS", "1.5"))
TIMEOUT_SECONDS = int(os.environ.get("CRAWL_TIMEOUT_SECONDS", "30"))

USER_AGENT = "Mozilla/5.0 (compatible; clinical-trial-matching/1.0)"

# 공개 임상시험 포털만 허용한다. 이벤트로 임의 URL 이 들어와도 여기서 막힌다.
ALLOWED_HOSTS = frozenset(
    {
        "trialforme.konect.or.kr",
        "koreaclinicaltrials.org",
        "www.koreaclinicaltrials.org",
    }
)

# 상세 페이지가 탭 본문을 채울 때 쓰는 번호. 페이지 자체가 2·3·4 를 읽어온다.
CONTENT_TAB_NUMBERS = (2, 3, 4)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"(?:\+?82[- ]?)?0(?:2|\d{2})[- ]?\d{3,4}[- ]?\d{4}")


class CrawlError(RuntimeError):
    """수집 실패. 한 건의 실패가 배치를 멈추지 않게 상위에서 잡는다."""


def _validated_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise CrawlError(f"허용되지 않은 URL: {value}")
    return value


def _get(url: str, *, referer: str | None = None) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    if referer:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise CrawlError(f"HTTP {exc.code} {url}") from exc
    except OSError as exc:
        raise CrawlError(f"요청 실패 {type(exc).__name__} {url}") from exc


def html_to_text(html: str) -> str:
    """태그를 지우고 사람이 읽는 순서를 유지한 텍스트로 바꾼다."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    # 블록 경계를 줄바꿈으로 남겨야 기준 항목이 한 줄로 뭉치지 않는다.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|td|th)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def redact(text: str) -> tuple[str, int]:
    """공고에 실린 연락처를 지운다. S3 에 들어가기 전에 처리한다."""
    count = 0

    def _email(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[이메일 삭제]"

    def _phone(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[전화번호 삭제]"

    text = EMAIL_PATTERN.sub(_email, text)
    text = PHONE_PATTERN.sub(_phone, text)
    return text, count


def discover_detail_urls(list_url: str, *, limit: int) -> list[str]:
    """공개 목록에서 상세 공고 URL 을 찾는다.

    목록은 일반 링크 대신 `javascript:goView('202600603')` 를 쓴다. 사이트
    JavaScript 를 실행하지 않고 공개 상세 URL 로만 변환한다.
    """
    if limit < 1 or limit > 50:
        raise CrawlError("한 번에 수집할 공고 수는 1~50 이어야 한다")
    html = _get(_validated_url(list_url))
    base = f"{urllib.parse.urlparse(list_url).scheme}://{urllib.parse.urlparse(list_url).hostname}"
    found = re.findall(r"goView\(['\"]([0-9]+)['\"]\)", html)
    urls: list[str] = []
    for serial in dict.fromkeys(found):
        urls.append(f"{base}/clnctest/view.do?clncTestSn={serial}")
        if len(urls) >= limit:
            break
    return urls


def _fetch_tabs(detail_url: str, serial: str) -> list[str]:
    """탭 본문을 AJAX 엔드포인트에서 직접 받는다."""
    base = urllib.parse.urlparse(detail_url)
    sections: list[str] = []
    for tab_number in CONTENT_TAB_NUMBERS:
        tab_url = (
            f"{base.scheme}://{base.hostname}/clnctest/sub/tab.do"
            f"?clncTestSn={urllib.parse.quote(serial)}&tabNum={tab_number}"
        )
        try:
            sections.append(_get(tab_url, referer=detail_url))
        except CrawlError as exc:
            # 탭 하나가 없어도 나머지는 쓸 수 있다.
            logger.warning("탭 %s 수집 실패: %s", tab_number, exc)
    return sections


def capture(url: str, *, bucket: str) -> dict:
    """공고 한 건을 수집해 S3 에 올린다. 로컬 파일은 만들지 않는다."""
    url = _validated_url(url)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    serial = (query.get("clncTestSn") or query.get("ctSeq") or [""])[0]
    if not serial:
        raise CrawlError(f"공고 번호를 찾을 수 없다: {url}")

    page_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    captured_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = "kct"

    detail_html = _get(url)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", detail_html, flags=re.S | re.I)
    title = html_to_text(title_match.group(1)) if title_match else ""

    if any(
        marker in detail_html for marker in ("로그인이 필요", "로그인 후", "접근 권한")
    ):
        raise CrawlError(f"로그인/접근 제한 화면이다: {url}")

    tab_html = _fetch_tabs(url, serial)
    combined_html = "\n".join([detail_html, *tab_html])
    # 같은 문구가 상세와 탭에 겹쳐 들어오므로 순서를 지키며 중복 줄을 지운다.
    merged = "\n".join(
        dict.fromkeys(html_to_text(chunk) for chunk in [detail_html, *tab_html])
    )
    body_text, redaction_count = redact(merged)

    hangul = len(re.findall(r"[가-힣]", body_text))
    metadata = {
        "source": "KoreaClinicalTrials",
        "source_url": url,
        "serial": serial,
        "title": title[:200],
        "captured_at": captured_at,
        "body_character_count": len(body_text),
        "hangul_character_count": hangul,
        "tab_sections": len(tab_html),
        "redaction_count": redaction_count,
    }
    if len(body_text) < 500 or hangul < 100:
        raise CrawlError(
            f"본문이 비정상적으로 짧다 ({len(body_text)}자, 한글 {hangul}자): {url}"
        )

    object_metadata = {
        "source-url": urllib.parse.quote(url, safe=":/?&=%#"),
        "source-url-sha256": page_id,
        "source": prefix,
        "captured-at": captured_at,
    }
    s3 = boto3.client("s3")
    # 감사 원본은 우리가 실제로 파싱한 바이트다. PNG 대신 HTML 로 남기면
    # 검색·비교가 되고 Textract 한국어 미지원 문제와도 무관해진다.
    s3.put_object(
        Bucket=bucket,
        Key=f"raw/trials/html/{prefix}_{page_id}.html",
        Body=combined_html.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
        Metadata=object_metadata,
    )
    document_key = f"trials/documents/{prefix}_{page_id}.txt"
    s3.put_object(
        Bucket=bucket,
        Key=document_key,
        Body=body_text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
        Metadata=object_metadata,
    )
    metadata["document_key"] = document_key
    logger.info(
        "수집 완료 %s (%s자, 한글 %s자, 마스킹 %s건)",
        document_key,
        len(body_text),
        hangul,
        redaction_count,
    )
    return metadata


def handler(event, _context) -> dict:
    """EventBridge 스케줄 또는 수동 호출 진입점.

    이벤트로 `list_url`·`limit`·`urls` 를 덮어쓸 수 있다. `urls` 를 주면 목록
    탐색을 건너뛰고 그 공고만 수집한다.
    """
    event = event or {}
    if not BUCKET:
        raise RuntimeError("S3_BUCKET_NAME 이 설정되지 않았다")

    limit = int(event.get("limit") or LIMIT)
    delay = max(float(event.get("delay_seconds") or DELAY_SECONDS), 1.0)
    targets = event.get("urls") or discover_detail_urls(
        event.get("list_url") or LIST_URL, limit=limit
    )

    captured: list[dict] = []
    failed: list[dict] = []
    for index, url in enumerate(targets[:limit]):
        try:
            captured.append(capture(url, bucket=BUCKET))
        except Exception as exc:
            logger.warning("공고 수집 실패 %s: %s", url, exc)
            failed.append({"url": url, "error": str(exc)})
        # 공개 사이트 부하를 줄인다. 마지막 건 뒤에는 쉬지 않는다.
        if index + 1 < len(targets[:limit]):
            time.sleep(delay)

    result = {
        "list_url": event.get("list_url") or LIST_URL,
        "discovered": len(targets),
        "captured_count": len(captured),
        "failed_count": len(failed),
        "captured": captured,
        "failed": failed,
    }
    logger.info("수집 요약 %s", json.dumps(result, ensure_ascii=False)[:2000])
    return result
