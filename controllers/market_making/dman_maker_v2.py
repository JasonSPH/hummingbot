from decimal import Decimal
from typing import List, Optional

import pandas_ta as ta  # noqa: F401
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.core.data_type.common import TradeType
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
        default="0.01,0.02,0.04,0.08",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Enter a comma-separated list of spreads for each DCA level: "
        ),
    )
    dca_amounts: List[Decimal] = Field(
        default="0.1,0.2,0.4,0.8",
        client_data=ClientFieldData(
            prompt_on_new=True, prompt=lambda mi: "Enter a comma-separated list of amounts for each DCA level: "
        ),
    )
    time_limit: int = Field(
        default=60 * 60,
        gt=0,
        client_data=ClientFieldData(prompt=lambda mi: "Enter the time limit for each DCA level: ", prompt_on_new=False),
    )
    stop_loss: Decimal = Field(
        default=Decimal("0.03"),
        gt=0,
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the stop loss (as a decimal, e.g., 0.03 for 3%): ", prompt_on_new=True
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
            prompt_on_new=False,
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
            prompt=lambda mi: "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ", prompt_on_new=False
        ),
    )

    macd_fast: int = Field(
        default=12, client_data=ClientFieldData(prompt=lambda mi: "Enter the MACD fast length: ", prompt_on_new=True)
    )
    macd_slow: int = Field(
        default=26, client_data=ClientFieldData(prompt=lambda mi: "Enter the MACD slow length: ", prompt_on_new=True)
    )
    macd_signal: int = Field(
        default=9, client_data=ClientFieldData(prompt=lambda mi: "Enter the MACD signal length: ", prompt_on_new=True)
    )
    natr_length: int = Field(
        default=14, client_data=ClientFieldData(prompt=lambda mi: "Enter the NATR length: ", prompt_on_new=True)
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
        self.max_records = max(config.macd_slow, config.macd_fast, config.macd_signal, config.natr_length) + 100
        self.dca_amounts_pct = [Decimal(amount) / sum(self.config.dca_amounts) for amount in self.config.dca_amounts]
        self.spreads = self.config.dca_spreads

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

    async def update_processed_data(self):
        candles = self.market_data_provider.get_candles_df(
            connector_name=self.config.candles_connector,
            trading_pair=self.config.candles_trading_pair,
            interval=self.config.interval,
            max_records=self.max_records,
        )
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        macd_output = ta.macd(
            candles["close"], fast=self.config.macd_fast, slow=self.config.macd_slow, signal=self.config.macd_signal
        )
        macd = macd_output[f"MACD_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]
        macd_signal = -(macd - macd.mean()) / macd.std()
        macdh = macd_output[f"MACDh_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]
        macdh_signal = macdh.apply(lambda x: 1 if x > 0 else -1)
        max_price_shift = natr / 2
        price_multiplier = ((0.5 * macd_signal + 0.5 * macdh_signal) * max_price_shift).iloc[-1]
        candles["spread_multiplier"] = natr
        candles["reference_price"] = candles["close"] * (1 + price_multiplier)
        self.processed_data = {
            "reference_price": Decimal(candles["reference_price"].iloc[-1]),
            "spread_multiplier": Decimal(candles["spread_multiplier"].iloc[-1]),
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
