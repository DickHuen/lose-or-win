"""Exchange-neutral data types and the interface the engine talks to.

The live implementation (polymarket.py) wraps the official Polymarket SDK;
the mock (mock.py) is used by the unit tests and `selftest`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Order statuses (official SDK PerpsOrderStatus, polymarket-client 0.11.0).
# Exchange price rule (not a strategy number): at most this many significant figures. Live 2026-09-30 the exchange
# refused a 6-figure BTC price ("price exceeds allowed significant figures"); its book quoted whole dollars (83264).
PRICE_SIG_FIGS = 5
ACTIVE_TRIGGER_STATUSES = {"untriggered", "armed"}
ACTIVE_ORDER_STATUSES = {"accepted", "open", "partial", "untriggered", "armed"}
FILLED_STATUSES = {"filled"}
NOT_FILLED_TERMINAL = {
    "cancelled", "auto_cancelled", "post_only_rejected", "fok_unfilled", "ioc_no_fill", "ioc_expired",
    "stp_cancelled", "zero_quantity", "duplicate_order", "order_not_found", "reduce_only_invalid",
    "reduce_only_expired", "order_expired", "parent_cancelled", "position_closed", "position_flipped",
    "reduce_only_invalid_at_trigger", "expired",
}


class ExchangeError(Exception):
    """Transport / unexpected-response failure. Outcome of a command may be unknown."""


class OrderRejected(ExchangeError):
    """The exchange explicitly rejected a command."""

    def __init__(self, message: str, *, restriction: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.restriction = restriction
        self.code = code


@dataclass
class Instrument:
    id: int
    symbol: str
    base_asset: str
    quote_asset: str
    category: str
    quantity_decimals: int
    price_decimals: int
    min_notional: float
    max_market_notional: float
    max_limit_notional: float
    max_leverage: int
    isolated_only: bool
    price_bounds: float
    max_order_count: int
    funding_interval: str
    risk_tiers: list[tuple[float, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Ticker:
    instrument_id: int
    mark: float
    index: float
    last: float
    mid: float
    funding_rate: float
    open_interest: float
    next_funding_ms: int
    ts_ms: int | None = None


@dataclass
class Book:
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    ts_ms: int = 0

    @property
    def best_bid(self) -> float:
        if not self.bids:
            raise ExchangeError("order book has no bids")
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        if not self.asks:
            raise ExchangeError("order book has no asks")
        return self.asks[0][0]


@dataclass
class Position:
    instrument_id: int
    size: float                 # signed: >0 long, <0 short
    entry_price: float
    leverage: int
    cross: bool
    liquidation_price: float
    unrealized_pnl: float
    cumulative_funding: float
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    position_value: float = 0.0

    @property
    def direction(self) -> int:
        return 1 if self.size > 0 else -1 if self.size < 0 else 0


@dataclass
class Balance:
    asset: str
    balance: float
    value: float


@dataclass
class AccountSnapshot:
    balances: list[Balance]
    positions: list[Position]
    total_account_value: float
    withdrawable: float
    in_liquidation: bool
    ts_ms: int

    def position(self, instrument_id: int) -> Position | None:
        for p in self.positions:
            if p.instrument_id == instrument_id and p.size != 0:
                return p
        return None


@dataclass
class AccountConfig:
    instrument_id: int
    leverage: int
    cross: bool


@dataclass
class Order:
    id: int
    instrument_id: int
    side: str                   # BUY | SELL
    price: float
    quantity: float
    tif: str
    reduce_only: bool
    status: str
    filled_quantity: float
    resting_quantity: float
    client_order_id: str | None = None
    tpsl_kind: str | None = None      # tp | sl
    tpsl_scope: str | None = None     # order | position
    trigger_price: float | None = None
    parent_order_id: int | None = None
    created_ms: int = 0
    updated_ms: int = 0

    @property
    def is_trigger(self) -> bool:
        return self.tpsl_kind is not None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_ORDER_STATUSES


@dataclass
class Fill:
    trade_id: int
    order_id: int
    instrument_id: int
    side: str                   # long | short (official SDK PerpsFill.side)
    price: float
    quantity: float
    taker: bool
    fee: float
    fee_asset: str
    previous_size: float
    previous_entry_price: float
    pnl: float
    liquidation: bool
    ts_ms: int
    client_order_id: str | None = None

    # The bot never adds to a position and flips in two steps, so previous_size alone
    # classifies fills without relying on the meaning of `side` (see API_NOTES).
    @property
    def is_opening(self) -> bool:
        return abs(self.previous_size) < 1e-12

    @property
    def is_reducing(self) -> bool:
        return abs(self.previous_size) >= 1e-12


@dataclass
class FundingPayment:
    id: int
    instrument_id: int
    size: float
    funding_rate: float
    funding: float
    ts_ms: int


@dataclass
class PlaceResult:
    accepted: bool
    order_id: int | None = None
    client_order_id: str | None = None
    tp_order_id: int | None = None
    sl_order_id: int | None = None
    order: Order | None = None
    error: str | None = None
    restriction: str | None = None
    outcome_unknown: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CancelResult:
    order_id: int
    ok: bool
    error: str | None = None


@dataclass
class ProxyKeyInfo:
    owner_address: str
    proxy: str
    expires_at_ms: int | None


@dataclass
class Flow:
    key: str                    # deposit hash / withdrawal id
    kind: str                   # deposit | withdrawal
    amount: float
    status: str
    ts_ms: int


class Exchange:
    """Interface. All methods are synchronous; implementations handle their own I/O."""

    # public
    def get_instruments(self) -> list[Instrument]: raise NotImplementedError
    def get_ticker(self, instrument_id: int) -> Ticker: raise NotImplementedError
    def get_book(self, instrument_id: int, depth: int) -> Book: raise NotImplementedError
    def get_klines(self, instrument_id: int, interval: str, start_ms: int, end_ms: int) -> list[Any]: raise NotImplementedError
    def get_funding_history(self, instrument_id: int, start_ms: int, end_ms: int) -> list[tuple[int, float]]: raise NotImplementedError
    def get_fee_schedule(self) -> list[dict[str, Any]]: raise NotImplementedError
    def get_geoblock(self) -> dict[str, Any]: raise NotImplementedError
    def get_server_time_raw(self) -> Any: raise NotImplementedError
    # authenticated reads
    def get_account(self) -> AccountSnapshot: raise NotImplementedError
    def get_account_config(self, instrument_id: int) -> AccountConfig | None: raise NotImplementedError
    def get_open_orders(self, instrument_id: int) -> list[Order]: raise NotImplementedError
    def get_orders(self, *, order_id: int | None = None, client_order_id: str | None = None,
                   instrument_id: int | None = None, start_ms: int | None = None,
                   end_ms: int | None = None) -> list[Order]: raise NotImplementedError
    def get_fills(self, start_ms: int, end_ms: int | None = None) -> list[Fill]: raise NotImplementedError
    def get_funding_payments(self, instrument_id: int, start_ms: int, end_ms: int | None = None) -> list[FundingPayment]: raise NotImplementedError
    def get_proxy_key_info(self) -> ProxyKeyInfo | None: raise NotImplementedError
    def get_flows(self, start_ms: int) -> list[Flow]: raise NotImplementedError
    # signed commands
    def update_leverage(self, instrument_id: int, leverage: int, cross: bool) -> AccountConfig: raise NotImplementedError
    def place_order(self, *, instrument_id: int, side: str, quantity: str, tif: str, price: str | None,
                    reduce_only: bool, client_order_id: str, tp_trigger: str | None = None,
                    sl_trigger: str | None = None) -> PlaceResult: raise NotImplementedError
    def place_position_tpsl(self, *, instrument_id: int, tp_trigger: str | None,
                            sl_trigger: str | None) -> PlaceResult: raise NotImplementedError
    def cancel_orders(self, order_ids: list[int]) -> list[CancelResult]: raise NotImplementedError
    def probe_proxy_withdrawal(self, *, owner: str, amount_base_units: int) -> dict[str, Any]: raise NotImplementedError
    def close(self) -> None: pass


def parse_server_time_ms(raw: Any) -> int | None:
    """Server time in ms from GET /v1/info/time (shape not in the SDK: first large number found)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = raw
    elif isinstance(raw, dict):
        v = next((x for x in raw.values() if isinstance(x, (int, float)) and not isinstance(x, bool)
                  and x > 1_000_000_000), None)
        if v is None:
            return None
    else:
        return None
    return int(v if v > 10_000_000_000 else v * 1000)


def taker_fee_for(schedule: list[dict[str, Any]], category: str, estimate: float) -> tuple[float, bool]:
    """(taker fee rate, listed) from GET /v1/info/fees (v1.5.2). The exchange may leave the instrument's category
    out of its schedule (seen live: only "equity" listed). Then the highest listed taker rate or the config
    estimate is used, whichever is higher, so the bot never assumes a lower fee than it has evidence for."""
    row = next((e for e in schedule if e.get("category") == category and e.get("taker_fee_rate") is not None), None)
    if row is not None:
        return float(row["taker_fee_rate"]), True
    listed = [float(e["taker_fee_rate"]) for e in schedule if e.get("taker_fee_rate") is not None]
    return max([float(estimate), *listed]), False


def mark_vs_book(mark: float, book: Book, bounds: float) -> tuple[bool, float | None]:
    """(consistent, deviation as a fraction) v1.5.3: the mark must sit near the SAME instrument's order book.
    Live 2026-09-30 the ticker belonged to another instrument (mark 7690.6, book 83264/83265). Tolerance: the
    instrument's own price bound (orders further than that from the mark are refused anyway), at least 2%."""
    if not book.bids or not book.asks or not mark or mark != mark:
        return False, None
    mid = (book.bids[0][0] + book.asks[0][0]) / 2.0
    dev = abs(mark / mid - 1.0)
    return dev <= max(float(bounds or 0.0), 0.02), dev
