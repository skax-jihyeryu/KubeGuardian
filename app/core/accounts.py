"""시나리오용 테스트 계정 읽기 (ADR-001).

Secret을 볼륨으로 마운트한 디렉터리를 읽는다: `<디렉터리>/<Secret 이름>/<키>` 파일이 값이다.
값은 이 프로세스의 `NamespacedHttp`에만 들어가고, 도구 입력·LLM 프롬프트·로그에는 나가지 않는다.
"""

from pathlib import Path

from core.error.service_exception import ErrorCode, ServiceException


def load_accounts(directory: str | Path) -> dict[str, dict[str, str]]:
    """Secret 이름 → {키: 값}. 디렉터리가 없으면 빈 dict (계정 없이 점검만 한다)."""
    root = Path(directory)
    if not root.is_dir():
        return {}
    accounts: dict[str, dict[str, str]] = {}
    for secret in sorted(root.iterdir()):
        # `..data`·`..<타임스탬프>`는 k8s 볼륨 마운트가 만드는 메타 항목이다
        if secret.name.startswith("..") or not secret.is_dir():
            continue
        accounts[secret.name] = {
            f.name: _value(f)
            for f in sorted(secret.iterdir())
            if not f.name.startswith("..") and f.is_file()
        }
    return accounts


def _value(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # 사유에는 위치와 예외 종류만 쓴다. 값과 예외 메시지(파일 내용이 섞일 수 있음)는 넣지 않는다
        raise ServiceException(
            ErrorCode.INTERNAL_ERROR,
            f"테스트 계정 파일을 읽지 못함: {path.parent.name}/{path.name} ({type(e).__name__})",
        ) from e
    return text.removesuffix("\n")
