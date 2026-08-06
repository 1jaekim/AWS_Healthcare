from backend.scripts.import_mfds_dur import _unwrap_rows


def test_unwraps_mfds_item_envelope() -> None:
    rows = _unwrap_rows(
        [{"item": {"INGR_CODE": "D1"}}, {"item": {"INGR_CODE": "D2"}}],
        limit=10,
    )
    assert rows == [{"INGR_CODE": "D1"}, {"INGR_CODE": "D2"}]


def test_accepts_flat_rows_and_honors_limit() -> None:
    rows = _unwrap_rows(
        [{"INGR_CODE": "D1"}, {"INGR_CODE": "D2"}],
        limit=1,
    )
    assert rows == [{"INGR_CODE": "D1"}]
