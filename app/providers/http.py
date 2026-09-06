"""Shared HTTP plumbing for providers.

Timeouts, bounded retries and error classification are implemented once here.
Every failure mode an external API can present is mapped onto a
:class:`~app.providers.base.ProviderError` so no raw ``httpx`` exception ever
escapes the provider layer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_setup import get_logger
from app.providers.base import ProviderError, ProviderUnavailableError

logger = get_logger(__name__)

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_PAYMENT_REQUIRED = 402
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_FLOOR = 500
MAX_RETRY_AFTER_SECONDS = 30.0
#: Переадресаций на один запрос. Больше одной-двух не бывает ни у одного из
#: источников, но цепочку надо чем-то оборвать.
MAX_REDIRECTS = 5


class ProviderAuthError(ProviderError):
    """Credentials were rejected. Retrying will not help."""

    def __init__(self, message: str = "authentication rejected") -> None:
        super().__init__("unauthorized", message)


class ProviderRateLimitedError(ProviderUnavailableError):
    def __init__(self, message: str = "rate limited") -> None:
        super().__init__("rate_limited", message)


class ProviderBadResponseError(ProviderError):
    """The response arrived but could not be understood."""

    def __init__(self, code: str = "bad_response", message: str = "") -> None:
        super().__init__(code, message)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry with exponential backoff.

    Only transient conditions are retried: timeouts, connection errors, 429 and
    5xx. A 401 or a malformed body is retried zero times, because repeating the
    call just burns quota against a deterministic failure.
    """

    max_retries: int = 2
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0

    def delay_for(self, attempt: int) -> float:
        return float(min(self.backoff_seconds * (2**attempt), self.max_backoff_seconds))


def build_client(
    *,
    base_url: str,
    timeout_seconds: float,
    headers: Mapping[str, str] | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        headers=dict(headers or {}),
        # Переадресации не отдаются httpx на откуп: он идёт по ``Location`` куда
        # угодно, вместе с телом и заголовками. Их ведёт request_raw, проверяя
        # каждый переход, — см. _reject_unsafe_redirect.
        follow_redirects=False,
    )


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    retry: RetryPolicy | None = None,
    provider: str = "unknown",
    credentialed: bool = True,
) -> tuple[Any, str]:
    """Perform a request and decode JSON.

    Returns the decoded payload plus the raw body text, so a caller running with
    ``STORE_RAW_RESPONSES`` enabled can persist exactly what arrived.
    """
    response = await request_raw(
        client,
        method,
        url,
        params=params,
        json_body=json_body,
        retry=retry,
        provider=provider,
        credentialed=credentialed,
    )
    return _decode(response), response.text


async def request_text(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    retry: RetryPolicy | None = None,
    provider: str = "unknown",
    credentialed: bool = True,
) -> str:
    """Perform a request and return the body as text.

    Same retry loop and the same classification as :func:`request_json`, minus
    the JSON decode. Exists for one shape of source: the one whose JSON endpoint
    is guarded by a CSRF token that has to be read off an HTML page first. A
    second retry loop written next to this one would drift from it, and the
    difference between "the site was down" and "the register is empty" is
    exactly what these loops encode.
    """
    response = await request_raw(
        client,
        method,
        url,
        params=params,
        retry=retry,
        provider=provider,
        credentialed=credentialed,
    )
    return response.text


async def request_raw(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    retry: RetryPolicy | None = None,
    provider: str = "unknown",
    credentialed: bool = True,
) -> httpx.Response:
    """The retry loop itself: one accepted response, or a :class:`ProviderError`.

    ``credentialed`` — посылаем ли мы этому источнику ключ. См. :func:`_classify`:
    отвергнуть можно только предъявленное.
    """
    policy = retry or RetryPolicy()
    last_error: ProviderError | None = None

    for attempt in range(policy.max_retries + 1):
        try:
            response = await _send_following_safe_redirects(
                client, method, url, params=params, json_body=json_body, provider=provider
            )
        except httpx.TimeoutException as exc:
            last_error = ProviderUnavailableError("timeout", str(exc) or "request timed out")
        except httpx.HTTPError as exc:
            last_error = ProviderUnavailableError(
                "connection_error", f"{type(exc).__name__}: {exc}"
            )
        else:
            error = _classify(response, provider=provider, credentialed=credentialed)
            if error is None:
                return response
            if not _is_retryable(error):
                raise error
            last_error = error
            await _honour_retry_after(response, error)

        if attempt < policy.max_retries:
            logger.info(
                "provider.retry",
                provider=provider,
                attempt=attempt + 1,
                error_code=last_error.code if last_error else None,
            )
            await asyncio.sleep(policy.delay_for(attempt))

    raise last_error or ProviderUnavailableError("unknown", "request failed")


async def _send_following_safe_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None,
    json_body: Mapping[str, Any] | None,
    provider: str,
) -> httpx.Response:
    """Переадресации вручную, с проверкой каждого перехода.

    ``follow_redirects=True`` означал, что источник сам решает, куда уйдёт
    запрос: на 307 с ``Location`` на посторонний домен httpx послушно повторял
    туда POST — целиком, с телом и заголовками. Чужой хост получал ФИО
    должника и ``X-CSRFToken``, а его ответ разбирался как ответ реестра.
    Работало это на любом источнике, не только на наследственных делах: хватало
    ответа с ``Location``.

    Поэтому смена хоста и понижение схемы запрещены, а безобидные переходы
    внутри того же хоста (косая черта в конце, канонический путь) остаются
    рабочими.
    """
    request = client.build_request(method, url, params=params, json=json_body)
    for _ in range(MAX_REDIRECTS):
        # follow_redirects передаётся явно, а не берётся из клиента: правило не
        # должно зависеть от того, как собран конкретный клиент.
        response = await client.send(request, follow_redirects=False)
        following = response.next_request
        if following is None:
            return response
        _reject_unsafe_redirect(request.url, following.url, provider=provider)
        logger.info(
            "provider.redirect",
            provider=provider,
            status=response.status_code,
            path=following.url.path,
        )
        await response.aclose()
        request = following

    raise ProviderBadResponseError(
        "too_many_redirects", f"{provider}: переадресации не кончаются (предел {MAX_REDIRECTS})"
    )


def _reject_unsafe_redirect(current: httpx.URL, target: httpx.URL, *, provider: str) -> None:
    """Перейти можно только туда, куда мы и собирались, — или никуда.

    Ошибка, а не тихий возврат ответа редиректа: «источник увёл нас в сторону»
    обязано быть видно как сбой проверки. Молча отданный 307 разобрался бы как
    непонятное тело, то есть как «проверено, ничего не найдено».
    """
    reason: str | None = None
    if target.host != current.host:
        reason = f"переадресация с {current.host} на посторонний хост {target.host}"
    elif current.scheme == "https" and target.scheme != "https":
        reason = f"переадресация с https на {target.scheme}"
    if reason is None:
        return
    logger.warning(
        "provider.redirect_blocked",
        provider=provider,
        target_host=target.host,
        target_scheme=target.scheme,
    )
    raise ProviderBadResponseError(
        "redirect_blocked", f"{provider}: {reason} — запрос не отправлен"
    )


def _classify(
    response: httpx.Response, *, provider: str, credentialed: bool = True
) -> ProviderError | None:
    status = response.status_code
    # Порог — 400, а не 401. С 401 всякий ответ 4xx ниже него считался успехом и
    # уходил в разбор тела: при пустом теле источник докладывался как приславший
    # мусор, а при теле вида ``{"data": []}`` — как ответивший пусто. Второе и
    # есть запрещённая подмена: отвергнутый запрос, показанный как «ничего не
    # найдено». Для 1С это отвергнутый ``$filter``, для NewDB — невалидные
    # параметры, и оба обязаны быть видимой ошибкой.
    if status < HTTP_BAD_REQUEST:
        return None
    if status in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}:
        if not credentialed:
            # Источнику, которому мы не предъявляем ключа, отвергнуть нечего.
            # Открытый сайт отвечает 403 на запрос, который ему не понравился:
            # ушла cookie-сессия, протух csrf-токен, не тот User-Agent, слишком
            # часто спрашиваем. Названное ``unauthorized``, это попадало в
            # ``REFUSAL_CODES`` — три должника подряд, и массовый прогон на
            # восемьсот строк вставал, сообщая оператору про баланс и ключ,
            # которых у бесплатного реестра нет. Бесплатный вспомогательный
            # источник не имеет права остановить платную работу; проверка
            # остаётся непроверенной, и это видно в строке этого должника.
            return ProviderUnavailableError(
                "rejected", f"{provider}: источник отклонил запрос (HTTP {status})"
            )
        return ProviderAuthError(f"{provider} rejected the credentials (HTTP {status})")
    if status == HTTP_PAYMENT_REQUIRED:
        # A depleted prepaid balance is not a transient fault: retrying spends
        # nothing and fixes nothing, and the operator needs to be told plainly.
        return ProviderBadResponseError(
            "payment_required", f"{provider}: недостаточно средств на счёте (HTTP {status})"
        )
    if status == HTTP_TOO_MANY_REQUESTS:
        return ProviderRateLimitedError(f"{provider} rate limit reached")
    if status >= HTTP_SERVER_ERROR_FLOOR:
        return ProviderUnavailableError("server_error", f"HTTP {status}")
    return ProviderBadResponseError("http_error", f"HTTP {status}")


def _is_retryable(error: ProviderError) -> bool:
    return error.code in {"rate_limited", "server_error", "timeout", "connection_error"}


async def _honour_retry_after(response: httpx.Response, error: ProviderError) -> None:
    """Respect a ``Retry-After`` header when the server sends a sane one."""
    if error.code != "rate_limited":
        return
    raw = response.headers.get("Retry-After")
    if not raw:
        return
    try:
        delay = float(raw)
    except ValueError:
        return
    await asyncio.sleep(min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS))


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderBadResponseError(
            "malformed_json", f"response was not valid JSON: {exc}"
        ) from exc
