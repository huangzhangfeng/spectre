"""
@author: Heerozh (Zhang Jianhao)
@copyright: Copyright 2019, Heerozh. All rights reserved.
@license: Apache 2.0
@email: heeroz@gmail.com
"""
import pandas as pd
import numpy as np
from collections import namedtuple, defaultdict
from .event import *


class Portfolio:
    def __init__(self):
        self._history = pd.DataFrame()
        self._positions = defaultdict(int)
        self._last_price = defaultdict(int)
        self._cash = 0
        self._current_date = None

    @property
    def history(self):
        return self._history.fillna(method='ffill')

    @property
    def positions(self):
        return self._positions

    @property
    def cash(self):
        return self._cash

    @property
    def value(self):
        values = [self._last_price[asset] * amount for asset, amount in self.positions.items()]
        return sum(values) + self._cash

    @property
    def leverage(self):
        values = [self._last_price[asset] * amount for asset, amount in self.positions.items()]
        return sum(np.abs(values)) / (sum(values) + self._cash)

    def __repr__(self):
        return "<Portfolio>:\n" + str(self.history)

    def clear(self):
        self.__init__()

    def set_date(self, date):
        if isinstance(date, str):
            date = pd.Timestamp(date)
        date = date.normalize()
        if self._current_date is not None and date < self._current_date:
            raise ValueError('Cannot set a date less than the current date')

        self._current_date = date

    def update(self, asset, amount):
        assert self._current_date is not None
        self._positions[asset] += amount
        self._history.loc[self._current_date, asset] = self._positions[asset]

    def update_cash(self, amount):
        self._cash += amount
        if self._current_date is not None:
            self._history.loc[self._current_date, 'cash'] = self._cash

    def process_split(self, asset, inverse_ratio: float):
        sp = self._positions[asset] * (inverse_ratio - 1)
        self.update(asset, sp)

    def process_dividends(self, asset, ratio):
        div = self._positions[asset] * ratio
        self.update_cash(div)

    def update_value(self, get_curr_price):
        for asset, shares in self._positions.items():
            price = get_curr_price(asset)
            if price:
                self._last_price[asset] = price
        self._history.loc[self._current_date, 'value'] = self.value


class CommissionModel:
    def __init__(self, percentage: float, per_share: float, minimum: float):
        self.percentage = percentage
        self.per_share = per_share
        self.minimum = minimum

    def calculate(self, price: float, shares: int):
        commission = price * abs(shares) * self.percentage
        commission += abs(shares) * self.per_share
        if commission < self.minimum:
            commission = self.minimum
        return commission


class BaseBlotter:
    """
    Base class for Order Management System.
    """
    max_shares = int(1e+5)

    def __init__(self) -> None:
        super().__init__()
        self._portfolio = Portfolio()
        self._current_dt = None
        self.commission = CommissionModel(0, 0, 0)
        self.slippage = CommissionModel(0, 0, 0)
        self.short_fee = CommissionModel(0, 0, 0)

    @property
    def positions(self):
        return self._portfolio.positions

    @property
    def portfolio(self):
        return self._portfolio

    def set_datetime(self, dt: pd.Timestamp) -> None:
        self._current_dt = dt
        self._portfolio.set_date(dt)

    def set_commission(self, percentage: float, per_share: float, minimum: float):
        """
        <WORK IN BACKTEST ONLY>
        commission is sum of following:
        :param percentage: percentage part, calculated by percentage * price * shares
                           us: 0, china: 0.0005
        :param per_share: calculated by per_share * shares
                          us: 0.005, china: 0.0006
        :param minimum: minimum commission if above does not exceed
                        us: 1, china: 5
        """
        self.commission = CommissionModel(percentage, per_share, minimum)

    def set_slippage(self, percentage: float, per_share: float):
        """
        <WORK IN BACKTEST ONLY>
        market impact add to price, sum of following:
        :param percentage: percentage * price * shares
        :param per_share: per_share * shares
        """
        self.slippage = CommissionModel(percentage, per_share, 0)

    def set_short_fee(self, percentage: float):
        """
        <WORK IN BACKTEST ONLY>
        fee pay for short
        :param percentage: percentage * close_price * shares,  us: 0, china: 0.001
        """
        self.short_fee = CommissionModel(percentage, 0, 0)

    def get_price(self, asset):
        raise NotImplementedError("abstractmethod")

    def _order(self, asset, amount):
        raise NotImplementedError("abstractmethod")

    def order(self, asset: str, amount: int):
        if abs(amount) > self.max_shares:
            raise OverflowError('Cannot order more than ±%d shares'.format(self.max_shares))
        if not isinstance(asset, str):
            raise KeyError("`asset` must be a string")

        return self._order(asset, amount)

    def order_target_percent(self, asset: str, pct: float):
        if not isinstance(asset, str):
            raise KeyError("`asset` must be a string")

        price = self.get_price(asset)
        if price is None:
            raise KeyError("`asset` is not tradable today.")
        amount = int((self._portfolio.value * pct) / price)
        held = self.positions[asset]
        amount -= held
        return self._order(asset, int(amount))

    def cancel_all_orders(self):
        raise NotImplementedError("abstractmethod")

    def process_split(self, asset, ratio):
        self._portfolio.process_split(asset, ratio)

    def process_dividends(self, asset, ratio):
        self._portfolio.process_dividends(asset, ratio)

    def get_history_positions(self):
        return self._portfolio.history

    def get_transactions(self):
        raise NotImplementedError("abstractmethod")


class SimulationBlotter(BaseBlotter, EventReceiver):
    Order = namedtuple("Order", ['date', 'asset', 'amount', 'price', 'final_price', 'commission'])

    def __init__(self, dataloader, cash=100000, daily_curb=None):
        """
        :param dataloader: dataloader for get prices
        :param daily_curb: How many fluctuations to prohibit trading, in return.
        """
        super().__init__()
        self.price_col = None
        self.market_opened = False
        self.dataloader = dataloader
        self.daily_curb = daily_curb
        self.orders = defaultdict(list)
        self._portfolio.update_cash(cash)
        self.prices = dataloader.load(None, None, 0)

    def __repr__(self):
        return self.get_transactions().__repr__()

    def clear(self):
        self.orders = defaultdict(list)
        self._portfolio.clear()

    def set_datetime(self, dt: pd.Timestamp) -> None:
        if self._current_dt is not None:
            # make up events for skipped dates
            for day in pd.bdate_range(self._current_dt, dt):
                if day == self._current_dt or day == dt:
                    continue
                super().set_datetime(day)
                self.market_open()
                self.market_close()

        super().set_datetime(dt)

    def set_price(self, name: str):
        if name == 'open':
            self.price_col = self.dataloader.ohlcv[0]
        else:
            self.price_col = self.dataloader.ohlcv[3]

    def get_price(self, asset: str):
        rowid = (self._current_dt, asset)
        if rowid not in self.prices.index:
            return None
        price = self.prices.loc[rowid, self.price_col]
        return price

    def _order(self, asset, amount):
        if not self.market_opened:
            raise RuntimeError('Out of market hours, or you did not subscribe this class '
                               'with SimulationEventManager')
        if abs(amount) > self.max_shares:
            raise OverflowError('Cannot order more than ±%d shares'.format(self.max_shares))

        if amount == 0:
            return

        # get price and change
        price = self.get_price(asset)
        if price is None:
            raise KeyError("`asset` is not tradable today.")

        if self.daily_curb is not None:
            df = self.prices.loc[(slice(None, self._current_dt), asset), :]
            if df.shape[0] < 2:
                return
            close_col = self.dataloader.ohlcv[3]
            previous_close = df[close_col].iloc[-2]
            change = price / previous_close - 1
            # Detecting whether transactions are possible
            if abs(change) > self.daily_curb:
                return

        # commission, slippage
        commission = self.commission.calculate(price, amount)
        slippage = self.slippage.calculate(price, 1)
        if amount < 0:
            commission += self.short_fee.calculate(price, amount)
            final_price = price - slippage
        else:
            final_price = price + slippage

        # make order
        order = SimulationBlotter.Order(
            self._current_dt, asset, amount, price, final_price, commission)
        self.orders[asset].append(order)

        # update portfolio, pay cash
        self._portfolio.update(asset, amount)
        self._portfolio.update_cash(-amount * final_price - commission)

    def cancel_all_orders(self):
        # don't need
        pass

    def get_transactions(self):
        data = []
        for asset, orders in self.orders.items():
            for o in orders:
                data.append(dict(date=o.date, amount=o.amount, price=o.price, symbol=o.asset,
                                 final_price=o.final_price, commission=o.commission))
        ret = pd.DataFrame(data)
        ret = ret.set_index('date')
        return ret

    def market_open(self):
        self.market_opened = True

    def market_close(self):
        self.cancel_all_orders()
        self.market_opened = False

        # push dividend/split data to portfolio
        if self.dataloader.adjustments is not None:
            div_col = self.dataloader.adjustments[0]
            sp_col = self.dataloader.adjustments[1]
            for asset, shares in self.positions.items():
                if shares == 0:
                    continue
                rowid = (self._current_dt, asset)
                if rowid not in self.prices.index:
                    continue
                row = self.prices.loc[rowid, :]
                if div_col in self.prices.columns:
                    div = row[div_col]
                    if div != np.nan and div != 0:
                        self._portfolio.process_dividends(asset, div)
                if sp_col in self.prices.columns:
                    split = row[sp_col]
                    if split != np.nan and split != 1:
                        self._portfolio.process_split(asset, 1 / split)

        # push close price to portfolio
        self._portfolio.update_value(self.get_price)

    def on_subscribe(self):
        self.schedule(MarketOpen(self.market_open, -100000))  # 100ms ahead for system preparation
        self.schedule(MarketClose(self.market_close))