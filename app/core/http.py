"""서비스·앱 HTTP 클라이언트 (네임스페이스 제한, 상세설계 §3.1·§6, ADR-001).

- 생성할 때 대상 네임스페이스를 정하고, 호출 대상은 그 네임스페이스의 Service 이름
  (`<service>.<namespace>.svc`) 또는 Pod IP(도구가 `ctx.k8s`로 읽은 값)만 받는다.
  Service 이름에는 점을 받지 않아 다른 네임스페이스·외부 호스트 이름은 만들 수 없다.
  Pod IP는 형식과 루프백·링크 로컬(메타데이터 주소)·미지정·멀티캐스트가 아닌지만 본다.
  그 IP가 대상 네임스페이스의 Pod인지는 도구가 `ctx.k8s`로 읽은 값만 넘겨서 지킨다.
  ExternalName Service(DNS로 바깥을 가리킴)를 거르는 것도 Service를 읽는 도구가 한다.
- 경로는 `/`로 시작하는 경로만 받고, 만든 URL의 호스트·포트가 의도한 값인지 다시 확인한다.
- 리다이렉트는 따라가지 않는다 (범위 밖으로 나가지 않기 위함). 응답 본문은 64KiB까지만 읽는다.
- 프록시 환경 변수(`HTTP_PROXY` 등)는 따르지 않는다. 클러스터 안만 부르므로 프록시를 거치면
  요청·계정이 밖으로 나가고 프록시 오류가 앱 응답처럼 보인다.
- 어떤 경로를 어떤 메서드로 부를지(허용 목록 GET, 시나리오)는 도구가 정한다.
- 연결 오류·시간 초과는 `HttpCallError`로 바꾼다. 도구는 이것을 실패 근거로 남긴다.
"""

import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 64 * 1024

_SERVICE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")  # DNS-1035 label (점 없음)
# `/`로 시작하는 경로 (`//host` 제외). pydantic(Rust regex)에서도 쓰므로 look-ahead를 쓰지 않는다
PATH_PATTERN = r"^(/|/[A-Za-z0-9._~!$&'()*+,;=:%?-][A-Za-z0-9._~!$&'()*+,;=:%/?-]*)$"
_PATH = re.compile(PATH_PATTERN)


class HttpCallError(Exception):
    """HTTP 호출 실패 (연결 오류·시간 초과). 메시지는 사유 한 줄이다."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    elapsed_ms: float
    text: str
    truncated: bool = False

    def json(self) -> Any | None:
        """JSON 본문이면 해석한 값, 아니면 None."""
        try:
            return json.loads(self.text)
        except ValueError:
            return None


class NamespacedHttp:
    def __init__(
        self,
        namespace: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        accounts: Mapping[str, Mapping[str, str]] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._namespace = namespace
        self._timeout_s = timeout_s
        self._accounts = dict(accounts or {})
        self._client = httpx.Client(follow_redirects=False, trust_env=False, transport=transport)

    @property
    def namespace(self) -> str:
        return self._namespace

    def get(
        self,
        *,
        port: int,
        path: str,
        service: str | None = None,
        pod_ip: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        url = self._url(service, pod_ip, port, path)
        return self._send("GET", url, timeout_s, headers=headers)

    def post(
        self,
        *,
        service: str,
        port: int,
        path: str,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        url = self._url(service, None, port, path)
        return self._send("POST", url, timeout_s, json=json, headers=headers)

    def account(self, secret_name: str) -> dict[str, str]:
        """시나리오용 테스트 계정 (Secret으로 주입). 없으면 KeyError."""
        if secret_name not in self._accounts:
            raise KeyError(f"테스트 계정이 주입되지 않음: {secret_name}")
        return dict(self._accounts[secret_name])

    def _url(self, service: str | None, pod_ip: str | None, port: int, path: str) -> str:
        if (service is None) == (pod_ip is None):
            raise ValueError("service와 pod_ip 중 하나만 준다")
        if service is not None:
            if not _SERVICE.fullmatch(service):
                raise ValueError(f"대상 네임스페이스의 Service 이름이 아님: {service!r}")
            host = f"{service}.{self._namespace}.svc"
        else:
            try:
                ip = ipaddress.ip_address(pod_ip)
            except ValueError as e:
                raise ValueError(f"Pod IP가 아님: {pod_ip!r}") from e
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                raise ValueError(f"Pod IP로 쓸 수 없는 주소: {pod_ip!r}")
            host = str(ip)
            if ":" in host:
                host = f"[{host}]"
        if not 1 <= port <= 65535:
            raise ValueError(f"포트 범위 밖: {port}")
        if not _PATH.fullmatch(path):
            raise ValueError(f"경로 형식이 아님: {path!r}")
        url = f"http://{host}:{port}{path}"
        parsed = httpx.URL(url)
        if parsed.host != host.strip("[]") or (parsed.port or 80) != port:  # 80은 None으로 읽힌다
            raise ValueError(f"주소가 대상과 다름: {url}")
        return url

    def _send(self, method: str, url: str, timeout_s: float | None, **kwargs: Any) -> HttpResponse:
        started = time.monotonic()
        try:
            with self._client.stream(
                method, url, timeout=timeout_s or self._timeout_s, **kwargs
            ) as resp:
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        break
                elapsed = (time.monotonic() - started) * 1000
                text = body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
                return HttpResponse(
                    resp.status_code, round(elapsed, 1), text, len(body) > MAX_BODY_BYTES
                )
        except httpx.HTTPError as e:
            raise HttpCallError(f"{type(e).__name__}: {e}") from e
