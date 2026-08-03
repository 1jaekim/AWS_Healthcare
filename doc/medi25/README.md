# Medi25 Crawler

Medi25 기반 임상시험 모집공고를 자동으로 수집하기 위한 Crawler 모듈입니다.

사용자가 입력한 질환 키워드를 기반으로 Medi25를 검색하고, 모집 중인 임상시험 공고만 선별하여 표준화된 메타데이터(JSON) 형태로 반환합니다.

본 모듈은 Clinical Trial Matching AI 프로젝트의 데이터 수집 계층(Data Collection Layer)을 담당하며, 이후 Trial Matching 및 Eligibility Evaluation을 위한 입력 데이터를 제공합니다.

---

# Architecture Overview

```
Disease Keyword
        │
        ▼
Medi25 Search
        │
        ▼
Search Result Parsing
        │
        ▼
Result Classification
        │
        ├──────────────┐
        │              │
        ▼              ▼
Recruiting        Non-Recruiting
Announcement      Pages
        │              │
        ▼              ▼
Detail Page       Ignore
Crawling
        │
        ▼
Metadata Extraction
        │
        ▼
Validation
        │
        ▼
Standardized JSON
```

---

# Project Structure

```
medi25/

├── README.md

├── medi25-crawler-agent.md
│      Agent Specification

├── medi25-crawler-skill.md
│      HTML Crawling & Metadata Extraction

├── crawler.py
│      실행 가능한 크롤러 구현체

├── requirements.txt
│      의존성 목록

└── output/
       크롤링 결과 JSON 저장 위치
```

---

# Quick Start

```bash
pip install -r requirements.txt
python crawler.py
```

실행 후 Chrome 창이 열리면 네이버로 로그인하고, 콘솔로 돌아와 Enter를 누릅니다.
결과는 `output/` 폴더에 저장됩니다.

검색 키워드를 바꾸려면 `crawler.py` 상단의 `SEARCH_KEYWORD` 값을 수정하세요.

---

# Components

## 1. Medi25 Crawler Agent

The Medi25 Crawler Agent orchestrates the crawling workflow.

Responsibilities

- Receive disease keyword
- Search Medi25
- Classify search results
- Execute Crawling Skill
- Validate extracted metadata
- Return standardized JSON

Related Document

```
medi25-crawler-agent.md
```

---

## 2. Medi25 Crawling Skill

The Medi25 Crawling Skill performs HTML crawling and metadata extraction.

Responsibilities

- Parse HTML pages
- Filter non-recruitment pages
- Visit recruiting announcement pages
- Extract metadata
- Validate output

Related Document

```
medi25-crawler-skill.md
```

---

# Filtering Logic

The crawler processes only recruiting announcements.

```
Reservation
        │
        ▼
Ignore

Closed Announcement
        │
        ▼
Ignore

AI Matching
        │
        ▼
Ignore

Healthcare Service
        │
        ▼
Ignore

Recruiting Announcement
        │
        ▼
Collect Metadata
```

Based on platform analysis, only Recruiting Announcement pages are forwarded to downstream processing.

## 구현된 판별 기준

실제 Medi25 HTML에서 상태를 구분하는 마커는 다음과 같습니다.

| 마커 | 의미 | 처리 |
|------|------|------|
| `div.item_wrap.end` 또는 `div.info.deadline` | 마감 | Ignore |
| `div.info.reservation` | 예약 접수 | Ignore |
| 위 둘 모두 없음 | 모집중 | Collect |

---

# Extracted Metadata

The crawler extracts the following information from each recruiting announcement.

| Field | Description |
|--------|-------------|
| Trial Title | Clinical trial title |
| Disease | Target disease |
| Sponsor | Sponsor organization |
| Hospital | Participating hospital |
| Location | Recruitment region |
| Age | Eligible age |
| Sex | Eligible sex |
| Visit Schedule | Visit information |
| Recruitment Status | Recruiting status |
| Detail URL | Original announcement URL |

## HTML 추출 위치

| HTML 요소 | 필드 |
|-----------|------|
| `div.name` | trial_title |
| `div.info.disease` | disease |
| `li.place` | sponsor |
| `li.place2` | location |
| `div.info.age` | age |
| `div[id^=announcement-]` | detail_url (ID 추출 후 조립) |

상세 URL 조립 규칙

```
announcement-10713
        ↓
/html/odition_1/odition_detail_read_step1_m.php?seq=10713
```

---

# Example

## Input

```json
{
    "keyword":"당뇨"
}
```

## Output

```json
{
  "source":"Medi25",
  "trial_title":"당뇨 동반 고혈압 있으신 분 BR-FDC-CT-301",
  "disease":"당뇨/고혈압",
  "sponsor":"보령",
  "hospital":"",
  "location":"전국",
  "age":"만19세~",
  "sex":"남성/여성",
  "visit_schedule":"총 8회방문",
  "recruitment_status":"Recruiting",
  "detail_url":"https://www.medi25.com/html/odition_1/odition_detail_read_step1_m.php?seq=10713"
}
```

## 실측 결과

`당뇨` 키워드 실행 시 관측된 분류 결과입니다.

```
전체 56건

├ 모집중  2건  → 수집
├ 예약   13건  → Ignore
└ 마감   35건  → Ignore
```

---

# Documents

| Document | Description |
|----------|-------------|
| medi25-crawler-agent.md | Agent specification |
| medi25-crawler-skill.md | Crawling skill specification |

---

# Scope

## Included

- Medi25 Search
- HTML Parsing
- Recruiting Announcement Detection
- Search Result Filtering
- Detail Page Crawling
- Metadata Extraction
- JSON Standardization
- Output Validation

---

## Excluded

- ClinicalTrials.gov
- MFDS Clinical Trial Portal
- PDF Parsing
- Eligibility Evaluation
- Trial Recommendation
- Knowledge Base
- AWS Infrastructure

---

# Notes

This module is dedicated exclusively to the Medi25 platform.

Business logic, patient eligibility evaluation, semantic ranking, and clinical trial recommendation are handled by downstream components of the Clinical Trial Matching AI system.

## 구현상 제약

- 네이버 소셜 로그인은 자동화하지 않습니다. 네이버의 봇 탐지로 인해 자동 로그인 시
  캡차 또는 차단이 발생하므로, 로그인은 수동으로 처리한 뒤 세션을 이어받습니다.
- Medi25는 Vue 기반 SPA입니다. 사이트 개편 시 셀렉터가 무효화될 수 있습니다.
- `hospital` 필드는 정확도가 낮습니다. Medi25가 의뢰사와 실시기관을 동일한
  요소(`li.place`)에 혼재시켜 노출하기 때문입니다.
- 크롤링 결과에는 개인정보가 포함될 수 있어 `.gitignore`로 `output/`을 제외합니다.
