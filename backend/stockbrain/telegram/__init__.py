"""Telegram: StockBrain's second first-class user interface.

Layering, outermost first:

``runtime``      lifecycle, long polling, backoff, health
``handlers``     thin chat adapters -- authenticate, parse, call, format
``approvals``    the server side of every button, including two-stage confirm
``notifier``     outbound proposal notifications, deduplicated in PostgreSQL
``service``      the read model every command queries
``messages``     pure rendering
``tokens``       opaque single-use callback tokens over ``approval_actions``
``auth``         numeric-id allowlists and the chat policy
``formatting``   HTML escaping and length bounding
``transport``    the four outbound operations, as a protocol

The trust boundary: Telegram supplies a numeric user id, a numeric chat id and
an opaque token.  It never supplies a ticker, a side, a quantity, a price, an
account or an authorization source, and it never receives a broker credential.
Authorization goes through the same
:meth:`~stockbrain.proposals.service.ProposalService.authorize` the web uses --
there is no Telegram-specific risk path to diverge from it.
"""

from stockbrain.telegram.runtime import TelegramRuntime

__all__ = ["TelegramRuntime"]
