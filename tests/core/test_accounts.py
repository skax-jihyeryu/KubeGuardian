import pytest

from core.accounts import load_accounts
from core.error.service_exception import ServiceException


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


def test_unreadable_file_raises_with_reason_but_without_values(tmp_path):
    _secret(tmp_path, "kg-test-user", id="tester")
    (tmp_path / "kg-test-user" / "password").write_bytes(b"\xff\xfe-secret-value")

    with pytest.raises(ServiceException) as e:
        load_accounts(tmp_path)

    assert "kg-test-user/password" in e.value.detail
    assert "UnicodeDecodeError" in e.value.detail
    assert "secret-value" not in e.value.detail and "tester" not in e.value.detail


def test_permission_error_is_reported_with_secret_and_key(tmp_path):
    _secret(tmp_path, "kg-test-user", id="tester")
    path = tmp_path / "kg-test-user" / "id"
    path.chmod(0)
    try:
        with pytest.raises(ServiceException) as e:
            load_accounts(tmp_path)
    finally:
        path.chmod(0o600)

    assert "kg-test-user/id" in e.value.detail and "PermissionError" in e.value.detail


def test_empty_secret_directory_is_not_an_account(tmp_path):
    """optional Secret이 없으면 kubelet이 빈 디렉터리를 마운트한다. 계정 없음이어야 한다."""
    _secret(tmp_path, "kg-test-user")

    assert load_accounts(tmp_path) == {}
