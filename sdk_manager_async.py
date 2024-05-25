import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import utils
import datetime
import functools
import json
from zoneinfo import ZoneInfo
import fubon_neo
from fubon_neo.sdk import FubonSDK


# The check_sdk decorator
def check_sdk(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if hasattr(self, 'sdk_manager') and self.sdk_manager is None:
            raise RuntimeError("SDK manager is not initialized")
        elif hasattr(self, '__sdk_manager') and self.__sdk_manager is None:
            raise RuntimeError("SDK manager is not initialized")
        elif not hasattr(self, 'sdk_manager') and not hasattr(self, '__sdk_manager'):
            raise RuntimeError("SDK manager variable not found")
        return func(self, *args, **kwargs)

    return wrapper


class SDKManager:
    __version__ = "2024.0.1"

    def __init__(self, max_marketdata_ws_connect=2, thread_pool_workers=12, logger=None, log_level=logging.DEBUG):
        # Set logger
        if logger is None:
            utils.mk_folder("log")
            current_date = datetime.datetime.now(ZoneInfo("Asia/Taipei")).date().strftime("%Y-%m-%d")
            logger = utils.get_logger(name="SDKManager", log_file=f"log/sdkmanager_{current_date}.log",
                                      log_level=log_level)

        self.__logger = logger

        # Credential data
        self.__id = None
        self.__trade_password = None
        self.__cert_filepath = None
        self.__cert_password = None
        self.__connection_ip = None
        self.__active_account_no = None

        # SDK and account info
        self.sdk = None
        self.accounts = None
        self.active_account = None
        self.trade_ws_on_event = lambda code, msg: self.__logger.debug(f"Trade ws event: code {code}, msg {msg}")
        self.trade_ws_on_order = lambda code, msg: self.__logger.debug(f"Trade ws order: code {code}, msg {msg}")
        self.trade_ws_on_order_changed = lambda code, msg: self.__logger.debug(
            f"Trade ws order_changed: code {code}, msg {msg}")
        self.trade_ws_on_filled = lambda code, msg: self.__logger.debug(
            f"Trade ws order_filled: code {code}, msg {msg}")

        # Marketdata
        self.__ws_connections = []
        self.__ws_subscription_counts = []
        self.__ws_subscription_list = []
        self.__subscription_details = {}  # {symbol -> [ws_pos, channel_id]}

        self.on_connect_callback = lambda: self.__logger.debug("Marketdata websocket connected")
        self.on_disconnect_callback = self.handle_marketdata_ws_disconnect
        self.on_error_callback = lambda error: self.__logger.debug(f"Marketdata ws error: err {error}")
        self.on_message_callback = lambda msg: self.__logger.debug(f"marketdata ws msg: {msg}")

        self.__is_marketdata_ws_connect = False
        self.__max_marketdata_ws_connect = max(min(5, int(max_marketdata_ws_connect)), 1)
        self.__logger.info(f"max_marketdata_ws_connect setting: {self.__max_marketdata_ws_connect}")

        # Callback lock
        self.__process_lock = threading.Lock()
        self.__re_login_lock_counter = 0

        # Version check
        self.__logger.debug(f"SDK version: {fubon_neo.__version__}")
        self.__logger.debug(f"SDKManager version: {self.__version__}")

        # Coordination
        self.__is_alive = True
        self.__is_relogin_running = False
        self.__is_terminate = False

        # Async
        self.__event_loop = asyncio.new_event_loop()
        self.__async_thread = threading.Thread(
            target=self.__event_loop.run_until_complete,
            args=(self.__async_keep_running(),)
        )
        self.__async_thread.start()

        self.__async_lock_by_symbol = {}  # {symbol -> the lock}
        self.__latest_timestamp = {}

        # workers = max(int(thread_pool_workers), 1)
        # self.__async_threadpool_executor = ThreadPoolExecutor(workers)
        # self.__logger.info(f"async threadpool worker setting: {workers}")

    """
        Auxiliary Functions
    """

    @staticmethod
    def check_is_terminated(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.__is_terminate:
                self.__logger.error("This SDKManager has been terminated. Please start a new one.")
            else:
                return func(self, *args, **kwargs)

        return wrapper

    async def __async_keep_running(self):
        try:
            while True:
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            self.__logger.debug("__async_keep_running cancelled.")

    """
        Main Functions
    """

    @check_is_terminated
    def login(self, id, trade_password, cert_filepath, cert_password, connection_ip=None):
        """
        Establish connection to the AP server and login
        :param connection_ip: (optional) Specify the url if want to connect to some specific place
        :return: True or False, depends on if the login attempt is successful
        """
        # Reset credential info and previous connection
        self.__id = None
        self.__trade_password = None
        self.__cert_filepath = None
        self.__cert_password = None
        self.__connection_ip = None
        self.accounts = None
        self.active_account = None
        if self.sdk is not None:
            try:
                self.sdk.logout()
            finally:
                self.sdk = None

        # Establish connection
        self.__logger.info("建立主機連線 ...")
        try:
            self.sdk = FubonSDK() if connection_ip is None else FubonSDK(connection_ip)
            self.__connection_ip = connection_ip
        except ValueError as e:
            self.__logger.error(f"無法連至API主機, error msg {e}")
            return False

        # Set sdk callback functions
        self.sdk.set_on_event(self.__handle_trade_ws_event)
        self.sdk.set_on_order(self.trade_ws_on_order)
        self.sdk.set_on_order_changed(self.trade_ws_on_order_changed)
        self.sdk.set_on_filled(self.trade_ws_on_filled)

        # Login
        self.__logger.info("登入帳號 ...")
        response = self.sdk.login(id, trade_password, cert_filepath, cert_password)

        if response.is_success:
            self.__logger.info(f"登入成功, 可用帳號:\n{response.data}")
            self.accounts = response.data

            # For auto re-connect
            self.__id = id
            self.__trade_password = trade_password
            self.__cert_filepath = cert_filepath
            self.__cert_password = cert_password

            # establish marketdata ws
            self.establish_marketdata_connection()

            return True

        else:
            self.__logger.info(f"登入失敗, message {response.message}")
            return False

    @check_is_terminated
    def terminate(self):
        # self.__is_terminate = True

        if self.sdk is not None:
            try:
                # Cancel reconnection functions
                self.sdk.set_on_event(lambda code, msg: None)

                for ws in self.__ws_connections:
                    ws.ee.remove_all_listeners()

                tasks = asyncio.all_tasks(self.__event_loop)
                for task in tasks:
                    task.cancel()

                self.__async_thread.join(5)
                self.__event_loop.close()
                self.__logger.debug("The async event loop closed.")

                # Disconnect marketdata ws
                for ws in self.__ws_connections:
                    ws.disconnect()

                # Logout
                self.sdk.logout()
                self.sdk = None

            finally:
                self.__is_terminate = True

    def is_login(self):
        result = False if self.sdk is None else True
        return result

    def __re_login(self, retry_counter=0, max_retry=20):
        if retry_counter > max_retry:
            self.__logger.error(f"交易連線重試次數過多，中止程式")
            self.__is_alive = False
            return False

        try:
            self.__is_relogin_running = True
            self.__logger.debug(f"re_login retry attempt {retry_counter}")

            self.accounts = None
            self.active_account = None
            if self.sdk is not None:
                try:
                    self.sdk.logout()
                finally:
                    self.sdk = None

            # Establish connection
            self.__logger.info("建立主機連線 ...")
            try:
                self.sdk = FubonSDK() if self.__connection_ip is None else FubonSDK(self.__connection_ip)
            except Exception as e:
                self.__logger.error(f"交易主機連線錯誤 msg: {e}")
                if isinstance(e, str) and "11001" not in e:
                    self.__is_alive = False
                    return False
                else:
                    time.sleep(5)
                    return self.__re_login(retry_counter=retry_counter + 1, max_retry=max_retry)

            # Set sdk callback functions
            self.sdk.set_on_event(self.__handle_trade_ws_event)
            self.sdk.set_on_order(self.trade_ws_on_order)
            self.sdk.set_on_order_changed(self.trade_ws_on_order_changed)
            self.sdk.set_on_filled(self.trade_ws_on_filled)

            # Login
            self.__logger.info("登入帳號 ...")
            response = self.sdk.login(self.__id,
                                      self.__trade_password,
                                      self.__cert_filepath,
                                      self.__cert_password)

            if response.is_success:
                self.__logger.info(f"登入成功, 可用帳號:\n{response.data}")
                self.accounts = response.data

                self.set_active_account_by_account_no(self.__active_account_no)

                self.__is_relogin_running = False

                # reconnect marketdata
                threading.Thread(target=self.handle_marketdata_ws_disconnect,
                                 args=(-9000, "Trade ws reconnected")).start()

                return True

            else:
                time.sleep(5)
                return self.__re_login(retry_counter=retry_counter + 1, max_retry=max_retry)

        except Exception as e:
            self.__logger.error(f"登入發生錯誤: {e}, 將重試 ... ")
            time.sleep(5)
            return self.__re_login(retry_counter=retry_counter + 1, max_retry=max_retry)

    @check_is_terminated
    def set_active_account_by_account_no(self, account_no):
        if not self.is_login():
            self.__logger.info("請先登入")
            return False

        for account in self.accounts:
            if account.account == account_no:
                self.active_account = account
                self.__active_account_no = account_no

                self.__logger.debug(f"指定帳號:\n{self.active_account}")

                return True

        self.__logger.info(f"查無指定帳號 {account_no}")

        return False

    def is_alive(self):
        return self.__is_alive

    """
        Marketdata WebSocket
    """

    def __handle_trade_ws_event(self, code, message):
        print(f"trade_ws_event code {code}, {type(code)}")
        if code in ["300", "301"]:
            self.__logger.info(f"交易連線異常，啟動重新連線 ..., code {code}")
            try:
                self.sdk.logout()
            except Exception as e:
                self.__logger.debug(f"Exception: {e}")

        else:
            self.__logger.debug(f"Trade ws event (debug) code {code}, msg {message}")

    @check_is_terminated
    def handle_marketdata_ws_disconnect(self, code, message):
        self.__logger.debug(f"Marketdata ws disconnected. code {code}, msg {message}")
        self.__is_marketdata_ws_connect = False

        with self.__process_lock:
            self.__handle_marketdata_ws_disconnect_threading(code, message)

    def __handle_marketdata_ws_disconnect_threading(self, code, message):
        if self.on_disconnect_callback is None:  # For planned termination
            return

        # with self.__process_lock:
        if self.__is_marketdata_ws_connect:  # Already reconnected
            self.__logger.debug(f"self.__is_marketdata_ws_connect is {self.__is_marketdata_ws_connect}, ignore...")
            return

        if self.__is_relogin_running:
            self.__logger.debug(f"self.__is_relogin_running is {self.__is_relogin_running}, ignore ...")
            return

        self.__logger.debug(f"Marketdata ws reconnecting ...")

        # Test if the trade connection is alive
        try:
            if self.is_login():
                response = self.sdk.stock.margin_quota(self.active_account, "2330")
                self.__logger.debug(f"Trade connection test response\n{response}")

                if not response.is_success and "Login Error" in response.message:
                    self.__logger.info(f"重連交易連線 ...")
                    self.__re_login()

        finally:
            # Reconnect marketdata
            self.establish_marketdata_connection(reconnect=True)

            # Resubscribe all stocks
            previous_subscription_list = self.__ws_subscription_list.copy()
            self.__ws_subscription_list = []  # Reset the subscription list
            self.__subscription_details = {}

            for symbol in previous_subscription_list:
                self.subscribe_realtime_trades(symbol)
                time.sleep(0.1)

    @check_is_terminated
    def establish_marketdata_connection(self, reconnect=False, retry_counter=0, max_retry=5):
        if retry_counter > max_retry:
            self.__logger.error(f"登入失敗重試次數達上限，停止嘗試")
            return False

        if not self.is_login():
            self.__logger.error("請先登入SDK")
            return False

        self.__logger.info("建立行情連線...")

        try:
            # Disconnect all current websocket if any
            for ws in self.__ws_connections:
                ws.disconnect()

        except Exception as e:
            self.__logger.debug(f"Marketdata ws exception: {e}")

        finally:
            self.__ws_connections = []
            self.__ws_subscription_counts = []
            if not reconnect:
                self.__ws_subscription_list = []
                self.__subscription_details = {}

            # Establish new connections
            try:
                for i in range(self.__max_marketdata_ws_connect):
                    self.__logger.debug(f"建立第 {i + 1} 條連線")
                    self.sdk.init_realtime()
                    ws = self.sdk.marketdata.websocket_client.stock
                    ws.on("connect", self.on_connect_callback)
                    ws.on("disconnect", self.on_disconnect_callback)
                    ws.on("error", self.on_error_callback)
                    ws.on("message", self.__ws_on_message_handler)
                    ws.connect()

                    self.__ws_connections.append(ws)
                    self.__ws_subscription_counts.append(0)

                    time.sleep(0.2)

                self.__is_marketdata_ws_connect = True

                return True

            except Exception as e:
                self.__logger.debug(f"Marketdata ws connection exception {e}, will try to reconnect")
                time.sleep(5)
                return self.establish_marketdata_connection(retry_counter=retry_counter + 1, max_retry=max_retry)

    @check_is_terminated
    def subscribe_realtime_trades(self, symbol: str):
        self.__logger.debug(f"subscribe_realtime_trades {symbol}")

        if symbol in self.__ws_subscription_list:
            self.__logger.info(f"{symbol} 已在訂閱列表中")
            return

        if not self.__ws_connections:
            self.establish_marketdata_connection()

        # Subscribes
        min_num = min(self.__ws_subscription_counts)
        if min_num >= 200:
            self.__logger.error(f"無多餘訂閱空間，忽略此請求")
            return

        # Subscribe
        pos = self.__ws_subscription_counts.index(min_num)

        try:
            self.__ws_connections[pos].subscribe({
                'channel': 'trades',
                'symbol': symbol
            })

        except Exception as e:
            self.__logger.error(f"subscribe_realtime_trades error: {e}")

        # Update records
        self.__ws_subscription_counts[pos] += 1
        self.__ws_subscription_list.append(symbol)
        self.__subscription_details[symbol] = [pos, None]

    @check_is_terminated
    def unsubscribe_realtime_trades(self, symbol: str):
        self.__logger.debug(f"unsubscribe_realtime_trades {symbol}")

        if symbol not in self.__ws_subscription_list:
            self.__logger.warning(f"{symbol} 不在訂閱列表中")
            return

        try:
            pos = self.__subscription_details[symbol][0]
            target_id = self.__subscription_details[symbol][1]
            self.__ws_connections[pos].unsubscribe({
                'id': target_id,
            })

        except Exception as e:
            self.__logger.error(f"unsubscribe_realtime_trades error: {e}")

        # Do not update records here, will do it when the confirmation callback message is received

    @check_is_terminated
    def set_ws_handle_func(self, func_name, func):
        """
        Set callback function to websocket marketdata
        :param func_name: "connect", "disconnect", "error", or "message"
        :param func: The corresponding callback function
        """
        match func_name:
            # case "connect":
            #     self.on_connect_callback = func
            # case "disconnect":
            #     self.on_disconnect_callback = func
            # case "error":
            #     self.on_error_callback = func
            case "message":
                self.on_message_callback = func
            case _:
                self.__logger.error(f"Undefined function name {func_name}")
                return

        # Set callbacks
        for ws in self.__ws_connections:
            ws.on("connect", self.on_connect_callback)
            ws.on("disconnect", self.on_disconnect_callback)
            ws.on("error", self.on_error_callback)
            ws.on("message", self.__ws_on_message_handler)

    @check_is_terminated
    def set_trade_handle_func(self, func_name, func):
        """
        Set sdk trade callback function
        :param func_name: "on_event", "on_order", "on_order_changed", or "on_filled"
        :param func: The corresponding callback function
        """
        match func_name:
            # case "on_event":
            #     self.trade_ws_on_event = func
            case "on_order":
                self.trade_ws_on_order = func
            case "on_order_changed":
                self.trade_ws_on_order_changed = func
            case "on_filled":
                self.trade_ws_on_filled = func
            case _:
                self.__logger.error(f"Undefined function name {func_name}")
                return

        # Set callbacks
        if self.sdk is not None:
            self.sdk.set_on_event(self.__handle_trade_ws_event)
            self.sdk.set_on_order(self.trade_ws_on_order)
            self.sdk.set_on_order_changed(self.trade_ws_on_order_changed)
            self.sdk.set_on_filled(self.trade_ws_on_filled)

        else:
            self.__logger.info("Please connect sdk first")

    @check_is_terminated
    def get_marketdata_rest_client(self):
        if not self.is_login():
            self.__logger.error("請先登入SDK")
            return None

        self.sdk.init_realtime()

        return self.sdk.marketdata.rest_client.stock

    """
        Callback wrappers
    """

    def __ws_on_message_handler(self, message):
        if self.on_message_callback is not None:
            if "pong" not in message:
                try:
                    msg = json.loads(message)  # Loads json str to dictionary

                    # subscription detail update
                    if msg["event"] == "subscribed":
                        symbol = msg["data"]["symbol"]
                        channel_id = msg["data"]["id"]
                        self.__async_lock_by_symbol[symbol] = asyncio.Lock()
                        self.__subscription_details[symbol][1] = channel_id

                    elif msg["event"] == "unsubscribed":
                        symbol = msg["data"]["symbol"]
                        ws_pos = self.__subscription_details[symbol][0]

                        self.__ws_subscription_list.remove(symbol)  # Clear from the symbol list
                        del self.__subscription_details[symbol]  # Clear from the detail dictionary
                        self.__ws_subscription_counts[ws_pos] -= 1  # Reduce the counter

                        # Remove async lock
                        del self.__async_lock_by_symbol[symbol]

                    # Pass the message to the custom callback function
                    self.__event_loop.create_task(
                        self.__ws_on_message_handler_task(msg)
                    )

                except Exception as e:
                    self.__logger.error(f"__ws_on_message_handler error: {e}, msg = {message}")

    async def __ws_on_message_handler_task(self, message: dict):
        self.__logger.debug(f"marketdata message: {message}")

        try:
            if message["event"] == "data":
                data = message["data"]
                symbol = data["symbol"]
                timestamp = int(data["time"])

                if symbol in self.__async_lock_by_symbol.keys():
                    async with self.__async_lock_by_symbol[symbol]:
                        if symbol not in self.__latest_timestamp.keys() or \
                                timestamp >  self.__latest_timestamp[symbol]:
                            self.__latest_timestamp[symbol] = timestamp
                            self.on_message_callback(message["data"])

                    # await self.event_loop.run_in_executor(
                    #     self.async_threadpool_executor,
                    #     self.on_message_callback,
                    #     message
                    # )

        except Exception as e:
            self.__logger.error(f"__ws_on_message_handler_task error: error - {e}, message - {message}")

    # def __trade_event_handler(self, code, message):
    #     self.__handle_trade_ws_event(code, message)
    #
    #     if self.trade_ws_on_event is not None:
    #         self.event_loop.create_task(
    #             self.__trade_event_handler_task(code, message)
    #         )

    # async def __trade_event_handler_task(self, code, message):
    #     await self.event_loop.run_in_executor(
    #         self.async_threadpool_executor,
    #         self.trade_ws_on_event,
    #         code, message
    #     )
