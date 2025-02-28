from decimal import Decimal
from typing import List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.core.data_type.common import PositionAction, PositionSide, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAExecutorConfig, DCAMode
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction


class DManMakerV2Config(MarketMakingControllerConfigBase):
    """
    Configuration required to run the D-Man Maker V2 strategy.
    """

    controller_name: str = "dman_maker_v2"
    candles_config: List[CandlesConfig] = []

    # DCA configuration
    dca_spreads: List[Decimal] = Field(
        default="0.01,0.02",
        client_data=ClientFieldData(
            is_updatable=True,
            prompt_on_new=True,
            prompt=lambda mi: "Enter a comma-separated list of spreads for each DCA level: "
        ),
    )
    dca_amounts: List[Decimal] = Field(
        default="0.1,0.2",
        client_data=ClientFieldData(
            is_updatable=True,
            prompt_on_new=True,
            prompt=lambda mi: "Enter a comma-separated list of amounts for each DCA level: "
        ),
    )
    time_limit: int = Field(
        default=60 * 60,
        gt=0,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the time limit for each DCA level: ",
            prompt_on_new=False),
    )
    stop_loss: Decimal = Field(
        default=Decimal("0.03"),
        gt=0,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the stop loss (as a decimal, e.g., 0.03 for 3%): ",
            prompt_on_new=True
        ),
    )
    top_executor_refresh_time: Optional[float] = Field(
        default=None, client_data=ClientFieldData(is_updatable=True, prompt_on_new=False)
    )
    executor_activation_bounds: Optional[List[Decimal]] = Field(
        default=None,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the activation bounds for the orders "
            "(e.g., 0.01 activates the next order when the price is closer than 1%): ",
            prompt_on_new=True,
        ),
    )
    candles_connector: str = Field(
        default=None,
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
        ),
    )
    candles_trading_pair: str = Field(
        default=None,
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
        ),
    )
    interval: str = Field(
        default="5m",
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            prompt_on_new=False,
        ),
    )
    natr_length: int = Field(
        default=14,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the NATR length: ",
            prompt_on_new=True),
    )
    eagerness: int = Field(
        default = 1,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter the eagerness: ",
            prompt_on_new=False,
        )
    )

    @validator("candles_connector", pre=True, always=True)
    def set_candles_connector(cls, v, values):
        if v is None or v == "":
            return values.get("connector_name")
        return v

    @validator("candles_trading_pair", pre=True, always=True)
    def set_candles_trading_pair(cls, v, values):
        if v is None or v == "":
            return values.get("trading_pair")
        return v

    @validator("executor_activation_bounds", pre=True, always=True)
    def parse_activation_bounds(cls, v):
        if isinstance(v, list):
            return [Decimal(val) for val in v]
        elif isinstance(v, str):
            if v == "":
                return None
            return [Decimal(val) for val in v.split(",")]
        return v

    @validator("dca_spreads", pre=True, always=True)
    def parse_spreads(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            if v == "":
                return []
            return [float(x.strip()) for x in v.split(",")]
        return v

    @validator("dca_amounts", pre=True, always=True)
    def parse_and_validate_amounts(cls, v, values, field):
        if v is None or v == "":
            return [1 for _ in values[values["dca_spreads"]]]
        if isinstance(v, str):
            return [float(x.strip()) for x in v.split(",")]
        elif isinstance(v, list) and len(v) != len(values["dca_spreads"]):
            raise ValueError(f"The number of {field.name} must match the number of {values['dca_spreads']}.")
        return v


class DManMakerV2(MarketMakingControllerBase):
    def __init__(self, config: DManMakerV2Config, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.position_action = PositionAction.OPEN
        self.max_records = config.natr_length * 2
        self.dca_amounts_pct = [Decimal(amount) / sum(self.config.dca_amounts) for amount in self.config.dca_amounts]
        self.spreads = self.config.dca_spreads
        self.long_filled_executors = []
        self.short_filled_executors = []

    def first_level_refresh_condition(self, executor):
        if self.config.top_executor_refresh_time is not None:
            if self.get_level_from_level_id(executor.custom_info["level_id"]) == 0:
                return self.market_data_provider.time() - executor.timestamp > self.config.top_executor_refresh_time
        return False

    def order_level_refresh_condition(self, executor):
        return self.market_data_provider.time() - executor.timestamp > self.config.executor_refresh_time

    def executors_to_refresh(self) -> List[ExecutorAction]:
        executors_to_refresh = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: not x.is_trading
            and x.is_active
            and (self.order_level_refresh_condition(x) or self.first_level_refresh_condition(x)),
        )
        return [
            StopExecutorAction(controller_id=self.config.id, executor_id=executor.id)
            for executor in executors_to_refresh
        ]

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        self.long_filled_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_trading and x.custom_info['filled_amount'] > 0 and x.custom_info['side'] == TradeType.BUY
        )

        self.short_filled_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_trading and x.custom_info['filled_amount'] > 0 and x.custom_info['side'] == TradeType.SELL
        )

        if self.long_filled_executors:
            print(f"Long executors filled: {[(x.custom_info['filled_amount'], x.custom_info['current_position_average_price']) for x in self.long_filled_executors]}")
        if self.short_filled_executors:
            print(f"Short executors filled: {[(x.custom_info['filled_amount'], x.custom_info['current_position_average_price']) for x in self.short_filled_executors]}")

        if self.long_filled_executors and self.short_filled_executors:
            if sum([x.custom_info['filled_amount'] for x in self.long_filled_executors]) == sum([x.custom_info['filled_amount'] for x in self.short_filled_executors]):
                if sum([x.custom_info['current_position_average_price'] * x.custom_info['filled_amount'] for x in self.long_filled_executors]) * Decimal(1 - 0.0005) < sum([x.custom_info['current_position_average_price'] * x.custom_info['filled_amount'] for x in self.short_filled_executors]):
                    print("Cancelling all executors!")
                    return [StopExecutorAction(controller_id=self.config.id, executor_id=executor.id, keep_position=True) for executor in self.long_filled_executors + self.short_filled_executors]
        return []

    async def update_processed_data(self):
        ob_snapshot = self.market_data_provider.get_order_book_snapshot(
            connector_name=self.config.connector_name, trading_pair=self.config.trading_pair
        )
        price_b0 = ob_snapshot[0].price.iloc[0]
        price_a0 = ob_snapshot[1].price.iloc[0]
        volume_b0 = ob_snapshot[0].amount.iloc[0]
        volume_a0 = ob_snapshot[1].amount.iloc[0]
        imbalance = (volume_b0 - volume_a0) / (volume_b0 + volume_a0)
        price_mid = (price_a0 + price_b0) / 2
        tick_size = (price_a0 - price_b0) / price_mid
        fee_rebate = 0.00003
        half_spread = (tick_size + fee_rebate) / 2
        theta = 0.6 * half_spread + 0.4
        price_reference = Decimal(price_mid + theta * 0.5 * (imbalance**3 + imbalance) * (price_a0 - price_b0))
        candles = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100

        long_amount = 0
        short_amount = 0
        for tp, pos_info in self.market_data_provider.get_connector(self.config.connector_name).account_positions.items():
            if tp.startswith(self.config.trading_pair):
                long_amount += pos_info.amount if pos_info.position_side == PositionSide.LONG else 0
                short_amount += pos_info.amount if pos_info.position_side == PositionSide.SHORT else 0
        if long_amount * Decimal(price_mid) > self.config.total_amount_quote and abs(short_amount) * Decimal(price_mid) > self.config.total_amount_quote:
            print("Starting executors to close positions!")
            self.position_action = PositionAction.CLOSE
        else:
            self.position_action = PositionAction.OPEN

        long_filled = sum([x.custom_info['filled_amount'] for x in self.long_filled_executors])
        short_filled = sum([x.custom_info['filled_amount'] for x in self.short_filled_executors])
        diff_filled = (short_filled - long_filled) * Decimal(price_mid) / (self.config.total_amount_quote / 2)
        price_shift = Decimal(natr.iloc[-1]) / 2 * (diff_filled ** 3 + diff_filled) * Decimal(0.5) * (1 + self.config.eagerness)
        print(f"Long filled: {long_filled}, Short filled: {short_filled}, Diff filled: {diff_filled}, Price shift: {price_shift * 100:.2f}%")

        self.processed_data = {
            "reference_price": Decimal(price_reference * (1 + price_shift)),
            "spread_multiplier": Decimal(natr.iloc[-1]),
            "features": candles,
        }

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        if trade_type == TradeType.BUY:
            prices = [price * (1 - spread) for spread in self.spreads]
        else:
            prices = [price * (1 + spread) for spread in self.spreads]
        amounts = [amount * pct for pct in self.dca_amounts_pct]
        amounts_quote = [amount * price for amount, price in zip(amounts, prices)]
        return DCAExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            mode=DCAMode.MAKER,
            position_action=self.position_action,
            side=trade_type,
            prices=prices,
            amounts_quote=amounts_quote,
            level_id=level_id,
            time_limit=self.config.time_limit,
            stop_loss=self.config.stop_loss,
            take_profit=self.config.take_profit,
            trailing_stop=self.config.trailing_stop,
            activation_bounds=self.config.executor_activation_bounds,
            leverage=self.config.leverage,
        )

    def to_format_status(self):
        lines = ['=' * 50]
        for tp, pos_info in self.market_data_provider.get_connector(self.config.connector_name).account_positions.items():
            if tp.startswith(self.config.trading_pair):
                lines.append(f"{self.config.trading_pair} {pos_info.position_side} Amount: {pos_info.amount} Entry Price: {pos_info.entry_price} Unrealized PnL: {pos_info.unrealized_pnl}")
        lines.append('=' * 50)
        return lines
