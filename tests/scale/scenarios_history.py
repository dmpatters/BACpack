"""Area 3, the history file at its cap (about 10.2 million samples by the
counter): startup, the storage page, the global prune, the recount and a
sample written at the cap."""
from __future__ import annotations

from unittest import mock

from bacpack import trending
from bacpack.db import init_db, make_engine
from bacpack.trending import TREND_HARD_CAP_BYTES, TrendPruner

from .scenarios import Context, scenario
from .support import web_client


def _client(ctx: Context) -> None:
    web_client(ctx)


@scenario("history.init_db_at_cap", area="history", dataset="full_history")
def init_db_at_cap(ctx: Context):
    """Startup of the parent process against the full history file."""
    engine = make_engine(ctx.db_path)
    try:
        init_db(engine)
    finally:
        engine.dispose()


@scenario("history.storage_api", area="history", dataset="full_history", setup=_client)
def storage_api(ctx: Context):
    return ctx.state["client"].get("/api/trends/storage")


HALF_LIMIT_MB = TREND_HARD_CAP_BYTES // 2 // (1024 * 1024)


def _save_half_limit(ctx: Context):
    """The settings save lowering the soft limit to half the hard cap. The
    background pruner is kept from starting, so what runs after is the
    caller's to time."""
    with mock.patch.object(TrendPruner, "kick", lambda self: None):
        return web_client(ctx).put("/api/settings/network", json={"trend_soft_limit_mb": HALF_LIMIT_MB})


@scenario("history.prune_global_half", area="history", dataset="full_history", writes=True, setup=_client)
def prune_global_half(ctx: Context):
    """The settings save that halves the soft limit; the excess (about 5.1
    million samples) is left to the background pruner."""
    return _save_half_limit(ctx)


def _lower_the_limit(ctx: Context) -> None:
    assert _save_half_limit(ctx).status_code == 200


@scenario(
    "history.background_prune_half",
    area="history",
    dataset="full_history",
    writes=True,
    setup=_lower_the_limit,
)
def background_prune_half(ctx: Context):
    """The background prune after the limit was halved, run to completion."""
    return trending.prune_to_limit(ctx.session_factory)


@scenario("history.recount", area="history", dataset="full_history")
def recount(ctx: Context):
    return trending.resync_trend_total_sample_count(ctx.session_factory)


@scenario("history.record_sample_at_cap", area="history", dataset="full_history", writes=True)
def record_sample_at_cap(ctx: Context):
    """One sample into a history already at the hard cap (it prunes the
    oldest sample)."""
    trending.record_sample(ctx.session_factory, ctx.ids.extra_trended_ids[0], 1.0)
