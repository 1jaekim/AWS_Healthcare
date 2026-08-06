# A2A 평가 데이터

`trialgpt_criterion_sample.jsonl`은 NCBI의 공개 도메인
`ncbi/TrialGPT-Criterion-Annotations`에서 라벨별 최대 5건을 결정론적으로 추출한
평가 샘플이다. 환자 사례는 공개 합성 데이터이며 실제 서비스 사용자 데이터가 아니다.

재생성:

```bash
cd backend
python scripts/prepare_trialgpt_eval.py --per-label 5
```

배포된 A2A 평가:

```bash
python scripts/evaluate_trialgpt_a2a.py --region ap-northeast-2 --limit 12
```

전문의 라벨 매핑:

| TrialGPT 라벨 | A2A 기대값 |
|---|---|
| `included`, `not excluded` | `OK` |
| `not included`, `excluded` | `NOT_OK` |
| `not enough information`, `not applicable` | `UNKNOWN` |

출처: <https://huggingface.co/datasets/ncbi/TrialGPT-Criterion-Annotations>
