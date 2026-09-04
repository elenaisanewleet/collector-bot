"""Placeholder for a future 1С integration.

Not implemented, and deliberately so.

Connecting to the customer's 1С requires facts we do not have: which
configuration they run, where it is hosted, whether OData or HTTP Services are
exposed, which catalogues and document types hold debtor data, what the fields
are called, and what rights our account will be granted. Writing an adapter
against guesses would produce code that looks finished and works nowhere.

When that access exists, the work is to implement
:class:`~app.providers.internal.base.InternalDebtorProvider` here and register it
in place of the CSV provider. No service, handler, scoring rule or report
touches 1С directly, so nothing above this layer needs to change.

See the README section "Что потребуется для подключения 1С" for the list of
details to collect from the customer.
"""

from __future__ import annotations

from typing import NoReturn


class OneCODataProvider:
    """Future integration.

    Not implemented because access to the customer's 1C instance
    and its schema is not currently available.

    This class is never instantiated by the running application: the registry
    does not reference it, and it is excluded from the provider wiring.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise NotImplementedError(
            "OneCODataProvider is a placeholder. Implement it once the customer's "
            "1C access and schema are known; see app/providers/internal/onec_future.py."
        )
