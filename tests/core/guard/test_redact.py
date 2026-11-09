import time

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
        "http://token:pw1@host:80",
        "http://token-service:80/?password=pw1",
        "user@token:pw1",  # URL이 아닌 `@` 뒤의 이름=값은 그대로 가린다
        "x@password:pw1",
        "user@secret=pw1",
        "login admin@token: pw1",
        "scheme://password=pw1",
        "http://token:pw1",  # 포트 자리가 숫자가 아니면 값으로 본다
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
        # 호스트 이름에 민감 단어가 있어도 URL의 host:port는 이름=값이 아니다 (S04 오탐 방지)
        "http://token-service:8080/api",
        "http://u@secret-store.dev.svc:8200",
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


# --- 리뷰 지적 회귀 테스트 ---


@pytest.mark.parametrize(
    "text",
    ["a" * 200_000, "a." * 100_000, 'token="' + "x" * 200_000, "redis://" + "a." * 100_000],
)
def test_long_line_finishes_quickly(text):
    start = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "text",
    [
        "    secret:\n      defaultMode: 420\n      secretName: db-secret",
        "- serviceAccountToken:\n    expirationSeconds: 3607",
        "password=\n user=bob",
        "DB_PASSWORD:  <set to the key 'password' in secret 'db-secret'>  Optional: false",
        'Bearer realm="api" error',
        "public_key=ssh-ed25519 token_ttl=3600 key_id=abc secret_type=Opaque",
        "pod/sk-hynix-batch-worker-7d9f8c-abcde   0/1   CrashLoopBackOff",
        "deployment.apps/sk-telecom-chat-gateway-prod scaled",
        "namespace sk-ax-dev-environment-01",
        'service "sk-agent-orchestrator-svc" not found',
    ],
)
def test_yaml_and_describe_output_are_untouched(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("redis://:onlypass@redis:6379", f"redis://:{MASK}@redis:6379"),
        ('{\\"password\\":\\"pw1\\"}', f'{{\\"password\\":\\"{MASK}\\"}}'),
        ('{"password":"a\\"b"}', f'{{"password":"{MASK}"}}'),
        ("password='abc", f"password='{MASK}"),
        ('"password" => "pw1"', f'"password" => "{MASK}"'),
        ("Authorization: dXNlcjpwYXNz", f"Authorization: {MASK}"),
        ("Authorization: ApiKey abc", f"Authorization: ApiKey {MASK}"),
        ("run --db-password pw1 --port 80", f"run --db-password {MASK} --port 80"),
        ("PGPASSWORD=pw1 DB_PASS=pw1 MYSQL_PWD=pw1 passphrase=pw1", None),
        ("slack xoxb-1234567890-abcdefghij sk-abcdefghijklmnopqrstuv", None),
    ],
)
def test_review_leak_cases_are_masked(text, expected):
    result = redact_text(text)
    if expected is None:
        assert "pw1" not in result and "abcdefghij" not in result
    else:
        assert result == expected
    assert redact_text(result) == result


def test_pem_private_key_body_is_masked():
    text = "private_key: -----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----"
    result = redact_text(text)
    assert "MIIEabc" not in result
    assert result.startswith("private_key: -----BEGIN RSA PRIVATE KEY-----")
    assert redact_text(result) == result


_B64 = "MIIEpAIBAAKCAQEAzzQ9abcdefgh"  # 16자 이상 base64 줄


def test_pem_cut_in_the_middle_masks_trailing_body_and_end_line():
    text = f"start\n{_B64}\n{_B64}x\n-----END RSA PRIVATE KEY-----\nafter"
    result = redact_text(text)
    assert _B64 not in result
    assert "END RSA PRIVATE KEY" not in result
    assert result.splitlines()[0] == "start" and result.splitlines()[-1] == "after"
    assert redact_text(result) == result


def test_pem_cut_keeps_ordinary_lines_around_it():
    text = "info: ready\nthis is an ordinary line\n-----END PRIVATE KEY-----\nnext line"
    result = redact_text(text)
    assert "info: ready" in result and "this is an ordinary line" in result
    assert result.endswith("next line")
    assert "END PRIVATE KEY" not in result


def test_pem_cut_with_short_last_line_masks_whole_body():
    b = "B" * 64
    result = redact_text(f"{b}\n{b}\nabc=\n-----END PRIVATE KEY-----")
    assert "BBBB" not in result and "abc=" not in result
    assert result == MASK


def test_pem_cut_with_short_last_line_keeps_ordinary_lines_above():
    b = "B" * 64
    result = redact_text(f"server started\nlistening\n{b}\nabc=\n-----END PRIVATE KEY-----\nnext")
    assert result == f"server started\nlistening\n{MASK}\nnext"


def test_complete_pem_is_unchanged_by_the_cut_rule():
    text = f"a\n-----BEGIN RSA PRIVATE KEY-----\n{_B64}\n-----END RSA PRIVATE KEY-----\nb"
    result = redact_text(text)
    assert result == "a\n-----BEGIN RSA PRIVATE KEY----------END RSA PRIVATE KEY-----\nb".replace(
        "----------", f"-----{MASK}-----"
    )
    assert redact_text(result) == result


def test_begin_without_end_still_masks_everything_after():
    result = redact_text(f"a\n-----BEGIN RSA PRIVATE KEY-----\n{_B64}\nlater line")
    assert result == f"a\n-----BEGIN RSA PRIVATE KEY-----{MASK}"


def test_list_args_and_list_values_are_masked():
    data = {"args": ["--db-password", "pw1", "--port", "80"], "password": ["pw2"]}
    assert redact_mapping(data) == {
        "args": ["--db-password", MASK, "--port", "80"],
        "password": [MASK],
    }


@pytest.mark.parametrize(
    "name",
    ["Authorization", "authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-Api-Key"],
)
def test_auth_and_cookie_header_mapping_keys_are_masked(name):
    assert redact_mapping({name: "Basic abc"}) == {name: MASK}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Cookie: sid=abc123", "Cookie: ***"),
        ("Set-Cookie: session=zzz; Path=/", "Set-Cookie: ***"),
        ("cookie: a=1; b=secretvalue", "cookie: ***"),
        ("Proxy-Authorization: Basic abc", "Proxy-Authorization: Basic ***"),
        ("Authorization: Basic abc", "Authorization: Basic ***"),
        ("X-Api-Key: abc123", "X-Api-Key: ***"),
        ('{"Cookie": "sid=abc123; b=2"}', '{"Cookie": "***"}'),
        ("GET / HTTP/1.1\nCookie: sid=abc123\nHost: x", "GET / HTTP/1.1\nCookie: ***\nHost: x"),
    ],
)
def test_auth_and_cookie_headers_in_text_are_masked(text, expected):
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "cookie_name: sessionid",
        "cookieConsent: true",
        "my_cookie: x",
        "authorization-mode: Node,RBAC",
        "Cookie jar is empty",
    ],
)
def test_cookie_like_words_are_untouched(text):
    assert redact_text(text) == text
    assert redact_mapping(
        {"cookie_name": "sid", "cookieConsent": "true", "authorization-mode": "RBAC"}
    ) == {
        "cookie_name": "sid",
        "cookieConsent": "true",
        "authorization-mode": "RBAC",
    }
