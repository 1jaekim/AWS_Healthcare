"""
Graph ETL Lambda
─────────────────────────
timeline / measurement / medication CSV 데이터를 Neptune용 Node·Edge CSV로 변환

입력: S3 raw/ 경로의 CSV 파일들
  - timeline.csv: 환자 타임라인 이벤트
  - measurement.csv: 검사 수치
  - medication.csv: 투약 이력

출력: S3 graph/ 경로의 Neptune Bulk Loader 형식 CSV
  - nodes/patients.csv
  - nodes/events.csv
  - nodes/measurements.csv
  - nodes/medications.csv
  - edges/patient_event.csv
  - edges/patient_measurement.csv
  - edges/patient_medication.csv
  - edges/event_measurement.csv
"""
import csv
import io
import json
import logging
import os
import uuid
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")

# ─── 환경변수 ────────────────────────────────────────────
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "aws-healthcare-data")
RAW_PREFIX = os.environ.get("S3_PREFIX_RAW", "raw/")
GRAPH_PREFIX = os.environ.get("S3_PREFIX_GRAPH", "graph/")

# ─── Neptune CSV 헤더 정의 ───────────────────────────────
# Neptune Bulk Loader 형식: ~id, ~label, property1, property2, ...
NODE_HEADERS = {
    "patients": ["~id", "~label", "patient_id:String"],
    "events": ["~id", "~label", "event_type:String", "event_date:String", "description:String"],
    "measurements": ["~id", "~label", "measurement_type:String", "value:Double", "unit:String", "date:String"],
    "medications": ["~id", "~label", "medication_name:String", "dosage:String", "start_date:String", "end_date:String", "status:String"],
}

# Neptune Edge 형식: ~id, ~from, ~to, ~label, property1, ...
EDGE_HEADERS = {
    "patient_event": ["~id", "~from", "~to", "~label"],
    "patient_measurement": ["~id", "~from", "~to", "~label", "date:String"],
    "patient_medication": ["~id", "~from", "~to", "~label"],
    "event_measurement": ["~id", "~from", "~to", "~label"],
}


def read_csv_from_s3(key: str) -> list[dict]:
    """S3에서 CSV 파일을 읽어 dict 리스트로 반환"""
    try:
        response = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        content = response["Body"].read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)
    except s3_client.exceptions.NoSuchKey:
        logger.warning(f"파일 없음: s3://{BUCKET_NAME}/{key}")
        return []
    except Exception as e:
        logger.error(f"CSV 읽기 실패 ({key}): {e}")
        raise


def write_csv_to_s3(key: str, headers: list[str], rows: list[list]) -> int:
    """Neptune CSV 형식으로 S3에 저장"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)

    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=output.getvalue().encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="aws:kms",
    )
    logger.info(f"저장: s3://{BUCKET_NAME}/{key} ({len(rows)} rows)")
    return len(rows)


def generate_id(prefix: str, *parts) -> str:
    """결정적 ID 생성 (동일 입력 → 동일 ID)"""
    key = f"{prefix}:{'|'.join(str(p) for p in parts)}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))


def process_timeline(records: list[dict]) -> tuple[list[list], list[list], set]:
    """타임라인 CSV → event 노드 + patient-event 엣지"""
    event_nodes = []
    patient_event_edges = []
    patient_ids = set()

    for row in records:
        patient_id = row.get("patient_id", "").strip()
        if not patient_id:
            continue

        patient_ids.add(patient_id)
        event_id = generate_id("event", patient_id, row.get("date", ""), row.get("event_type", ""))
        patient_node_id = generate_id("patient", patient_id)

        event_nodes.append([
            event_id,
            "Event",
            row.get("event_type", "unknown"),
            row.get("date", ""),
            row.get("description", ""),
        ])

        patient_event_edges.append([
            generate_id("edge_pe", patient_id, event_id),
            patient_node_id,
            event_id,
            "HAS_EVENT",
        ])

    return event_nodes, patient_event_edges, patient_ids


def process_measurements(records: list[dict]) -> tuple[list[list], list[list], list[list], set]:
    """measurement CSV → measurement 노드 + patient-measurement 엣지 + event-measurement 엣지"""
    measurement_nodes = []
    patient_measurement_edges = []
    event_measurement_edges = []
    patient_ids = set()

    for row in records:
        patient_id = row.get("patient_id", "").strip()
        if not patient_id:
            continue

        patient_ids.add(patient_id)
        date = row.get("date", "")
        meas_type = row.get("measurement_type", row.get("type", "unknown"))

        measurement_id = generate_id("measurement", patient_id, date, meas_type)
        patient_node_id = generate_id("patient", patient_id)

        # value를 숫자로 변환 시도
        try:
            value = float(row.get("value", 0))
        except (ValueError, TypeError):
            value = 0.0

        measurement_nodes.append([
            measurement_id,
            "Measurement",
            meas_type,
            value,
            row.get("unit", ""),
            date,
        ])

        patient_measurement_edges.append([
            generate_id("edge_pm", patient_id, measurement_id),
            patient_node_id,
            measurement_id,
            "HAS_MEASUREMENT",
            date,
        ])

        # 같은 날짜의 event와 연결
        event_id = generate_id("event", patient_id, date, "visit")
        event_measurement_edges.append([
            generate_id("edge_em", event_id, measurement_id),
            event_id,
            measurement_id,
            "RECORDED_DURING",
        ])

    return measurement_nodes, patient_measurement_edges, event_measurement_edges, patient_ids


def process_medications(records: list[dict]) -> tuple[list[list], list[list], set]:
    """medication CSV → medication 노드 + patient-medication 엣지"""
    medication_nodes = []
    patient_medication_edges = []
    patient_ids = set()

    for row in records:
        patient_id = row.get("patient_id", "").strip()
        if not patient_id:
            continue

        patient_ids.add(patient_id)
        med_name = row.get("medication_name", row.get("name", "unknown"))
        medication_id = generate_id("medication", patient_id, med_name, row.get("start_date", ""))
        patient_node_id = generate_id("patient", patient_id)

        medication_nodes.append([
            medication_id,
            "Medication",
            med_name,
            row.get("dosage", ""),
            row.get("start_date", ""),
            row.get("end_date", ""),
            row.get("status", "active"),
        ])

        patient_medication_edges.append([
            generate_id("edge_pmed", patient_id, medication_id),
            patient_node_id,
            medication_id,
            "TAKES_MEDICATION",
        ])

    return medication_nodes, patient_medication_edges, patient_ids


def build_patient_nodes(patient_ids: set) -> list[list]:
    """수집된 patient_id로 Patient 노드 생성"""
    return [
        [generate_id("patient", pid), "Patient", pid]
        for pid in sorted(patient_ids)
    ]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda 핸들러
    event 형식:
        {
            "timeline_key": "raw/timeline.csv",       (optional)
            "measurement_key": "raw/measurement.csv", (optional)
            "medication_key": "raw/medication.csv"    (optional)
        }
    """
    logger.info("Graph ETL Lambda 시작")

    # 소스 파일 경로 결정
    timeline_key = event.get("timeline_key", f"{RAW_PREFIX}timeline.csv")
    measurement_key = event.get("measurement_key", f"{RAW_PREFIX}measurement.csv")
    medication_key = event.get("medication_key", f"{RAW_PREFIX}medication.csv")

    # CSV 데이터 읽기
    timeline_data = read_csv_from_s3(timeline_key)
    measurement_data = read_csv_from_s3(measurement_key)
    medication_data = read_csv_from_s3(medication_key)

    logger.info(
        f"입력 데이터: timeline={len(timeline_data)}, "
        f"measurement={len(measurement_data)}, "
        f"medication={len(medication_data)}"
    )

    # 데이터 변환
    all_patient_ids = set()

    event_nodes, patient_event_edges, pids = process_timeline(timeline_data)
    all_patient_ids.update(pids)

    measurement_nodes, patient_measurement_edges, event_measurement_edges, pids = process_measurements(measurement_data)
    all_patient_ids.update(pids)

    medication_nodes, patient_medication_edges, pids = process_medications(medication_data)
    all_patient_ids.update(pids)

    patient_nodes = build_patient_nodes(all_patient_ids)

    # Neptune CSV로 S3에 저장
    stats = {}

    # 노드 저장
    stats["patients"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}nodes/patients.csv", NODE_HEADERS["patients"], patient_nodes
    )
    stats["events"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}nodes/events.csv", NODE_HEADERS["events"], event_nodes
    )
    stats["measurements"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}nodes/measurements.csv", NODE_HEADERS["measurements"], measurement_nodes
    )
    stats["medications"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}nodes/medications.csv", NODE_HEADERS["medications"], medication_nodes
    )

    # 엣지 저장
    stats["patient_event_edges"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}edges/patient_event.csv", EDGE_HEADERS["patient_event"], patient_event_edges
    )
    stats["patient_measurement_edges"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}edges/patient_measurement.csv", EDGE_HEADERS["patient_measurement"], patient_measurement_edges
    )
    stats["patient_medication_edges"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}edges/patient_medication.csv", EDGE_HEADERS["patient_medication"], patient_medication_edges
    )
    stats["event_measurement_edges"] = write_csv_to_s3(
        f"{GRAPH_PREFIX}edges/event_measurement.csv", EDGE_HEADERS["event_measurement"], event_measurement_edges
    )

    result = {
        "status": "success",
        "total_patients": len(all_patient_ids),
        "output_prefix": f"s3://{BUCKET_NAME}/{GRAPH_PREFIX}",
        "stats": stats,
    }

    logger.info(f"Graph ETL 완료: {json.dumps(result)}")
    return result
