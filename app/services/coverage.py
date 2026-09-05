"""Что откроется от каждого недостающего поля — посчитанное, а не написанное.

Карточка запроса обязана говорить не абстрактное «заполните поле», а «добавьте
ИНН — откроются банкротство, статус ИП и арбитраж». Соблазн написать это
строкой велик, и один раз ему уже поддались: в ``report_actions`` подписи кнопок
перечисляли источники словами. Такая строка разъезжается с провайдером при
первой правке гейта, и разъехавшись, врёт — обещает источник, который откажется
отвечать, или молчит про тот, который ответил бы.

Поэтому здесь ничего не написано. Всё считается предикатом
:meth:`app.providers.base.BaseProvider.missing_input_for`, который сам провайдер
зовёт из своего ``_fetch``. Провайдер и обещание не могут разойтись, потому что
это один и тот же код.

Считается от **субъекта**, а не от прошлого отчёта. У ``missing_input`` нет
колонки в БД, и на кэш-хите отчёт откатывается на текст сообщения провайдера —
карточка, построенная по отчёту, занизила бы список.

Не подключённый источник не попадает ни в «спрошу», ни в «не спрошу»: оператор
не сделает с ним ничего, а место в карточке занимает. Он остаётся виден в самом
отчёте, где :func:`app.bot.view._gaps` печатает «Не подключено» отдельной
строкой, — и это правильное для него место.
"""

from __future__ import annotations

from datetime import date

from app.domain.enums import MissingInput, ProviderName
from app.domain.identity import PersonName, SearchSubject, VehicleDescriptor
from app.providers.base import BaseProvider
from app.providers.registry import ProviderRegistry

#: Внутренняя база отвечает всегда: это наш собственный файл, ключа она не
#: требует и «нечем спросить» сказать не может. В ``registry.external`` её нет,
#: поэтому в списке опрашиваемых она появляется отсюда.
INTERNAL = ProviderName.INTERNAL


def _providers(subject: SearchSubject, registry: ProviderRegistry) -> list[BaseProvider]:
    """Все источники, у которых стоит спрашивать про покрытие.

    Мост «паспорт → ИНН» входит намеренно: записей он не приносит, но именно он
    отвечает на вопрос «что даст паспорт», и без него эта строка карточки была
    бы пустой ровно там, где паспорт единственный доступный ход.

    И выпадает так же намеренно, когда ИНН физлица уже известен: сервис поиска
    его тогда не зовёт вовсе (``InnBridgeProvider.is_needed``), и предлагать за
    него паспорт значило бы продать платный вызов за уже имеющийся ответ.
    """
    bridge = registry.inn_bridge
    needed = bridge is not None and bridge.is_needed(subject)
    return [*registry.external, *([bridge] if bridge is not None and needed else [])]


def will_answer(subject: SearchSubject, registry: ProviderRegistry) -> tuple[ProviderName, ...]:
    """Кого спросим на этих данных. Порядок — как в реестре, он стабилен."""
    return (
        INTERNAL,
        *(
            provider.name
            for provider in _providers(subject, registry)
            if provider.is_configured and not provider.missing_input_for(subject)
        ),
    )


def blocked(
    subject: SearchSubject, registry: ProviderRegistry
) -> dict[tuple[MissingInput, ...], tuple[ProviderName, ...]]:
    """Кого не спросим и почему, сгруппировано по причине.

    Группировка по причине, а не по источнику, — то же решение, что в
    :func:`app.bot.view._gaps`, и по той же причине: «ЕФРСБ, ФНС, Суды — нужен
    ИНН» это одно действие оператора, а три строки подряд про одно и то же
    читаются как три беды.

    Мост «паспорт → ИНН» сюда не попадает, хотя в :func:`will_answer` он есть.
    Он не реестр фактов: записей не приносит и покрытие отчёта не увеличивает,
    а строка «ИНН по паспорту (ФНС) — нужны серия и номер паспорта» в списке
    неопрошенных источников читалась бы как ещё одна потеря данных. Что даст
    паспорт, говорит :func:`unlocked_by` — там мост на своём месте.
    """
    groups: dict[tuple[MissingInput, ...], list[ProviderName]] = {}
    for provider in registry.external:
        if not provider.is_configured:
            continue
        missing = provider.missing_input_for(subject)
        if missing:
            groups.setdefault(tuple(missing), []).append(provider.name)
    return {reason: tuple(names) for reason, names in groups.items()}


def unlocked_by(
    field: str, subject: SearchSubject, registry: ProviderRegistry
) -> tuple[ProviderName, ...]:
    """Что откроется, если добавить это поле, — разница двух ответов.

    Не список, переписанный из провайдеров, а вычитание: ``will_answer`` на
    субъекте с подставленным значением минус ``will_answer`` на нынешнем. Отсюда
    два честных ответа, которых захардкоженный список дать не мог. Телефон
    открывает **пусто** — ни один внешний реестр по нему не ищет, и обещать
    иное значило бы врать формой. Паспорт открывает **мост**, а не три источника
    напрямую: три откроются потом, если мост вернёт ИНН, и «если» тут не
    формальность — ФНС по этим данным вполне может не найти ничего.
    """
    probe = _probe(field)
    if probe is None:
        return ()
    before = set(will_answer(subject, registry))
    after = will_answer(subject.model_copy(update=probe), registry)
    return tuple(name for name in after if name not in before)


#: Значения-заглушки, которыми проверяется «а если бы поле было?». Наружу не
#: уходят никогда: ``missing_input_for`` — чистая функция от полей субъекта.
#: Тот же приём, что у ``report_actions._PROBE_PASSPORT``, и он там уже доказал,
#: что предикат провайдера — единственный способ не соврать кнопкой.
_PROBE_INN = "0" * 12
_PROBE_PASSPORT = "0" * 10
_PROBE_PHONE = "+70000000000"
_PROBE_VIN = "X" * 17


def _probe(field: str) -> dict[str, object] | None:
    match field:
        case "birth_date":
            return {"birth_date": date(1980, 1, 1)}
        case "inn":
            return {"inn": _PROBE_INN}
        case "passport":
            return {"passport": _PROBE_PASSPORT}
        case "phone":
            return {"phone": _PROBE_PHONE}
        case "vin":
            return {"vehicle": VehicleDescriptor(vin=_PROBE_VIN)}
        case "fio" | "name":
            return {"name": PersonName(last_name="Пробный", first_name="Пробный")}
        case _:
            return None


__all__ = ["blocked", "unlocked_by", "will_answer"]
