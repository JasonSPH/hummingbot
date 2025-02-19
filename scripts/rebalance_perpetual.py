import logging
import numpy as np
import pandas as pd
import time
from typing import Any, List
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.derivative_base import DerivativeBase
from hummingbot.connector.exchange_base import ExchangeBase
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type import common
from hummingbot.core.data_type.common import OrderType, PositionMode
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import BuyOrderCompletedEvent, BuyOrderCreatedEvent, SellOrderCompletedEvent, \
    SellOrderCreatedEvent, MarketOrderFailureEvent, OrderCancelledEvent, OrderFilledEvent
from hummingbot.core.event.events import OrderFilledEvent, OrderType, TradeType
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.strategy.script_strategy_base import Decimal, ScriptStrategyBase, OrderType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class RebalancePerpetualScript(ScriptStrategyBase):
    connector_name = "gate_io_perpetual"
    last_ordered_ts = 0.0
    trading_pair = [
        "DOGE-USDT",
        "XRP-USDT",
    ]

    rb: Dict = {
        "connector_name": connector_name,
        "trading_pair": trading_pair,
        "is_buy": True,
        "threshold": Decimal("0.02"),
        "target_value": Decimal("20"),
        "status": "",
    }

    markets = {rb["connector_name"]: trading_pair}

    buy_interval = 60

    price = {}
    activate_order_id = {}
    asset_value = {}

    position_mode: PositionMode = PositionMode.ONEWAY
    set_leverage_flag = False

    leverage = 3
    max_leverage = 4
    min_leverage = 2

    min_amount = {
        "DOGE-USDT": Decimal("3"),
        "XRP-USDT": Decimal("3"),
    }

    @property
    def connector(self) -> ExchangeBase:
        return self.connectors[self.connector_name]

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.check_and_set_leverage()

    def check_and_set_leverage(self):
        if not self.set_leverage_flag:
            perp_connector = self.connector
            perp_connector.set_position_mode(self.position_mode)
            for trading_pair in self.trading_pair:
                perp_connector.set_leverage(trading_pair, self.leverage)
            self.logger().info(
                f"Set leverage to {self.leverage}x for {perp_connector} on {self.trading_pair}"
            )
            self.set_leverage_flag = True

    def on_tick(self):
        if self.last_ordered_ts < (self.current_timestamp - self.buy_interval):
            if self.rb.get("status") == "":
                self.init_rebalance()
            elif self.rb["status"] == "ACTIVATE":
                try:
                    self.cancel_all_order()
                    time.sleep(1)
                    self.get_balance()
                    time.sleep(1)
                    self.create_order()
                except Exception as e:
                    self.logger().error(f"Error in on_tick: {str(e)}")
            self.last_ordered_ts = self.current_timestamp

    def cancel_all_order(self):
        for exchange in self.connectors.values():
            safe_ensure_future(exchange.cancel_all(timeout_seconds=6))

    def init_rebalance(self):
        print("init_rebalance")
        self.rb["status"] = "ACTIVATE"
        self.market = self.rb["connector_name"]

    def get_balance(self):
        print("get_balance")
        balance = self.get_balance_df()
        self.balance = Decimal(float(balance.loc[balance["Asset"] == "USDT", "Total Balance"]))
        df1 = self.connectors[self.rb["connector_name"]].account_positions
        total_unrealized_pnl = 0
        total_asset_value = 0
        for tp in self.trading_pair:
            position_pair = tp
            unrealized_pnl = 0
            if position_pair in df1:
                amount = Decimal(df1[position_pair].amount)
                unrealized_pnl = Decimal(df1[position_pair].unrealized_pnl)
            else:
                amount = 0
            price = Decimal(self.connectors[self.rb["connector_name"]].get_mid_price(tp))
            self.price[tp] = price
            self.asset_value[tp] = amount * price
            total_asset_value = self.asset_value[tp]
            total_unrealized_pnl += unrealized_pnl

    def create_order(self):
        rb = self.rb.copy()
        for tp in self.asset_value:
            if self.asset_value[tp] > rb["target_value"] * (1 + rb["threshold"]):
                self.sell(
                    rb["connector_name"],
                    tp,
                    max(Decimal(rb["target_value"] / self.price[tp] * rb["threshold"]), self.min_amount[tp]),
                    OrderType.LIMIT_MAKER,
                    self.price[tp] * Decimal("1.001"),
                    common.PositionAction.CLOSE
                )
            elif self.asset_value[tp] <= rb["target_value"] * (1 - rb["threshold"]):
                self.buy(
                    rb["connector_name"],
                    tp,
                    max(Decimal(rb["target_value"] / self.price[tp] * rb["threshold"]), self.min_amount[tp]),
                    OrderType.LIMIT_MAKER,
                    self.price[tp] * Decimal("0.999"),
                    common.PositionAction.OPEN
                )
            else:
                # 双向挂单
                self.sell(
                    rb["connector_name"],
                    tp,
                    max(Decimal(rb["target_value"] / self.price[tp] * rb["threshold"]), self.min_amount[tp]),
                    OrderType.LIMIT_MAKER,
                    self.price[tp] * Decimal("1.005"),
                    common.PositionAction.CLOSE
                )
                self.buy(rb["connector_name"],
                         tp,
                         max(Decimal(rb["target_value"] / self.price[tp] * rb["threshold"]), self.min_amount[tp]),
                         OrderType.LIMIT_MAKER,
                         self.price[tp] * Decimal("0.995"),
                         common.PositionAction.OPEN
                         )

    def get_position_df(self) -> pd.DataFrame:
        columns: List[str] = ["Exchange", "Trading Pair", "Amount", "Entry Price", "Unrealized PnL", "Percentage"]
        data: List[Any] = []
        dc_position = self.connectors[self.connector_name].account_positions
        for trading_pair in dc_position:
            amount = Decimal(dc_position[trading_pair].amount)
            entry_price = Decimal(dc_position[trading_pair].entry_price)
            unrealized_pnl = Decimal(dc_position[trading_pair].unrealized_pnl)
            percentage = round(unrealized_pnl / (abs(amount) * entry_price), 4)
            data.append([self.connector_name, trading_pair, amount, entry_price, unrealized_pnl, percentage])

        df = pd.DataFrame(data, columns=columns)
        df.sort_values(by=["Exchange", "Trading Pair"], inplace=True)
        return df
