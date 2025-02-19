import logging
from hummingbot.strategy.script_strategy_base import Decimal
from typing import Dict
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.event.events import OrderType, BuyOrderCreatedEvent, SellOrderCreatedEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class RebalanceSpotScript(ScriptStrategyBase):
    last_ordered_ts = 0.0

    setting: Dict = {
        "connector_name": "gate_io",
        "trading_pair": "BTC-USDT",
        "threshold": 0.05,
        "target_value": 100,
        "status": "",
        "buy_interval": 10.0,
        "start_price": 0.0,
    }
    markets = {setting["connector_name"]: {setting["trading_pair"]}}
    price = 0

    def on_tick(self):
        if self.last_ordered_ts < (self.current_timestamp - self.setting["buy_interval"]):
            if self.setting.get("status") == "":
                self.setting["status"] = "ACTIVATE"
                base, quote = split_hb_trading_pair(self.setting["trading_pair"])
                self.setting["base"] = base
                self.setting["quote"] = quote
                self.setting["start_price"] = self.connectors[self.setting["connector_name"]].get_mid_price(
                    self.setting["trading_pair"]
                )
            elif self.setting["status"] == "ACTIVATE":
                self.cancel_all_order()
                self.get_balance()
                self.create_order()

            self.last_ordered_ts = self.current_timestamp

    def cancel_all_order(self):
        active_orders = self.get_active_orders(self.setting["connector_name"])
        for order in active_orders:
            self.cancel(self.setting["connector_name"], self.setting["trading_pair"], order.client_order_id)

    def get_balance(self):
        df = self.get_balance_df()
        self.setting["base_asset"] = float(df.loc[df["Asset"] == self.setting["base"], "Total Balance"])
        self.price = float(self.connectors[self.setting["connector_name"]].get_mid_price(self.setting["trading_pair"]))
        self.setting["base_value"] = self.setting["base_asset"] * self.price
        self.setting["quote_asset"] = float(df.loc[df["Asset"] == self.setting["quote"], "Total Balance"])

    def create_order(self):
        setting = self.setting.copy()
        if setting["base_value"] > setting["target_value"] * (1 + setting["threshold"]):
            logging.info(
                f"基础货币价值过高 当前:{setting['base_value']:.2f} 目标:{setting['target_value']:.2f} 差异:{((setting['base_value']/setting['target_value'])-1)*100:.2f}%"
            )
            self.sell(
                setting["connector_name"],
                setting["trading_pair"],
                Decimal(setting["target_value"] / self.price * setting["threshold"]),
                OrderType.LIMIT,
                Decimal(self.price * 1.0001),
            )
        elif setting["base_value"] <= setting["target_value"] * (1 - setting["threshold"]):
            logging.info(
                f"基础货币价值过低 当前:{setting['base_value']:.2f} 目标:{setting['target_value']:.2f} 差异:{((setting['base_value']/setting['target_value'])-1)*100:.2f}%"
            )
            self.buy(
                setting["connector_name"],
                setting["trading_pair"],
                Decimal(setting["target_value"] / self.price * setting["threshold"]),
                OrderType.LIMIT,
                Decimal(self.price * 0.9999),
            )

    def did_create_buy_order(self, event: BuyOrderCreatedEvent):
        self.logger().info(logging.INFO, f"买单创建成功 {event.order_id}")

    def did_create_sell_order(self, event: SellOrderCreatedEvent):
        self.logger().info(logging.INFO, f"卖单创建成功 {event.order_id}")
