from core.accounts import load_accounts


def _secret(root, name, **keys):
    d = root / name
    d.mkdir()
    for key, value in keys.items():
        (d / key).write_text(value, encoding="utf-8")


def test_loads_secret_directories_as_key_value_maps(tmp_path):
    _secret(tmp_path, "kg-test-user", id="tester\n", password="pw1")

    assert load_accounts(tmp_path) == {"kg-test-user": {"id": "tester", "password": "pw1"}}


def test_strips_only_one_trailing_newline(tmp_path):
    _secret(tmp_path, "s", a="x\n\n", b="y")

    assert load_accounts(tmp_path)["s"] == {"a": "x\n", "b": "y"}


def test_ignores_kubernetes_mount_metadata(tmp_path):
    _secret(tmp_path, "kg-test-user", id="tester")
    # 볼륨 마운트가 만드는 ..data 디렉터리와 타임스탬프 디렉터리, 그 안의 ..로 시작하는 파일
    _secret(tmp_path, "..data", id="hidden")
    _secret(tmp_path, "..2026_10_22", id="hidden")
    (tmp_path / "kg-test-user" / "..data").write_text("meta")

    assert load_accounts(tmp_path) == {"kg-test-user": {"id": "tester"}}


def test_missing_directory_means_no_accounts(tmp_path):
    assert load_accounts(tmp_path / "none") == {}
