import logging
import asyncio
import threading
import time
import traceback
import utils
import datetime
import functools
import json
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
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
    __version__ = "2024.10.5"

    def __init__(self, max_marketdata_ws_connect=1, logger=None, log_level=logging.DEBUG):
        # Set logger
        log_shutdown_event = None
        if logger is None:
            utils.mk_folder("log")
            current_date = datetime.datetime.now(ZoneInfo("Asia/Taipei")).date().strftime("%Y-%m-%d")
            logger,log_shutdown_event = utils.get_logger(
                name="SDKManager",
                log_file=f"log/sdkmanager_{current_date}.log",
                log_level=log_level
            )

        self.__logger = logger
        self.__logger_shutdown = log_shutdown_event

        # Credential data
        self.__id = None
        self.__trade_password = None
        self.__cert_filepath = None
        self.__cert_password = None
        self.__connection_ip = None
        self.__active_account_no = None

        # SDK and account info
        self.sdk: FubonSDK | None = None
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
        self.__ws_message_queue: asyncio.Queue | None = None

        self.__is_marketdata_ws_connect = False
        self.__max_marketdata_ws_connect = max(min(5, int(max_marketdata_ws_connect)), 1)
        self.__logger.info(f"max_marketdata_ws_connect setting: {self.__max_marketdata_ws_connect}")

        self.__heartbeat_timestamp = time.time()

        # Threadpool executor
        self.__threadpool_executor = ThreadPoolExecutor()

        # Callback lock
        self.__process_lock = threading.Lock()
        self.__re_login_lock_counter = 0

        # Version check
        self.__logger.debug(f"SDK version: {fubon_neo.__version__}")
        self.__logger.debug(f"SDKManager version: {self.__version__}")
        self.sdk_version = tuple(map(int, (fubon_neo.__version__.split("."))))

        # Coordination
        self.__is_alive = True
        self.__is_relogin_running = False
        self.__is_terminate = False
        self.__termination_initiated = False
        self.__relogin_lock = threading.Lock()
        self.__realtime_data_processing_locks = {}

        # Async
        self.__trade_message_queue = asyncio.Queue()
        self.__event_loop = asyncio.new_event_loop()
        # self.__async_thread = threading.Thread(
        #     target=self.__event_loop.run_until_complete,
        #     args=(self.__async_keep_running(),)
        # )
        # self.__async_thread.start()
        self.__async_thread = self.__threadpool_executor.submit(
            self.__event_loop.run_until_complete,
            self.__async_keep_running()
        )
        self.__logger.debug(f"Async loop started ...")

        self.__async_lock_by_symbol = {}  # {symbol -> the lock}
        self.__latest_timestamp = {}

    """
        Auxiliary Functions
    """

    @staticmethod
    def check_is_terminated(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.__is_terminate:
                print("This SDKManager has been terminated. Please start a new one.")
            else:
                return func(self, *args, **kwargs)

        return wrapper

    async def __async_keep_running(self):
        # Initialize the trade message queue manager
        #asyncio.ensure_future(self.__ws_on_message_handler_queue_manager())

        # Keep running
        try:
            while True:
                time_now = time.time()
                if time_now - self.__heartbeat_timestamp > 90:
                    # threading.Thread(target=self.handle_marketdata_ws_disconnect,
                    #                  args=(-1, "Heartbeat lost ...")).start()
                    self.__threadpool_executor.submit(
                        self.handle_marketdata_ws_disconnect,
                        -1, "Heartbeat lost ..."
                    )
                    self.__heartbeat_timestamp = time.time()
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
        :param connection_ip: (optional) Specify the url if you want to connect to some specific place
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
            self.establish_marketdata_connection(caller="login")

            self.__logger.debug(f"登入及建立行情連線步驟完成")
            return True

        else:
            self.__logger.info(f"登入失敗, message {response.message}")
            return False

    @check_is_terminated
    def terminate(self):
        self.__termination_initiated = True

        if self.sdk is not None:
            try:
                # Cancel reconnection functions
                self.sdk.set_on_event(lambda code, msg: None)

                for ws in self.__ws_connections:
                    ws.ee.remove_all_listeners()

                tasks = asyncio.all_tasks(self.__event_loop)
                for task in tasks:
                    task.cancel()

                # self.__async_thread.join(5)
                self.__async_thread.result(timeout=5)
                self.__event_loop.close()
                self.__logger.debug("The async event loop closed.")
                self.__threadpool_executor.shutdown(wait=False, cancel_futures=True)
                self.__logger.debug("The threadpool_executor shutdown.")

                # Disconnect marketdata ws
                for ws in self.__ws_connections:
                    ws.disconnect()

                # Logout
                self.sdk.logout()
                self.sdk = None

            finally:
                self.__is_terminate = True
                if self.__logger_shutdown is not None:
                    self.__logger_shutdown.set()

    def is_login(self):
        result = False if self.sdk is None else True
        return result

    def __re_login(self, retry_counter=0, max_retry=20):
        if retry_counter > max_retry:
            self.__logger.error(f"交易連線重試次數過多 {retry_counter}，延長重試時間")
            self.__is_alive = False
            time.sleep(5)

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
                    self.__logger.debug(f"Retry Login (a) ...")
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
                # threading.Thread(target=self.handle_marketdata_ws_disconnect,
                #                  args=(-9000, "Trade ws reconnected")).start()
                self.__threadpool_executor.submit(
                    self.handle_marketdata_ws_disconnect,
                    -9000, "Trade ws reconnected"
                )

                return True

            else:
                time.sleep(5)
                self.__logger.debug(f"Retry Login (b) ...")
                return self.__re_login(retry_counter=retry_counter + 1, max_retry=max_retry)

        except Exception as e:
            self.__logger.error(f"登入發生錯誤: {e}, 將重試 ... ")
            time.sleep(5)
            self.__logger.debug(f"Retry login (c) ...")
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

    def is_terminated(self):
        return self.__is_terminate

    """
        Marketdata WebSocket
    """

    def __handle_trade_ws_event(self, code, message):
        if code in ["300"]:
            self.__logger.info(f"交易連線異常，啟動重新連線 ..., code {code}")
            
            try:
                self.sdk.logout()
            except Exception as e:
                self.__logger.debug(f"Exception: {e}")
            finally:
                with self.__relogin_lock:
                    self.__logger.debug(f"Login (a) ...")
                    self.__re_login()

        elif code in ["302"]:
            self.__logger.info(f"交易連線中斷 ..., code {code}")

        else:
            self.__logger.debug(f"Trade ws event (debug) code {code}, msg {message}")

    @check_is_terminated
    def handle_marketdata_ws_disconnect(self, code, message):
        self.__logger.debug(f"Marketdata ws disconnected. code {code}, msg {message}")
        self.__is_marketdata_ws_connect = False

        # t = threading.Thread(
        #     target=self.__handle_marketdata_ws_disconnect_threading,
        #     args=(code, message)
        # )
        # t.start()
        # t.join()

        future = self.__threadpool_executor.submit(
            self.__handle_marketdata_ws_disconnect_threading,
            code, message
        )
        future.result()

    def __handle_marketdata_ws_disconnect_threading(self, code, message):
        with self.__process_lock:
            if self.on_disconnect_callback is None:  # For planned termination
                return

            if self.__is_marketdata_ws_connect:  # Already reconnected
                self.__logger.debug(f"self.__is_marketdata_ws_connect is {self.__is_marketdata_ws_connect}, ignore...")
                return

            if self.__is_relogin_running:
                self.__logger.debug(f"self.__is_relogin_running is {self.__is_relogin_running}, ignore ...")
                return

            self.__logger.debug(f"Marketdata ws reconnecting ...")

            # Reconnect marketdata
            self.establish_marketdata_connection(reconnect=True, caller="disconnect_handler")

            # Resubscribe all stocks
            previous_subscription_list = self.__ws_subscription_list.copy()
            self.__ws_subscription_list = []  # Reset the subscription list
            self.__subscription_details = {}

            for symbol in previous_subscription_list:
                self.__threadpool_executor.submit(
                    self.subscribe_realtime_trades,
                    symbol
                )
                # self.subscribe_realtime_trades(symbol)
                # time.sleep(0.01)

    def __connection_operator(self):
        self.sdk.init_realtime()
        ws = self.sdk.marketdata.websocket_client.stock
        ws.on("connect", self.on_connect_callback)
        ws.on("disconnect", self.on_disconnect_callback)
        ws.on("error", self.on_error_callback)
        ws.on("message", self.__ws_on_message_handler)
        ws.connect()

        self.__ws_connections.append(ws)
        self.__ws_subscription_counts.append(0)

        return ws

    @check_is_terminated
    def establish_marketdata_connection(self, reconnect=False, retry_counter=0, max_retry=5, caller=None):
        if retry_counter > max_retry:
            self.__logger.error(f"登入失敗重試過多 {retry_counter}，延長重試時間 ...")
            time.sleep(5)

        if not self.is_login():
            self.__logger.error("請先登入SDK")
            return False

        else:
            self.__logger.debug(f"Try trade connection ...")
            with self.__relogin_lock:
                response = self.sdk.stock.margin_quota(self.accounts[0], "2330")
                self.__logger.debug(f"Trade connection test response\n{response}")

                if not response.is_success and "Login Error" in response.message:
                    self.__logger.info(f"重連交易連線 (a) ...")
                    self.__re_login()

            self.__logger.debug(f"Try trade connection completed")

        self.__logger.info("建立行情連線...")
        self.__logger.debug(f"caller: {caller}")

        try:
            disconnect_threads = []

            # Disconnect all current websocket if any
            for ws in self.__ws_connections:
                # t = threading.Thread(target=ws.disconnect)
                t = self.__threadpool_executor.submit(
                    ws.disconnect
                )

                disconnect_threads.append(t)

            for t in disconnect_threads:
                t.result()
            # for t in disconnect_threads:
            #     t.start()
            #
            # for t in disconnect_threads:
            #     t.join()



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

                    # Doing the connection with timeout
                    # t = threading.Thread(target=self.__connection_operator)
                    # t.start()
                    # t.join(5)
                    t = self.__threadpool_executor.submit(
                        self.__connection_operator
                    )
                    t.result(timeout=5)

                    if not t.done():
                        raise TimeoutError

                    time.sleep(0.2)
                    self.__logger.debug(f"建立第 {i + 1} 條連線完畢")

                self.__is_marketdata_ws_connect = True
                self.__logger.debug(f"建立連線成功!")
                return True

            except Exception as e:
                self.__logger.debug(f"Marketdata ws connection exception {e}, will try to reconnect")
                time.sleep(5)
                return self.establish_marketdata_connection(retry_counter=retry_counter + 1, max_retry=max_retry,
                                                            caller="self_retry")

    @check_is_terminated
    def subscribe_realtime_trades(self, symbol: str):
        self.__logger.debug(f"subscribe_realtime_trades {symbol}")

        if symbol in self.__ws_subscription_list:
            self.__logger.info(f"{symbol} 已在訂閱列表中")
            return

        if not self.__ws_connections:
            self.establish_marketdata_connection(caller=" subscribe_realtime_trades")

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
        self.__realtime_data_processing_locks[symbol] = asyncio.Lock()

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
                self.__logger.debug(f"Set self.on_message_callback = {func}")
            case _:
                self.__logger.error(f"Undefined function name {func_name}")
                # return

        # Set callbacks
        # for ws in self.__ws_connections:
        #     ws.on("connect", self.on_connect_callback)
        #     ws.on("disconnect", self.on_disconnect_callback)
        #     ws.on("error", self.on_error_callback)
        #     ws.on("message", self.__ws_on_message_handler)

    @check_is_terminated
    def set_ws_message_handle_queue(self, queue: asyncio.Queue):
        self.__ws_message_queue = queue

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

    def __ws_on_message_find_symbol_by_id(self, target_id):
        try:
            for symbol, values in self.__subscription_details.items():
                if values[1] == target_id:
                    return symbol
        except Exception as e:
            self.__logger.debug(f"__ws_on_message_find_symbol_by_id: error {e}")
            return None

        return None

    def __ws_on_message_handler(self, message):
        if "pong" not in message:
            try:
                time_now = time.time()
                msg = json.loads(message)  # Loads json str to dictionary

                if (msg["event"] == "data") and (self.on_message_callback is not None):
                    try:
                        # Add meta-data
                        msg["data"]["ws_received_time"] = time_now
                        msg["data"]["ws_latency"] = time_now - int(msg["data"]["time"]) / 1000000

                        # Invoke the callback
                        #self.__logger.debug(f"sent callback {msg["data"]["ws_latency"]}")
                        self.on_message_callback(msg["data"])
                        #self.__logger.debug(f"sent callback completed")
                    except Exception as e:
                        self.__logger.error(f"self.__trade_message_queue Exception: {e}, " +
                                            f"traceback:\n{traceback.format_exc()}")

                    # try:
                    #     self.__ws_message_queue.put_nowait(msg["data"])
                    # except asyncio.QueueFull as e:
                    #     self.__logger.warning(f"self.__trade_message_queue is full! Exception: {e}")
                    # except Exception as e:
                    #     self.__logger.error(f"self.__trade_message_queue Exception: {e}, " +
                    #                         f"traceback:\n{traceback.format_exc()}")

                elif msg["event"] == "subscribed":  # subscription detail update
                    symbol = msg["data"]["symbol"]
                    channel_id = msg["data"]["id"]
                    self.__async_lock_by_symbol[symbol] = asyncio.Lock()
                    self.__latest_timestamp[symbol] = 0
                    self.__subscription_details[symbol][1] = channel_id

                elif msg["event"] == "unsubscribed":
                    channel_id = msg["data"]["id"]
                    symbol = self.__ws_on_message_find_symbol_by_id(channel_id)

                    if symbol is not None:
                        ws_pos = self.__subscription_details[symbol][0]

                        self.__ws_subscription_list.remove(symbol)  # Clear from the symbol list
                        del self.__subscription_details[symbol]  # Clear from the detail dictionary
                        self.__ws_subscription_counts[ws_pos] -= 1  # Reduce the counter

                        # Remove async lock
                        del self.__async_lock_by_symbol[symbol]
                        del self.__latest_timestamp[symbol]
                    else:
                        pass
                        # self.__logger.debug(f"Cannot find the id")

                elif msg["event"] == "heartbeat":
                    if time_now > self.__heartbeat_timestamp:
                        self.__heartbeat_timestamp = time_now

                    latency = time_now - float(msg["data"]["time"]) / 1000000
                    self.__logger.debug(f"marketdata - {message}, latency: {latency:.6f} s")

            except Exception as e:
                self.__logger.error(f"__ws_on_message_handler error: {e}, msg = {message}, " +
                                    f"traceback:\n{traceback.format_exc()}")

