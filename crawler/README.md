# Medi25 Crawler

## Playwright 공개 공고 → AWS 저장

한국임상시험참여포털 또는 Medi25의 로그인 제한 없는 공개 상세 페이지를 PNG로
캡처하고, 이메일·전화번호를 DOM에서 마스킹한 뒤 S3에 업로드한다. 업로드된
`trials/screenshots/*.png`는 EventBridge → `healthcare-trials-pipeline` →
Textract → Bedrock → DynamoDB `CriteriaStore(pending_review)` 순서로 처리된다.

```bash
python -m pip install -r crawler/requirements-playwright.txt
python -m playwright install chromium
python crawler/playwright_capture.py \
  --url "https://www.koreaclinicaltrials.org/clnctrial/clncView.do?ctSeq=281" \
  --s3-bucket "<HealthcareDataBucketName>"
```

로그인 또는 접근 제한 화면은 S3에 업로드하지 않는다. `pending_review` 데이터는
연구자가 원문과 구조화 결과를 검토하기 전에는 승인 데이터로 사용하지 않는다.

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
crawler/

├── README.md

├── medi25-crawler-agent.md
│      Agent Specification

└── medi25-crawler-skill.md
       HTML Crawling & Metadata Extraction
```

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
  "trial_title":"당뇨 동반 고혈압 있으신 분",
  "disease":"당뇨/고혈압",
  "sponsor":"보령",
  "hospital":"전국",
  "location":"전국",
  "age":"만19세 이상",
  "sex":"남성/여성",
  "visit_schedule":"총 8회 방문",
  "recruitment_status":"Recruiting",
  "detail_url":"..."
}
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

✅ Medi25 Search

✅ HTML Parsing

✅ Recruiting Announcement Detection

✅ Search Result Filtering

✅ Detail Page Crawling

✅ Metadata Extraction

✅ JSON Standardization

✅ Output Validation

---

## Excluded

⬜ ClinicalTrials.gov

⬜ MFDS Clinical Trial Portal

⬜ PDF Parsing

⬜ Eligibility Evaluation

⬜ Trial Recommendation

⬜ Knowledge Base

⬜ AWS Infrastructure

---

# Notes

This module is dedicated exclusively to the Medi25 platform.

Business logic, patient eligibility evaluation, semantic ranking, and clinical trial recommendation are handled by downstream components of the Clinical Trial Matching AI system.
