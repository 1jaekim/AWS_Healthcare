"""
Medi25 임상시험 모집공고 크롤러 v3
===================================
모집공고 → 당뇨 검색 → 모집중 공고만 필터링 → 메타데이터 JSON 추출

사용법: python crawler.py
"""

import json
import time
import re
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
)
from webdriver_manager.chrome import ChromeDriverManager


# ============================================================
# 설정
# ============================================================
BASE_URL = "https://www.medi25.com"
SEARCH_KEYWORD = "당뇨"
OUTPUT_DIR = Path(__file__).parent / "output"
REQUEST_DELAY = 3
PAGE_LOAD_TIMEOUT = 20


def create_driver():
    """Chrome WebDriver 생성"""
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(5)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def wait_for_login(driver):
    """수동 로그인 대기"""
    driver.get(BASE_URL)
    time.sleep(2)
    print("\n" + "=" * 60)
    print("  1. 브라우저에서 네이버로 로그인하세요.")
    print("  2. 로그인 완료되면 여기서 Enter 누르세요.")
    print("=" * 60)
    input("\n>>> Enter를 누르세요... ")
    print("[*] 로그인 확인 완료")
    time.sleep(1)


def go_to_recruit_and_search(driver, keyword):
    """모집공고 페이지로 이동 + 키워드 검색"""
    # 모집공고 클릭
    print("[*] 모집공고 페이지 이동 중...")
    try:
        link = driver.find_element(
            By.CSS_SELECTOR, "a[href*='odition_list_new1_m']"
        )
        link.click()
        time.sleep(3)
        print("[+] 모집공고 페이지 이동 성공")
    except NoSuchElementException:
        # 직접 URL로 이동
        driver.get(f"{BASE_URL}/html/odition_1/odition_list_new1_m.php")
        time.sleep(3)
        print("[+] 모집공고 페이지 이동 (URL 직접)")

    # 검색어 입력
    print(f"[*] '{keyword}' 검색 중...")
    time.sleep(2)

    try:
        # medi_al 내부의 검색 input (공고 리스트 페이지 검색칸)
        search_input = WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".medi_al .searchbox input, .medi_al .sd-input input")
            )
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", search_input)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", search_input)
        time.sleep(0.5)
        search_input.send_keys(Keys.CONTROL + "a")
        search_input.send_keys(keyword)
        time.sleep(0.5)
        search_input.send_keys(Keys.ENTER)
        time.sleep(4)
        print(f"[+] '{keyword}' 검색 완료")
    except (TimeoutException, ElementNotInteractableException) as e:
        # URL 파라미터로 검색
        print(f"[!] 검색칸 입력 실패, URL 파라미터로 검색...")
        driver.get(f"{BASE_URL}/html/odition_1/odition_list_new1_m.php?keyword={keyword}")
        time.sleep(4)
        print(f"[+] URL 검색 완료")


def extract_recruiting_cards(driver):
    """
    검색 결과에서 모집중인 공고만 추출
    HTML 구조: ul.announcement_list > li > div.item > div.item_wrap
    - 마감: div.item_wrap.end 또는 div.info.deadline 존재
    - 예약: div.info.reservation 존재
    - 모집중: 둘 다 없는 경우
    """
    results = []

    # Vue가 렌더링될 때까지 대기
    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ul.announcement_list li"))
        )
    except TimeoutException:
        print("[!] 공고 리스트를 찾을 수 없습니다.")
        return results

    cards = driver.find_elements(By.CSS_SELECTOR, "ul.announcement_list > li")
    print(f"[*] 전체 카드 수: {len(cards)}")

    for card in cards:
        try:
            item_wrap = card.find_element(By.CSS_SELECTOR, "div.item_wrap")

            # 마감 여부 확인
            is_closed = "end" in (item_wrap.get_attribute("class") or "")
            if not is_closed:
                try:
                    card.find_element(By.CSS_SELECTOR, "div.info.deadline")
                    is_closed = True
                except NoSuchElementException:
                    pass

            if is_closed:
                continue

            # 예약 여부 확인
            try:
                card.find_element(By.CSS_SELECTOR, "div.info.reservation")
                continue  # 예약 공고는 스킵
            except NoSuchElementException:
                pass

            # 여기까지 왔으면 모집중!
            # 메타데이터 추출
            title = ""
            disease = ""
            sponsor = ""
            location = ""
            age = ""
            announcement_id = ""

            try:
                title = card.find_element(By.CSS_SELECTOR, "div.name").text.strip()
            except NoSuchElementException:
                pass

            try:
                disease = card.find_element(By.CSS_SELECTOR, "div.info.disease").text.strip()
            except NoSuchElementException:
                pass

            try:
                sponsor = card.find_element(By.CSS_SELECTOR, "li.place").text.strip()
            except NoSuchElementException:
                pass

            try:
                location = card.find_element(By.CSS_SELECTOR, "li.place2").text.strip()
            except NoSuchElementException:
                pass

            try:
                age = card.find_element(By.CSS_SELECTOR, "div.info.age").text.strip()
            except NoSuchElementException:
                pass

            # 공고 ID 추출 (div[id^="announcement-"])
            try:
                ann_div = card.find_element(By.CSS_SELECTOR, "div[id^='announcement-']")
                ann_id_raw = ann_div.get_attribute("id")  # "announcement-12204"
                announcement_id = ann_id_raw.replace("announcement-", "")
            except NoSuchElementException:
                pass

            detail_url = ""
            if announcement_id:
                detail_url = f"{BASE_URL}/html/odition_1/odition_detail_read_step1_m.php?seq={announcement_id}"

            results.append({
                "source": "Medi25",
                "trial_title": title,
                "disease": disease,
                "sponsor": sponsor,
                "hospital": "",
                "location": location,
                "age": age,
                "sex": "",
                "visit_schedule": "",
                "recruitment_status": "Recruiting",
                "detail_url": detail_url,
                "_announcement_id": announcement_id,
            })

        except Exception as e:
            continue

    return results


def extract_detail_metadata(driver, url):
    """상세 페이지에서 추가 메타데이터 추출"""
    try:
        driver.get(url)
        time.sleep(REQUEST_DELAY)

        body_text = driver.find_element(By.TAG_NAME, "body").text
        extra = {}

        # 성별
        if "남성" in body_text and "여성" in body_text:
            extra["sex"] = "남성/여성"
        elif "남성" in body_text:
            extra["sex"] = "남성"
        elif "여성" in body_text:
            extra["sex"] = "여성"

        # 방문 일정
        visit_match = re.search(r'(총\s*\d+\s*회\s*방문|방문\s*\d+\s*회|\d+박\s*\d+일)', body_text)
        if visit_match:
            extra["visit_schedule"] = visit_match.group(0)

        # 병원
        hospital_match = re.search(r'(실시기관|참여병원|병원)[:\s]*([^\n]+)', body_text)
        if hospital_match:
            extra["hospital"] = hospital_match.group(2).strip()[:50]

        return extra
    except Exception as e:
        print(f"    [!] 상세페이지 오류: {e}")
        return {}


def save_debug_snapshot(driver, filename="debug_page.html"):
    """
    디버깅용 HTML 저장 (개인정보 제거).

    medi25의 원본 page_source에는 로그인 사용자의 이름, 전화번호,
    회원 ID가 <script> 블록에 그대로 박혀 있다.
    그대로 저장하면 개인정보가 파일로 남으므로,
    공고 리스트 영역만 떼어내고 스크립트를 전부 제거한 뒤 저장한다.
    """
    try:
        # 공고 리스트 영역만 추출 (스크립트/헤더/푸터 제외)
        html = driver.execute_script("""
            const list = document.querySelector('ul.announcement_list')
                      || document.querySelector('.medi_al .contents')
                      || document.querySelector('.medi_al');
            return list ? list.outerHTML : '';
        """) or ""

        if not html:
            print("    [!] 공고 영역을 찾지 못해 스냅샷을 건너뜁니다.")
            return None

        # 혹시 남아있을 스크립트/스타일 제거
        html = re.sub(r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style\b.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # 개인정보 패턴 마스킹 (혹시 인라인 속성에 남은 경우 대비)
        html = re.sub(r"01[016-9]-?\d{3,4}-?\d{4}", "[전화번호 삭제]", html)
        html = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[이메일 삭제]", html)

        debug_path = OUTPUT_DIR / filename
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"    디버그 스냅샷 저장 (개인정보 제거됨): {debug_path}")
        return debug_path

    except Exception as e:
        print(f"    [!] 스냅샷 저장 실패: {e}")
        return None


def main():
    """메인"""
    print("=" * 60)
    print("  Medi25 임상시험 모집공고 크롤러 v3")
    print(f"  키워드: {SEARCH_KEYWORD}")
    print("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    driver = create_driver()

    try:
        # 1. 로그인
        wait_for_login(driver)

        # 2. 모집공고 + 검색
        go_to_recruit_and_search(driver, SEARCH_KEYWORD)

        # 3. 모집중 공고 추출
        print("\n[*] 모집중 공고 추출 중...")
        recruiting = extract_recruiting_cards(driver)

        if not recruiting:
            print("[!] 모집중인 공고를 찾지 못했습니다.")
            save_debug_snapshot(driver)
            return

        print(f"\n[+] 모집중 공고: {len(recruiting)}건 발견!")
        for i, r in enumerate(recruiting, 1):
            print(f"    {i}. {r['trial_title']} ({r['sponsor']})")

        # 4. 상세 페이지 크롤링 (선택사항)
        print(f"\n[*] 상세 페이지에서 추가 정보 수집 중...")
        for i, item in enumerate(recruiting, 1):
            if item["detail_url"]:
                print(f"  [{i}/{len(recruiting)}] {item['trial_title'][:40]}...")
                extra = extract_detail_metadata(driver, item["detail_url"])
                if extra.get("sex"):
                    item["sex"] = extra["sex"]
                if extra.get("visit_schedule"):
                    item["visit_schedule"] = extra["visit_schedule"]
                if extra.get("hospital"):
                    item["hospital"] = extra["hospital"]
            time.sleep(1)

        # _announcement_id 필드 제거 (내부용)
        for item in recruiting:
            item.pop("_announcement_id", None)

        # 5. 결과 저장
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = OUTPUT_DIR / f"medi25_{SEARCH_KEYWORD}_{timestamp}.json"

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(recruiting, f, ensure_ascii=False, indent=2)

        print(f"\n{'=' * 60}")
        print(f"  크롤링 완료!")
        print(f"  모집중 공고: {len(recruiting)}건")
        print(f"  저장 위치: {output_file}")
        print(f"{'=' * 60}")

        # 콘솔 출력
        print("\n--- 수집된 데이터 ---\n")
        for r in recruiting:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print()

    except KeyboardInterrupt:
        print("\n[*] 중단됨")
    finally:
        driver.quit()
        print("[*] 종료.")


if __name__ == "__main__":
    main()
