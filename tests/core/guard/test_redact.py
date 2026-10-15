import pytest

from core.guard.redact import MASK, redact_mapping, redact_text


def test_password_assignment_is_masked():
    assert redact_text("login failed password=hunter2 user=bob") == (
        f"login failed password={MASK} user=bob"
    )


def test_json_key_value_is_masked():
    text = '{"api_key": "abc123", "region": "kr"}'
    assert redact_text(text) == f'{{"api_key": "{MASK}", "region": "kr"}}'


def test_bearer_header_is_masked():
    text = "Authorization: Bearer eyJhbGciOi.payload.sig"
    assert redact_text(text) == f"Authorization: Bearer {MASK}"


def test_url_userinfo_password_is_masked():
    text = "connect postgres://app:s3cr3t@db.dev:5432/app failed"
    assert redact_text(text) == f"connect postgres://app:{MASK}@db.dev:5432/app failed"


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=pw1",
        "client_secret: pw1",
        "accessToken=pw1",
        "x-api-key: pw1",
        "GITHUB_TOKEN='pw1'",
        "https://h/cb?token=pw1&x=1",
    ],
)
def test_sensitive_key_variants_are_masked(text):
    assert "pw1" not in redact_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "keyboard=us monkey=3 tokenizer=bpe",
        "max_tokens=100",
        "secretName: db-secret",
        "key=password",
        "Back-off restarting failed container api in pod api-7d9",
        'MountVolume.SetUp failed for volume "cfg" : secret "db-secret" not found',
        "see https://example.com:8443/path",
    ],
)
def test_normal_text_is_untouched(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2",
        '{"token": "abc"}',
        "Authorization: Bearer abc.def",
        "redis://u:p@r:6379",
    ],
)
def test_redact_text_is_idempotent(text):
    once = redact_text(text)
    assert redact_text(once) == once


def test_env_value_is_masked_when_name_is_sensitive():
    env = [
        {"name": "DB_PASSWORD", "value": "pw1"},
        {"name": "LOG_LEVEL", "value": "info"},
    ]
    assert redact_mapping(env) == [
        {"name": "DB_PASSWORD", "value": MASK},
        {"name": "LOG_LEVEL", "value": "info"},
    ]


def test_env_value_from_secret_ref_keeps_reference():
    env = [
        {
            "name": "DB_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": "db-secret", "key": "password"}},
        }
    ]
    assert redact_mapping(env) == env


def test_nested_dict_and_list_are_redacted():
    data = {
        "pod": "api-1",
        "config": {"apiKey": "pw1", "replicas": 2, "debug": True},
        "events": [{"message": "probe failed token=pw2"}, "url redis://u:pw3@r:6379"],
        "token": 12345,
    }
    assert redact_mapping(data) == {
        "pod": "api-1",
        "config": {"apiKey": MASK, "replicas": 2, "debug": True},
        "events": [{"message": f"probe failed token={MASK}"}, f"url redis://u:{MASK}@r:6379"],
        "token": MASK,
    }


def test_redact_mapping_does_not_mutate_input():
    data = {"password": "pw1"}
    redact_mapping(data)
    assert data == {"password": "pw1"}


def test_redact_mapping_is_idempotent():
    data = {"env": [{"name": "API_TOKEN", "value": "pw1"}], "log": "password=pw2"}
    once = redact_mapping(data)
    assert redact_mapping(once) == once
