import asyncio
import os
import multiprocessing
import sys
import time
import logging
import json
import numpy
import utils
import datetime
import random
import traceback
import functools
from queue import Empty, Full
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo
from sdk_manager_async import SDKManager, check_sdk
from dotenv import load_dotenv
from fubon_neo.sdk import Order
from fubon_neo.constant import TimeInForce, OrderType, PriceType, MarketType, BSAction
from concurrent.futures import ThreadPoolExecutor
from itertools import islice


class Strategy(ABC):
    """
    Strategy template class
    """
    __version__ = "2024.0.7"

    def __init__(self, logger=None, log_level=logging.DEBUG):
        # Set logger
        log_shutdown_event = None
        if logger is None:
            current_date = datetime.datetime.now(ZoneInfo("Asia/Taipei")).date().strftime("%Y-%m-%d")
            utils.mk_folder("log")
            logger, log_shutdown_event = utils.get_logger(
                name="Strategy",
                log_file=f"log/strategy_{current_date}.log",
                log_level=log_level
            )

        self.logger = logger
        self.logger_shutdown = log_shutdown_event

        # The sdk_manager
        self.sdk_manager: SDKManager | None = None

        # Coordination
        # self.__is_strategy_run = False

    """
        Public Functions
    """

    def set_sdk_manager(self, sdk_manager: SDKManager):
        self.sdk_manager = sdk_manager
        # self.logger.info(f"The SDKManager version: {self.sdk_manager.__version__}")

    @check_sdk
    def add_realtime_marketdata(self, symbol: str):
        """
         Add a realtime trade data websocket channel
        :param symbol: stock symbol (e.g., "2881")
        """
        self.sdk_manager.subscribe_realtime_trades(symbol)

    @check_sdk
    def remove_realtime_marketdata(self, symbol: str):
        """
        Remove a realtime market data websocket channel
        :param symbol: stock symbol (e.g., "2881")
        """
        self.sdk_manager.unsubscribe_realtime_trades(symbol)

    @abstractmethod
    @check_sdk
    def run(self):
        """
        Strategy logic to be implemented.
        """
        raise NotImplementedError("Subclasses must implement this method")


class TradingHeroAlpha(Strategy):
    __version__ = "2024.15.1"
    __strategy_code__ = "cdl"
    __zeta__ = 8.6

    def position_algo(self, previous_close: float) -> int:
        if self.__strategy_code__ == "cdl":
            return min(5, max(round(2 * (80 / previous_close)), 1))
        else:
            return min(5, max(round(2 * (120 / previous_close)), 1))

    def __init__(self, the_queue: multiprocessing.Queue, logger=None, log_level=logging.DEBUG):
        super().__init__(logger=logger, log_level=log_level)

        # Info
        self.logger.info(f"Strategy version: {self.__version__}")
        self.logger.info(f"Is cdl? {self.__strategy_code__ == "cdl"}")

        # Multiprocessing queue
        self.queue: multiprocessing.Queue = the_queue

        # Setup target symbols
        self.__symbols = ['00887', '6598', '3710', '6651', '3540', '6754', '3592', '4105', '6456', '6591', '3325', '6261', '3563', '3694', '4939', '4129', '3083', '6573', '8183', '8155']  # 輸入股票代碼

        self.__symbols_task_done = []

        # Price data
        self.__lastday_close = {}
        self.__open_price_today = {}
        self.__ws_message_queue = asyncio.Queue()

        self.__trail_stop_profit_cutoff: dict[str, float] | None = None
        self.__max_price_seen: dict[str, float] | None = None
        self.__past_prices_seen: dict[str, list[float]] | None = None
        self.__average_price: dict[str, float] | None = None

        # Order coordinators
        self.__position_info = {}
        self.__is_reload = False  # If the program start with reload mode
        utils.mk_folder("position_info_store")
        self.__pos_info_file_path = \
            f"position_info_store/position_info_{datetime.datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d')}.json"
        # Check if the file exists
        if os.path.exists(self.__pos_info_file_path):
            # Load the dictionary from the JSON file
            with open(self.__pos_info_file_path, 'r') as f:
                self.__position_info = json.load(f)
            self.logger.info(f"Loaded position_info from file:\n {self.__position_info}")
            self.__is_reload = True

        self.__open_order_placed = {}
        self.__closure_order_placed = {}

        self.__on_going_orders = {}  # {symbol -> [order_no]}
        self.__on_going_orders_details: dict[str, dict[str, float]] = {}
        self.__order_type_enter = {}
        self.__order_type_exit = {}
        self.__on_going_orders_lock: dict[str, asyncio.Lock] | None = None
        self.__max_latest_price_timestamp = 0 if not self.__is_reload else time.time()

        self.__failed_order_count = {}
        self.__failed_exit_order_count = {}
        self.__clean_list = []

        self.__suspend_entering_symbols = []

        for s in self.__symbols:
            self.__on_going_orders[s] = []
            self.__order_type_enter[s] = []
            self.__order_type_exit[s] = []

        # Position sizing
        self.__fund_available = 420000  # 總下單額度控管
        # self.__enter_lot_limit = 3  # 單一商品總下單張數上限
        self.__enter_lot_limit: dict[str, int] = {}
        self.__max_lot_per_round = 2 # min(2, self.__enter_lot_limit)  # Maximum number of round to send order non-stopping
        self.__fund_available_update_lock = asyncio.Lock()
        self.__active_target_list: list[str] = []
        self.__not_in_active_target_list_notified: list[str] = []
        self.__is_all_symbol_included: bool = False
        self.logger.info(f"初始可用額度: {self.__fund_available} TWD")
        #self.logger.info(f"個股下單上限: {self.__enter_lot_limit} 張")

        # Strategy checkpoint
        minute_digit_offset = [-2, -1, 0, 1, 2]
        second_digit_offset = [*range(-25, 26, 1)]
        self.__strategy_exit_time = datetime.time(13, int(18 + random.choice(minute_digit_offset)),
                                                  int(30 + random.choice(second_digit_offset)))
        self.__strategy_enter_cutoff_time = datetime.time(9, 45)
        self.__market_close_time = datetime.time(13, 32)
        self.__is_market_close_time_passed = False  # Flag for using self.__price_data_queue_handler()

        self.__program_start_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        # ThreadPoolExecutor
        self.__threadpool_executor = ThreadPoolExecutor()

        # The async loops
        self.__event_loop: asyncio.events.AbstractEventLoop | None = None

    def __flush_position_info(self):
        # Send message to the multiprocessing queue
        message = {
            "event": "position_info",
            "data": self.__position_info
        }
        try:
            self.queue.put_nowait(message)
        except Full:
            self.logger.warning("Supervisor's queue is full ...")

    def __self_restart(self):
        message = {
            "event": "restart",
            "data": None
        }
        self.queue.put(message)

    async def __heartbeat_task(self):
        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        while now_time < self.__market_close_time:
            self.__emit_heartbeat()

            price_waiting_time = time.time() - self.__max_latest_price_timestamp / 1000000
            if (price_waiting_time > 300) and \
                    (self.__position_info or
                     (datetime.time(8, 30) < now_time < datetime.time(9, 0))):
                message = {
                    "event": "warning",
                    "data": price_waiting_time
                }
                try:
                    self.queue.put_nowait(message)
                except Full:
                    self.logger.warning("Supervisor's queue is full ...")

            # Terminate early if all tasks done
            if self.__symbols_task_done and \
                    set(self.__symbols_task_done) == set(self.__symbols):
                self.__market_close_time = now_time
                self.logger.info(f"All tasks done, exit early ...")

            # Prepare the next run
            await asyncio.sleep(5)
            now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        # Update the flag
        self.__is_market_close_time_passed = True

    def __emit_heartbeat(self):
        # Update the multiprocessing queue
        message = {
            "event": "strategy_heartbeat",
            "data": time.time()
        }
        try:
            self.queue.put_nowait(message)
        except Full:
            self.logger.warning("Supervisor's queue is full ...")

    async def __place_order(self, order):
        place_order_start_time = time.time()

        # Create a partial function with unblock set to False
        place_order_partial = functools.partial(
            self.sdk_manager.sdk.stock.place_order,
            self.sdk_manager.active_account,
            order,
            unblock=False
        )

        # Run in executor
        response = await self.__event_loop.run_in_executor(
            self.__threadpool_executor,
            place_order_partial,
        )

        place_order_end_time = time.time()

        return response, place_order_start_time, place_order_end_time

    @check_sdk
    def run(self):
        # Startup async event loops
        asyncio.run(self.__async_run())

        # Finishing ...
        self.__threadpool_executor.shutdown(wait=False, cancel_futures=True)
        self.__event_loop.close()
        self.logger_shutdown.set()

    async def __async_run(self):
        # Initialize the event loop
        self.__event_loop = asyncio.get_event_loop()
        self.__event_loop.set_task_factory(asyncio.eager_task_factory)

        # Start the heartbeat
        heartbeat_task = self.__event_loop.create_task(self.__heartbeat_task())

        # Remove symbols that can do day-trade short sell
        self.logger.debug("Strategy.run - Cleaning symbol list ...")

        if self.__is_reload:  # remove all symbols that are not in the position_info
            candidate_symbols = self.__symbols.copy()
            self.__symbols = []

            for symbol in candidate_symbols:
                if symbol in self.__position_info:
                    self.__symbols.append(symbol)

            self.logger.debug(f"Reload - refined symbols: {self.__symbols}, position info: {self.__position_info}")

        elif self.sdk_manager.sdk_version >= (1, 3, 1):
            candidate_symbols = self.__symbols.copy()
            self.__symbols = []

            for symbol in candidate_symbols:
                query = self.sdk_manager.sdk.stock.daytrade_and_stock_info(self.sdk_manager.active_account, symbol)
                retry_attempt_count = 0
                is_success = False

                while not is_success:
                    if retry_attempt_count >= 2:
                        self.logger.info(
                            f"Failed to retrieve day-trade info for {symbol} too many time, skip this target")
                        break  # Proceed to the next symbol

                    elif not query.is_success:
                        self.logger.debug(f"Fail retrieving day-trade info for {symbol}, retry later ...")
                        retry_attempt_count += 1

                    else:
                        try:
                            if (query.data.status is not None) and (int(query.data.status) >= 8):
                                self.logger.debug(f"{symbol}'s status {query.data.status}, add to the list")
                                self.__symbols.append(symbol)
                            else:
                                self.logger.info(
                                    f"{symbol}'s day-trade status: {query.data.status}, remove from the list")

                            is_success = True
                            break  # Proceed to the next symbol

                        except Exception as err:
                            self.logger.error(f"{symbol} - query {query}")
                            self.logger.error(
                                f"Daytrade info query exception {err}, traceback {traceback.format_exc()}," +
                                f" retry ...")
                            retry_attempt_count += 1

                    # Wait and retry
                    await asyncio.sleep(0.2)
                    query = self.sdk_manager.sdk.stock.daytrade_and_stock_info(self.sdk_manager.active_account, symbol)

            self.logger.debug(f"Original symbol list (length {len(candidate_symbols)}):\n {candidate_symbols}")
            self.logger.info(f"Refined symbol list (length {len(self.__symbols)}):\n {self.__symbols}")

        else:
            self.logger.debug(
                f"SDK version is less than 1.3.1, current {self.sdk_manager.sdk_version}, ignore symbol cleaning ...")

        # Get stock's last day close price
        rest_stock = self.sdk_manager.sdk.marketdata.rest_client.stock

        self.logger.info("Strategy.run - Getting stock close price of the last trading day ...")
        for s in self.__symbols:
            try:
                is_task_success = False
                response = {}

                while not is_task_success:
                    response = rest_stock.intraday.quote(symbol=s)
                    if "status" in response and "429" in response:
                        self.logger.info(f"statusCode 429, wait and try again ...")
                        await asyncio.sleep(60)
                    elif datetime.datetime.strptime(response["date"], "%Y-%m-%d").date() != \
                            datetime.datetime.now(ZoneInfo("Asia/Taipei")).date():
                        self.logger.info(f"Date {response["date"]} is not today, wait and try again ...")
                        # self.logger.debug(f"data:\n{response}")
                        await asyncio.sleep(60)
                    else:
                        is_task_success = True

                self.__lastday_close[s] = float(response["previousClose"])
                self.logger.debug(f"symbol: {s}, previous_close: {self.__lastday_close[s]}")

            except Exception as er:
                self.logger.error(f"{s} get the last day close error: {er}, traceback\n{traceback.format_exc()}")
                self.__lastday_close[s] = 9999999999

            await asyncio.sleep(0.1)

        # Calculate lot limit
        for symbol in self.__symbols:
            self.__enter_lot_limit[symbol] = self.position_algo(self.__lastday_close[symbol])
            self.logger.debug(f"Symbol {symbol}'s lot limit: {self.__enter_lot_limit[symbol]}")

        # Set callback functions
        self.logger.debug("Strategy.run - Set callback functions ...")
        self.sdk_manager.set_trade_handle_func("on_filled", self.__order_filled_processor)
        self.sdk_manager.set_ws_handle_func("message", self.__price_data_callback)

        self.logger.info("Strategy.run - Initialize price data processing variables ...")

        self.__on_going_orders_lock = {symbol: asyncio.Lock() for symbol in self.__symbols}
        self.__trail_stop_profit_cutoff = {symbol: -999 for symbol in self.__symbols}
        self.__max_price_seen = {symbol: 0 for symbol in self.__symbols}
        self.__past_prices_seen = {symbol: [] for symbol in self.__symbols}
        self.__average_price = {symbol: 0 for symbol in self.__symbols}

        # Await
        await asyncio.sleep(0)

        self.logger.debug("Strategy.run - Subscribing realtime market datafeed ...")

        # Subscribe realtime marketdata
        futures = [
            self.__event_loop.run_in_executor(
                self.__threadpool_executor,
                self.add_realtime_marketdata,
                symbol
            )
            for symbol in self.__symbols
        ]

        # Await all futures
        await asyncio.gather(*futures)

        self.logger.debug("Strategy.run - Market data preparation has completed.")

        # Order update agents
        t = self.__event_loop.create_task(self.__order_status_updater())

        # Position sizing agent
        tps = self.__event_loop.create_task(self.__position_sizing_agent())

        await self.__position_closure_executor()  # Position closure agent
        await tps
        await t
        await heartbeat_task

        self.logger.debug(f"async run finished ...")

    async def __add_to_active_list(self, add_count: int, delay: float=0):
        if delay > 0:
            await asyncio.sleep(delay)

        if not self.__is_all_symbol_included:
            symbols_to_add = (symbol for symbol in self.__symbols if symbol not in self.__active_target_list)
            limited_symbols = islice(symbols_to_add, add_count)
            self.__active_target_list.extend(limited_symbols)

            if len(self.__active_target_list) == len(self.__symbols):
                self.__is_all_symbol_included = True

    async def __position_sizing_agent(self):
        """
            Add x more symbols every y second after market open
        """
        x = 3
        y = 5

        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        while now_time < datetime.time(8, 59, 50):
            await asyncio.sleep(1)
            now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        self.logger.debug(f"開始啟動加入進場標的 (time {now_time}) ...")

        while now_time < datetime.time(9, 0, 15):
            if len(self.__active_target_list) >= 6: #== len(self.__symbols):
                break

            await self.__add_to_active_list(x)
            await asyncio.sleep(y)

            now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        # All symbol included
        # self.__active_target_list = self.__symbols
        # self.__is_all_symbol_included = True
        # self.logger.debug(f"全進場標的列表 (time {now_time}): {self.__active_target_list}")

    async def __order_status_updater(self):
        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()
        self.logger.info(f"Start __order_status_updater")

        while now_time < self.__market_close_time:
            # Check if anything to check
            if not all(len(lst) == 0 for lst in self.__on_going_orders.values()):
                # Get order results
                try:
                    the_account = self.sdk_manager.active_account

                    response = await self.__event_loop.run_in_executor(
                        self.__threadpool_executor,
                        self.sdk_manager.sdk.stock.get_order_results,
                        the_account
                    )

                    if response.is_success:
                        data = response.data

                        for d in data:
                            try:
                                order_no = str(d.order_no)
                                symbol = str(d.stock_no)
                                status = int(d.status)
                                marker = str(d.user_def)

                                if ("hvl" in marker) and (status != 10) and (status != 50):
                                    if not self.__on_going_orders_lock[symbol].locked():
                                        async with self.__on_going_orders_lock[symbol]:
                                            try:
                                                self.__on_going_orders[symbol].remove(order_no)
                                                self.logger.debug(
                                                    f"on_going_orders updated (order updater): symbol {symbol}, " +
                                                    f"order_no {order_no}"
                                                )

                                            finally:
                                                continue

                            except Exception as err:
                                self.logger.debug(f"__order_status_updater error (inner loop) - {err}")
                            finally:
                                continue

                    else:
                        self.logger.debug(f"__order_status_updater retrieve order results failed, " +
                                          f"message {response.message}")

                except Exception as err:
                    self.logger.debug(f"__order_status_updater error - {err}")

            # sleep
            await asyncio.sleep(5)

            # Update the time
            now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

    async def __position_closure_executor_symbol(self, symbol: str):
        try:
            async with self.__on_going_orders_lock[symbol]:
                # DayTrade 全部出場
                if int(self.__position_info[symbol]["size"]) >= 1000 and \
                        (symbol not in self.__closure_order_placed) and \
                        (not self.__on_going_orders[symbol]):
                    self.logger.info(f"{symbol} 時間出場條件成立 ...")

                    qty = int(self.__position_info[symbol]["size"])

                    self.logger.debug(f"{symbol} 出場股數 {qty}")

                    # Emit heartbeat
                    self.__emit_heartbeat()

                    # Trading
                    while qty >= 1000:
                        # Send order
                        order = Order(
                            buy_sell=BSAction.Buy,
                            symbol=symbol,
                            price=None,
                            quantity=1000,
                            market_type=MarketType.Common,
                            price_type=PriceType.Market,
                            time_in_force=TimeInForce.ROD,
                            order_type=OrderType.Stock,
                            user_def="hvl_close",
                        )

                        response, place_order_start_time, place_order_end_time = await self.__place_order(order)

                        if response.is_success:
                            self.logger.info(f"{symbol} 時間出場下單成功, size {qty}")
                            self.logger.debug(
                                f"速度 {1000 * (place_order_end_time - place_order_start_time):.6f} ms, " +
                                f"非由行情觸發. Data:\n{response.data}")

                            self.__closure_order_placed[symbol] = True

                            # Update order_type_exit
                            if symbol in self.__order_type_exit:
                                self.__order_type_exit[symbol].append(response.data.order_no)
                            else:
                                self.__order_type_exit[symbol] = [response.data.order_no]

                            # Update on_going_orders list
                            if symbol in self.__on_going_orders:
                                self.__on_going_orders[symbol].append(response.data.order_no)
                            else:
                                self.__on_going_orders[symbol] = [response.data.order_no]

                            order_no = response.data.order_no
                            if order_no not in self.__on_going_orders_details:
                                self.__on_going_orders_details[order_no] = {"ordered":0, "filled":0}
                            self.__on_going_orders_details[order_no]["ordered"] += float(response.data.after_qty)

                            self.logger.debug(
                                f"on_going_orders updated (closure): symbol {symbol}, " +
                                f"order_no {response.data.order_no}, " +
                                f"order details: {self.__on_going_orders_details[order_no]}"
                            )

                            # Update qty
                            qty -= 1000
                            self.logger.debug(f"{symbol} 剩餘出場股數 {qty}")

                            # Pause
                            sec_to_sleep = random.randint(5, 10)
                            await asyncio.sleep(sec_to_sleep)

                        else:
                            self.logger.warning(
                                f"{symbol} 時間出場下單失敗, size {qty}, msg: {response.message}")

                            # OS error handling
                            if ("Broken pipe" in response.message) or ("os error" in response.message):
                                raise OSError

                            break

                        # Await
                        await asyncio.sleep(0)

                else:
                    self.logger.debug(f"時間出場條件\"未\"成立 ...")
                    self.logger.debug(f"(Closure session) symbol {symbol}")
                    self.logger.debug(f"(Closure session) position info: {self.__position_info}")
                    self.logger.debug(
                        f"(Closure session) closure order placed keys: {self.__closure_order_placed.keys()}")
                    self.__clean_list.append(symbol)

        except OSError as err:
            self.logger.error(f"OS Error {err} while sending orders, restart the strategy subprocess ...")
            self.logger.debug(f"\ttraceback:\n{traceback.format_exc()}")
            self.__self_restart()

        except Exception as err:
            self.logger.error(f"__realtime_price_data_processor, error: {err}")
            self.logger.debug(f"\ttraceback:\n{traceback.format_exc()}")

    async def __position_closure_executor(self):
        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        while now_time < self.__market_close_time:
            try:
                if now_time > self.__strategy_exit_time:
                    self.__clean_list = []

                    symbols_to_check = set(self.__position_info.keys()).copy()  # Symbols for this round of iteration

                    futures = [
                        self.__position_closure_executor_symbol(symbol)
                        for symbol in symbols_to_check
                    ]

                    # Await all futures
                    await asyncio.gather(*futures)

                    # Execute position info cleaning
                    for symbol in self.__clean_list:
                        try:
                            del self.__position_info[symbol]
                        finally:
                            self.__event_loop.run_in_executor(
                                self.__threadpool_executor,
                                self.remove_realtime_marketdata,
                                symbol
                            )

                            # Add the symbol to the task done list
                            self.__symbols_task_done.append(symbol)

                    # self.__flush_position_info() is not necessary here
                    self.__clean_list = []

            except Exception as err:
                self.logger.debug(f"時間出場迴圈錯誤: {err}, traceback: {traceback.format_exc()}")

            finally:
                # sleep
                await asyncio.sleep(1)

                # Update the time
                now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

        self.logger.debug(f"Position closure executor completed.")
        self.__symbols_task_done = self.__symbols

    def __price_data_callback(self, data):
        def add_item(my_list, new_item):
            my_list.append(new_item)
            if len(my_list) > 20:
                my_list.pop(0)

        try:
            # Update the newest timestamp over all symbols
            timestamp = int(data["time"])
            if timestamp > self.__max_latest_price_timestamp:
                self.__max_latest_price_timestamp = timestamp

            # Determine if pass to further processing
            is_continuous = True if "isContinuous" in data else False
            is_open = True if "isOpen" in data else False

            pass_data_flag = False
            symbol = data["symbol"]

            if is_open or is_continuous:
                try:
                    baseline_price = float(data["price"]) if "price" in data else float(data["bid"])
                    if baseline_price > self.__max_price_seen[symbol]:
                        self.__max_price_seen[symbol] = baseline_price

                    # Update past prices seen
                    add_item(self.__past_prices_seen[symbol], baseline_price)

                    # Update the average price
                    if len(self.__past_prices_seen[symbol]) >= 1:
                        self.__average_price[symbol] = \
                            numpy.mean(self.__past_prices_seen[symbol])

                except (KeyError, ValueError) as err:
                    if is_open:
                        self.logger.info(f"Market open without price! Symbol: {symbol}, data: {data}")
                    else:
                        self.logger.debug(f"__price_data_callback exception: {err}, " +
                                          f"traceback:\n{traceback.format_exc()}\n, data:\n{data}")

                    return

                if is_open or (symbol not in self.__open_price_today):
                    pass_data_flag = True
                elif is_continuous:
                    stop_condition_zeta = False
                    if symbol in self.__position_info:
                        # Calculate pre-screen variables
                        ask_price = float(data["ask"]) if ("ask" in data and float(data["ask"]) > 0) else float(
                            data["price"])  # Add if for robustness
                        gap_until_now_pct = 100 * (ask_price - self.__lastday_close[symbol]) / self.__lastday_close[
                            symbol]
                        stop_condition_zeta = (gap_until_now_pct > self.__zeta__)
                        if stop_condition_zeta:
                            self.logger.debug(f"Zeta! ({symbol}:{gap_until_now_pct:.3f} %): {data}")

                    if self.__is_reload:
                        pass_data_flag = stop_condition_zeta or (not self.__on_going_orders_lock[symbol].locked())
                    else:
                        pass_data_flag = stop_condition_zeta or \
                                         ((not self.__on_going_orders_lock[symbol].locked()) and
                                          self.__average_price[symbol] > 0)

                # Pass the message for further processing (or not)
                if pass_data_flag:
                    asyncio.run_coroutine_threadsafe(
                        self.__realtime_price_data_processor(data),
                        loop=self.__event_loop
                    )

        except Exception as err:
            self.logger.debug(f"__price_data_callback exception: {err}, " +
                              f"traceback:\n{traceback.format_exc()}\n, data:\n{data}")

    async def __realtime_price_data_processor(self, data):
        def order_success_routine(symbol:str, response, order_type):
            try:
                if order_type == "enter":
                    order_type_list = self.__order_type_enter
                elif order_type == "stop":
                    order_type_list = self.__order_type_exit
                else:
                    raise ValueError

                # Update the order_type_exit list
                if symbol in order_type_list:
                    order_type_list[symbol].append(response.data.order_no)
                else:
                    order_type_list[symbol] = [response.data.order_no]

                # Update on_going_orders list
                if symbol in self.__on_going_orders:
                    self.__on_going_orders[symbol].append(response.data.order_no)
                else:
                    self.__on_going_orders[symbol] = [response.data.order_no]

                order_no = response.data.order_no
                if order_no not in self.__on_going_orders_details:
                    self.__on_going_orders_details[order_no] = {"ordered":0, "filled":0}
                self.__on_going_orders_details[order_no]["ordered"] += float(response.data.after_qty)

                # For 交易所價格穩定機制 0051
                status = int(response.data.status)
                if status == 90:
                    self.__on_going_orders_details[order_no]["ordered"] = 0
                    self.logger.debug(f"Error code 0051, set ordered to 0, " +
                                      f"self.__on_going_orders_details:\n{self.__on_going_orders_details},\n" +
                                      f"response:\n{response}")

                self.logger.debug(
                    f"on_going_orders updated ({order_type}): symbol {symbol}, " +
                    f"order_no {response.data.order_no}, " +
                    f"order_details: {self.__on_going_orders_details[order_no]}"
                )
            except Exception as er:
                self.logger.error(f"exit_order_success_routine failed! Exception: {er}, " +
                                  f"traceback: {traceback.format_exc()}")

        def is_sweet_range(change_pct: float) -> bool:
            if self.__strategy_code__ == "cdl":
                return 1 < change_pct < 4.5
            else:
                return -5 < change_pct < 3

        def remove_symbol_at_entry_stage_routine(symbol: str):
            self.__event_loop.run_in_executor(
                self.__threadpool_executor,
                self.remove_realtime_marketdata,
                symbol
            )
            self.__symbols_task_done.append(symbol)

            # Add one more symbol to the active list for the replacement
            asyncio.ensure_future(self.__add_to_active_list(1), loop=self.__event_loop)

        def open_check(symbol: str, is_open_flag: bool) -> (bool, bool):
            gap_change_pct = 100 * (self.__open_price_today[symbol] - self.__lastday_close[symbol]) \
                             / self.__lastday_close[symbol]

            pass_the_check = True

            if not is_sweet_range(gap_change_pct):  # Do not trade this target today
                self.logger.info(f"{symbol} 開盤漲幅超過區間 (實際漲幅: {gap_change_pct:.2f} %)，移除標的" +
                                 f"lastday_close: {self.__lastday_close[symbol]}, " +
                                 f"open_price_today: {self.__open_price_today[symbol]}")
                self.__open_order_placed[symbol] = 99999

                # Execute the routine
                remove_symbol_at_entry_stage_routine(symbol)

                # Alter the pass flag
                pass_the_check = False

            else:
                self.logger.info(f"{symbol} 開盤漲幅符合區間 (實際漲幅: {gap_change_pct:.2f} %). " +
                                 f"lastday_close: {self.__lastday_close[symbol]}, " +
                                 f"open_price_today: {self.__open_price_today[symbol]}")

                is_open_flag = True  # When this case happens, see as if it is the open tick (i.e., de facto)

            return pass_the_check, is_open_flag

        # ########################
        #        Main body
        # ########################
        # Trading logic
        initialization_time = time.time()
        symbol = None
        is_locked = False

        try:
            # Proceed to the trading logic
            symbol = data["symbol"]
            is_continuous = True if "isContinuous" in data else False
            is_open = True if "isOpen" in data else False

            if is_open:
                self.logger.info(f"Market open {symbol}: {data}")

            # Cutout stock that does not fit the open price goal
            if (symbol not in self.__open_price_today) and \
                    (is_open or is_continuous) and \
                    ("price" in data) and (not self.__is_reload):

                if is_open or (self.__program_start_time < datetime.time(9, 0, 0)):
                    self.__open_price_today[symbol] = float(data["price"])
                    is_pass, is_open = open_check(symbol, is_open)  # Override is_open

                    if not is_pass:
                        return

                else:   # The program started after 9AM, and missed the open tick
                    try:
                        # Get the open price by the web API
                        quote = self.sdk_manager.sdk.marketdata.rest_client.stock.intraday.quote(symbol=symbol)
                        open_price = quote["openPrice"]

                        self.__open_price_today[symbol] = float(open_price)
                        is_pass, _ = open_check(symbol, is_open)  # Don't override is_open

                        if not is_pass:
                            return

                    except Exception as err:
                        self.logger.error(f"Retrieve quote for {symbol} failed. " +
                                          f"Exception: {err},\n{traceback.format_exc()}")

                        # Remove the symbol and skip
                        self.__open_order_placed[symbol] = 99999
                        remove_symbol_at_entry_stage_routine(symbol)
                        return

            # Start trading logic =============
            order_lock_checkpoint_start = time.time()
            await self.__on_going_orders_lock[symbol].acquire()
            order_lock_checkpoint_end = time.time()

            is_locked = True

            # Start processing this tick
            now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

            # 開盤動作
            if (now_time < self.__strategy_enter_cutoff_time) and \
                    (not self.__is_reload) and \
                    (symbol not in self.__suspend_entering_symbols) and \
                    (symbol not in self.__open_order_placed or
                     self.__open_order_placed[symbol] < self.__enter_lot_limit[symbol]):

                if symbol not in self.__active_target_list:
                    if symbol not in self.__not_in_active_target_list_notified:
                        self.logger.info(f"{symbol} 尚未納入進場列表: {self.__active_target_list}")
                        self.__not_in_active_target_list_notified.append(symbol)

                elif ("bid" not in data) or (float(data["bid"]) == 0):
                    if "isLimitUpBid" not in data:  # Do not need to output log, limit up has been reached
                        self.logger.debug(f"{symbol} 進場判斷邏輯讀無 bid 價格, data:\n{data}")

                else:
                    # Calculate price change
                    baseline_price = float(data["bid"])
                    matched_price = float(data["price"])

                    price_change_pct_bid = 100 * (baseline_price - self.__lastday_close[symbol]) / \
                                           self.__lastday_close[symbol]

                    # Check for entering condition
                    quantity_to_bid = 0
                    pre_allocate_fund = 0
                    fund_lock_checkpoint_start = fund_lock_checkpoint_end = 0  # init the timer variables

                    if is_sweet_range(price_change_pct_bid) and \
                            (
                                    (
                                        (matched_price < self.__max_price_seen[symbol]) and
                                        (matched_price < self.__average_price[symbol])
                                    ) or
                                    is_open
                            ):
                        fund_lock_checkpoint_start = time.time()
                        async with self.__fund_available_update_lock:
                            fund_lock_checkpoint_end = time.time()
                            max_share_possible = \
                                int((self.__fund_available / float(data["bid"])) / 1000) * 1000

                            # Calculate quantity to bid
                            if max_share_possible < 1000:
                                if is_open:
                                    self.logger.info(f"剩餘可用額度不足, symbol {symbol}, price {data['bid']}, " +
                                                     f"fund_available {self.__fund_available}")
                                    self.logger.info(f"{symbol} 稍後再確認進場訊號 ...")

                            else:
                                quantity_to_bid = int(self.__enter_lot_limit[symbol] * 1000)
                                if symbol in self.__open_order_placed:
                                    quantity_to_bid -= self.__open_order_placed[symbol] * 1000

                                self.logger.debug(f"{symbol} 剩餘下單量 {quantity_to_bid} 股")

                                if max_share_possible < quantity_to_bid:
                                    self.logger.debug(f"剩餘額動不足進場 {symbol}: {quantity_to_bid} 股," +
                                                      f" 調整下單量至 1 張")
                                    quantity_to_bid = 1000

                                # Pre-allocate fund
                                scale_factor = min(quantity_to_bid, self.__max_lot_per_round * 1000)
                                pre_allocate_fund = scale_factor * float(baseline_price)
                                self.__fund_available -= pre_allocate_fund

                                self.logger.info(f"{symbol} 預留下單額度更新: {pre_allocate_fund}")
                                self.logger.info(f"可用額度更新: {self.__fund_available}")

                    elif is_open:
                        self.logger.debug(f"{symbol} 價格進場條件不符合, price change: {price_change_pct_bid} %, " +
                                          f"matched_price: {matched_price}, " +
                                          f"max_price: {self.__max_price_seen[symbol]}, " +
                                          f"average_price: {self.__average_price[symbol]}, " +
                                          f"baseline_price: {baseline_price}, is_open: {is_open}")

                    # 進場操作
                    if quantity_to_bid >= 1000:
                        self.logger.info(f"{symbol} 進場條件成立 ...")

                        quantity_has_bid = 0  # Record how much has already been bid this time

                        while quantity_to_bid >= 1000:
                            order = Order(
                                buy_sell=BSAction.Sell,
                                symbol=symbol,
                                price=None,
                                quantity=1000,
                                market_type=MarketType.Common,
                                price_type=PriceType.Market,
                                time_in_force=TimeInForce.IOC,
                                order_type=OrderType.DayTrade,
                                user_def="hvl_enter",
                            )

                            response, place_order_start_time, place_order_end_time = await self.__place_order(order)

                            if response.is_success:
                                self.logger.debug(f"{symbol} 下單成功! " + f"成功進場委託單直回:\n{response}")
                                self.logger.debug(
                                    f"Order lock checkpoint: {1000 * (order_lock_checkpoint_end - order_lock_checkpoint_start):.6f} ms, " +
                                    f"Fund lock checkpoint: {1000 * (fund_lock_checkpoint_end - fund_lock_checkpoint_start):.6f} ms, " +
                                    f"啟動洗價等待時間 {1000 * (initialization_time - data['ws_received_time']):.6f} ms, " +
                                    f"下單前置 {1000 * (place_order_start_time - data['ws_received_time']):.6f} ms, " +
                                    f"速度 {1000 * (place_order_end_time - place_order_start_time):.6f} ms, " +
                                    f"觸發行情資料:\n {data}"
                                )

                                # Update the order record
                                if symbol in self.__open_order_placed:
                                    self.__open_order_placed[symbol] += 1
                                else:  # The first time to buy this product
                                    self.__open_order_placed[symbol] = 1

                                    # Add one more symbol to the active list for the replacement
                                    asyncio.ensure_future(self.__add_to_active_list(1, delay=10),
                                                          loop=self.__event_loop)

                                self.logger.info(
                                    f"{symbol} 進場下單成功, 總進場張數 {self.__open_order_placed[symbol]}")

                                # Update pre-allocated fund
                                pre_allocate_fund -= 1000 * float(baseline_price)
                                self.logger.info(f"{symbol} 預留下單額度更新: {pre_allocate_fund}")

                                # Execute the routine after a success order
                                order_success_routine(symbol, response, "enter")

                                # Update remain quantity_to_bid
                                quantity_to_bid -= 1000
                                quantity_has_bid += 1000

                                self.logger.debug(f"{symbol} 已下單 {quantity_has_bid} 股，" +
                                                  f"剩餘下單量 {quantity_to_bid} 股")

                                # Bid max shares per round
                                if quantity_has_bid >= self.__max_lot_per_round * 1000:
                                    break

                            else:
                                self.logger.debug(f"失敗進場委託單直回:\n{response}\n觸發行情:\n{data}")

                                # OS error handling
                                if ("Broken pipe" in response.message) or ("os error" in response.message):
                                    raise OSError

                                if symbol in self.__failed_order_count:
                                    self.__failed_order_count[symbol] += 1
                                else:
                                    self.__failed_order_count[symbol] = 1

                                if (self.__failed_order_count[symbol] >= 2) or ("不可市價委託" in response.message):
                                    if (symbol in self.__open_order_placed) and \
                                            (self.__open_order_placed[symbol] > 0):
                                        self.logger.info(f"{symbol} 已進場 " +
                                                         f"{self.__open_order_placed[symbol]} 張, " +
                                                         f'剩餘量委託失敗次數過多取消')
                                        self.logger.info(f"{symbol} 不再確認進場訊號 ...")
                                        self.__suspend_entering_symbols.append(symbol)

                                    else:
                                        self.logger.info(f"{symbol} 進場下單失敗次數達 2 次且未進場，移除標的")
                                        self.__open_order_placed[symbol] = 99999

                                        # Execute the routine
                                        remove_symbol_at_entry_stage_routine(symbol)

                                # Cancel ramin quantity_to_bid
                                quantity_to_bid = -99999

                        # Adjust for actual used fund
                        async with self.__fund_available_update_lock:
                            self.__fund_available += pre_allocate_fund
                            self.logger.info(f"{symbol} 實際運用差額: {pre_allocate_fund}")
                            self.logger.info(f"可用額度更新: {self.__fund_available}")

                        # Pause if enter successfully
                        if quantity_has_bid > 0:
                            await asyncio.sleep(10)

            elif (symbol not in self.__open_order_placed) and (not self.__is_reload):  # 今天完全沒進場
                self.logger.info(f"{symbol} 今日無進場，移除股價行情訂閱 ...")
                self.__event_loop.run_in_executor(
                    self.__threadpool_executor,
                    self.remove_realtime_marketdata,
                    symbol
                )
                self.__symbols_task_done.append(symbol)

            # 停損停利出場
            if (now_time < self.__strategy_exit_time) and \
                    (symbol in self.__position_info) and \
                    (not self.__on_going_orders[symbol]):

                info = self.__position_info[symbol]
                sell_price = info["price"]
                ask_price = float(data["ask"]) if ("ask" in data and float(data["ask"]) > 0) else float(
                    data["price"])  # Add if for robustness

                gap_until_now_pct = 100 * (ask_price - self.__lastday_close[symbol]) / self.__lastday_close[
                    symbol]
                # current_pnl_pct = 100 * (sell_price - ask_price) / ask_price
                # is_early_session = now_time <= datetime.time(9, 30)

                # if (self.__trail_stop_profit_cutoff[symbol] < 0) and (current_pnl_pct >= 3.5):
                #     # Set the cutoff to the current_pnl_pct
                #     self.__trail_stop_profit_cutoff[symbol] = current_pnl_pct - 0.5
                # elif (self.__trail_stop_profit_cutoff[symbol] > 0) and \
                #         (current_pnl_pct - self.__trail_stop_profit_cutoff[symbol] >= 1):
                #     # Rise the cutoff
                #     self.__trail_stop_profit_cutoff[symbol] = current_pnl_pct - 0.5

                # stop_condition_1 = is_early_session and (current_pnl_pct <= -5)
                # stop_condition_2 = (not is_early_session) and (current_pnl_pct <= -3)
                # stop_condition_alpha = current_pnl_pct < self.__trail_stop_profit_cutoff[symbol]

                # if self.__strategy_code__ == "cdl":
                #     stop_condition_1 = False #current_pnl_pct < -4.5
                #     stop_condition_2 = stop_condition_alpha = False
                # else:
                stop_condition_1 = stop_condition_2 = stop_condition_alpha = False

                stop_condition_zeta = (gap_until_now_pct > self.__zeta__)

                if stop_condition_zeta or stop_condition_1 or stop_condition_2 or stop_condition_alpha:
                    self.logger.info(f"{symbol} 停損/停利條件成立 ...")

                    if stop_condition_zeta:
                        self.logger.info(f"{symbol} 漲幅達 {gap_until_now_pct} %, 斷然出場！")
                        order = Order(
                            buy_sell=BSAction.Buy,
                            symbol=symbol,
                            price=None,
                            quantity=int(self.__position_info[symbol]["size"]),
                            market_type=MarketType.Common,
                            price_type=PriceType.Market,
                            time_in_force=TimeInForce.ROD,
                            order_type=OrderType.Stock,
                            user_def="hvl_stop",
                        )
                    else:
                        order = Order(
                            buy_sell=BSAction.Buy,
                            symbol=symbol,
                            price=None,
                            quantity=1000,
                            market_type=MarketType.Common,
                            price_type=PriceType.Market,
                            time_in_force=TimeInForce.IOC,
                            order_type=OrderType.Stock,
                            user_def="hvl_stop",
                        )

                    # 下單
                    if self.__failed_exit_order_count.get(symbol, 0) < 5:

                        response, place_order_start_time, place_order_end_time = await self.__place_order(order)

                        if response.is_success:
                            self.logger.info(f"{symbol} 停損/停利下單成功")
                            self.logger.debug(
                                f"Order lock checkpoint: {1000 * (order_lock_checkpoint_end - order_lock_checkpoint_start):.6f} ms, " +
                                f"啟動洗價等待時間 {1000 * (initialization_time - data['ws_received_time']):.6f} ms, " +
                                f"下單前置 {1000 * (place_order_start_time - data['ws_received_time']):.6f} ms, " +
                                f"速度 {1000 * (place_order_end_time - place_order_start_time):.6f} ms, " +
                                f"觸發行情資料:\n {data}\nresponse:\n{response}"
                            )

                            # Execute the routine after a success order
                            order_success_routine(symbol, response, "stop")

                        elif (response.message is not None) and ("集合競價" in response.message):
                            self.logger.info(f"集合競價! 失敗停損/停利出場委託單直回:\n{response}")

                            if stop_condition_zeta: #current_pnl_pct < 0 or stop_condition_zeta:
                                self.logger.info(f"集合競價停損, 斷然出場 ...")

                                order = Order(
                                    buy_sell=BSAction.Buy,
                                    symbol=symbol,
                                    price=None,
                                    quantity=int(self.__position_info[symbol]["size"]),
                                    market_type=MarketType.Common,
                                    price_type=PriceType.LimitUp,
                                    time_in_force=TimeInForce.ROD,
                                    order_type=OrderType.Stock,
                                    user_def="hvl_stop",
                                )

                                response, place_order_start_time, place_order_end_time = await self.__place_order(
                                    order)

                                self.logger.debug(
                                    f"集合競價停損出場單: " +
                                    f"Order lock checkpoint: {1000 * (order_lock_checkpoint_end - order_lock_checkpoint_start):.6f} ms, " +
                                    f"啟動洗價等待時間 {1000 * (initialization_time - data['ws_received_time']):.6f} ms, " +
                                    f"下單前置 {1000 * (place_order_start_time - data['ws_received_time']):.6f} ms, " +
                                    f"速度 {1000 * (place_order_end_time - place_order_start_time):.6f} ms, " +
                                    f"觸發行情資料:\n{data}\nresponse:\n{response}"
                                )

                                # Execute the routine after a success order
                                order_success_routine(symbol, response, "stop")
                            else:
                                self.logger.info(f"集合競價停利, 等待 5 秒再處理 ..., 觸發行情\n{data}")
                                await asyncio.sleep(5)

                        elif (response.message is not None) and ("價格穩定" in response.message):
                            if (response.code is not None) and ("51" in str(response.code)):
                                # 價格穩定，可成交部分之委託數量生效
                                self.logger.debung(f"價格穩定，可成交部分之委託數量生效, 剩餘剔退 ...")
                                # Execute the routine after a success order
                                order_success_routine(symbol, response, "stop")

                                self.logger.debug(
                                    f"Order lock checkpoint: {1000 * (order_lock_checkpoint_end - order_lock_checkpoint_start):.6f} ms, " +
                                    f"啟動洗價等待時間 {1000 * (initialization_time - data['ws_received_time']):.6f} ms, " +
                                    f"下單前置 {1000 * (place_order_start_time - data['ws_received_time']):.6f} ms, " +
                                    f"速度 {1000 * (place_order_end_time - place_order_start_time):.6f} ms, " +
                                    f"觸發行情資料:\n {data}\nresponse:\n{response}"
                                )
                            else:
                                self.logger.debug(f"價格穩定觸發，等待 5 秒再處理 ..., 觸發行情\n{data}")
                                await asyncio.sleep(5)

                        else:
                            self.logger.debug(f"失敗停損出場委託單直回:\n{response}\n觸發行情:\n{data}")

                            # OS error handling
                            if ("Broken pipe" in response.message) or ("os error" in response.message):
                                raise OSError

                            if symbol in self.__failed_exit_order_count:
                                self.__failed_exit_order_count[symbol] += 1
                            else:
                                self.__failed_exit_order_count[symbol] = 1

                            if self.__failed_exit_order_count[symbol] >= 5:
                                self.logger.warning(f"{symbol} 停損/利下單失敗次數達 5 次!")
                    else:
                        self.logger.warning(f"{symbol} 停損/利下單失敗次數過多! 手動處理!")
                        # Remove position info for the symbol
                        if symbol in self.__position_info:
                            del self.__position_info[symbol]
                            self.__flush_position_info()

                        # Unsubscribe the price data
                        self.__event_loop.run_in_executor(
                            self.__threadpool_executor,
                            self.remove_realtime_marketdata,
                            symbol
                        )

                        # Add the symbol to the task done list
                        self.__symbols_task_done.append(symbol)

        except OSError as err:
            self.logger.error(f"OS Error {err} while sending orders, restart the strategy subprocess ...")
            self.logger.debug(f"\ttraceback:\n{traceback.format_exc()}")
            self.__self_restart()

        except Exception as err:
            self.logger.error(f"__realtime_price_data_processor, error: {err}")
            self.logger.debug(f"\ttraceback:\n{traceback.format_exc()}")
            self.logger.debug(f"\treceived data: {data}")

        finally:
            if is_locked:
                self.__on_going_orders_lock[symbol].release()

    def __order_filled_processor(self, code, filled_data):
        asyncio.run_coroutine_threadsafe(
            self.__order_filled_processor_async(code, filled_data),
            loop=self.__event_loop
        )

    async def __order_filled_processor_async(self, code, filled_data):
        self.logger.debug(f"__order_filled_processor: code {code}, filled_data\n{filled_data}")

        if filled_data is not None:
            try:
                order_no = str(filled_data.order_no)
                symbol = str(filled_data.stock_no)
                account_no = str(filled_data.account)
                filled_qty = int(filled_data.filled_qty)
                filled_price = float(filled_data.filled_price)

                target_account_no = str(self.sdk_manager.active_account.account)

                if account_no == target_account_no and (symbol in self.__on_going_orders_lock):
                    async with self.__on_going_orders_lock[symbol]:
                        if order_no in self.__order_type_enter[symbol]:
                            if symbol not in self.__position_info:
                                self.__position_info[symbol] = {
                                    "price": filled_price,
                                    "size": filled_qty
                                }
                                self.__flush_position_info()

                            else:
                                original_price = self.__position_info[symbol]["price"]
                                original_size = self.__position_info[symbol]["size"]

                                new_size = original_size + filled_qty
                                new_price = (original_price * original_size + filled_price * filled_qty) / new_size

                                self.__position_info[symbol] = {
                                    "price": new_price,
                                    "size": new_size
                                }
                                self.__flush_position_info()

                            self.logger.debug(f"position_info updated (enter): {self.__position_info}")

                        elif order_no in self.__order_type_exit[symbol]:
                            if symbol not in self.__position_info:
                                self.logger.debug(f"Symbol {symbol} is not in self.__position_info")

                            else:
                                original_size = self.__position_info[symbol]["size"]

                                if filled_qty >= original_size:  # Position closed
                                    # Remove position info for the symbol
                                    del self.__position_info[symbol]
                                    self.__flush_position_info()

                                    # Add the symbol to the task done list
                                    self.__symbols_task_done.append(symbol)

                                    # Unsubscribe realtime market data
                                    self.__event_loop.run_in_executor(
                                        self.__threadpool_executor,
                                        self.remove_realtime_marketdata,
                                        symbol
                                    )

                                else:
                                    self.__position_info[symbol]["size"] = original_size - filled_qty
                                    self.__flush_position_info()

                                self.logger.debug(f"position_info updated (stop/closure): {self.__position_info}")

                        else:
                            self.logger.debug(f"Unregistered order {order_no}, ignore.")

                            return

                        # Update on_going_orders
                        try:
                            self.__on_going_orders_details[order_no]["filled"] += filled_qty
                            if self.__on_going_orders_details[order_no]["filled"] >= \
                                    self.__on_going_orders_details[order_no]["ordered"]:
                                self.__on_going_orders[symbol].remove(order_no)

                            self.logger.debug(
                                f"on_going_orders updated (filled data): symbol {symbol}, " +
                                f"order_no {order_no}, " +
                                f"order details: {self.__on_going_orders_details[order_no]}"
                            )
                        except Exception as err:
                            self.logger.error(f"Update on_going_orders (filled data) exception: {err}, " +
                                              f"traceback {traceback.format_exc()}")
                            if order_no in self.__on_going_orders[symbol]:
                                self.__on_going_orders[symbol].remove(order_no)
                                self.logger.info(f"Remove order anyway (filled data): symbol {symbol}, " +
                                                 f"order_no {order_no}")
                else:
                    self.logger.debug(f"Unregistered order, ignore. account - {account_no}")

            except Exception as err:
                self.logger.error(f"Filled data processing error - {err}. Filled data:\n{filled_data}")

                # Deal with 'NoneType' object has no attribute 'create_future'
                if "create_future" in str(err):
                    self.logger.info(f"Filled data processing - Re-process the filled_data")
                    self.__order_filled_processor(code, filled_data)

        else:
            self.logger.error(f"Filled order error event: code {code}, filled_data {filled_data}")


def main(convoy_queue: multiprocessing.Queue, logger: logging.Logger):
    try:
        # Send the starting check message to the multiprocessing queue
        message = {
            "event": "start",
            "data": None
        }
        convoy_queue.put(message)

        # Load login info as the environment variables
        load_dotenv()  # Load .env
        my_id = os.getenv("ID")
        trade_password = os.getenv("TRADEPASS")
        cert_filepath = os.getenv("CERTFILEPATH")
        cert_password = os.getenv("CERTPASSS")
        active_account = os.getenv("ACTIVEACCOUNT")

        # Create SDKManger instance
        sdk_manager = SDKManager()
        logger.info("logging in ...")
        sdk_manager.login(my_id, trade_password, cert_filepath, cert_password)
        logger.info("Set account No. ...")
        sdk_manager.set_active_account_by_account_no(active_account)

        # Strategy
        my_strategy = TradingHeroAlpha(convoy_queue)
        logger.info("Set SDK manager ...")
        my_strategy.set_sdk_manager(sdk_manager)
        logger.info("Start running ...")
        my_strategy.run()

        # Ending
        logger.info("Ending session ...")
        sdk_manager.terminate()

        # Send the terminating message to the multiprocessing queue
        message = {
            "event": "terminate",
            "data": None
        }
        convoy_queue.put(message)

    except Exception as err:
        logger.error(f"Main script exception {err}, traceback:\n{traceback.format_exc()}")

    finally:
        logger.info("program ended")


# Main script
if __name__ == '__main__':
    st = time.time()

    utils.mk_folder("log")
    utils.mk_folder("position_info_store")
    current_date = datetime.datetime.now(ZoneInfo("Asia/Taipei")).date().strftime("%Y-%m-%d")

    logger, log_shutdown_event = utils.get_logger(
        name="Strategy_supervisor",
        log_file=f"log/strategy_supervisor_{current_date}.log",
        log_level=logging.DEBUG
    )

    # Start the multiprocessing procedure
    queue = multiprocessing.Queue()
    p = multiprocessing.Process(target=main, args=(queue, logger))
    p.start()

    while True:
        try:
            # Try to get a message from the queue with a timeout
            # loop_start_time = time.time()
            message = queue.get(timeout=30)

            if message["event"] == "strategy_heartbeat":
                pass
                # logger.debug(f"Heartbeat interval: {message['data'] - loop_start_time:.6f} s")

            elif message["event"] == "restart":
                logger.info("Restarting strategy ...")
                raise Empty

            elif message["event"] == "position_info":
                pos_info_file_path = \
                    f"position_info_store/position_info_{datetime.datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d')}.json"

                # Save the position info
                try:
                    # Load the dictionary from a JSON file
                    with open(pos_info_file_path, 'w') as f:
                        json.dump(message["data"], f)

                    logger.debug(f"position_info updated: {message['data']}")

                except Exception as e:
                    logger.debug(f"Flush position_info to file failed!!! Exception: {e}, " +
                                 f"traceback:\n{traceback.format_exc()}")

            elif message["event"] == "terminate":
                logger.info("Supervisor exiting ...")
                break

            elif message["event"] == "warning":
                logger.warning(f"Over {message['data']} seconds, there is no price comes in!")

            else:
                logger.debug(f"Queue message: {message}")

        except Empty:
            # If the queue is empty, terminate the process
            logger.debug("Operation timed out. Terminating the strategy process and starting a new one.")
            p.terminate()
            p.join(timeout=10)
            p.kill()

            # Start a new process
            p = multiprocessing.Process(target=main, args=(queue, logger))
            p.start()

    et = time.time()
    logger.info(f"EXECUTE TIME: {et - st}")
    # Exiting
    log_shutdown_event.set()
    sys.exit()
