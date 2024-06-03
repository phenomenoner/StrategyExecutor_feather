# [好物分享] 實單測試 - 超實用SDK總管及逐筆洗價策略示範 (FubonNeo)
程式交易免不了需要經過實單測試的過程，才能確保執行時都能正確順暢。這裡和大家分享一個經過實單測試的 FubonNeo SDK總管實作程式碼，以及一個相對應的逐筆洗價示範策略，希望能幫助大家在程式交易的路途上走的更順利！

```Disclaimer: 本文僅供教學與參考之用，實務交易應自行評估並承擔相關風險```

## 功能特色
* SDK總管 ``sdk_manager_async.py``
    * 自動斷線重連 (包含交易連線及行情連線)
    * 行情斷線重連後自動回復訂閱頻道
    * 管理多個行情連線，只需要訂閱及退定，程式自動分配到尚有訂閱空間的連線
    * 行情推送外接 function 非同步執行處理
<br>

* 示範策略 ``strategy.py``
    * 委託單執行狀況確認
    * 部位管理
    * 進場、停損停利出場及收盤前最後出場

## 執行邏輯
以下為主程式邏輯，主要分為兩個步驟，第一個步驟是完成登入並設定好SDKManager所使用的帳號，第二步驟為將設定好的SDKManager帶入策略執行即可。

```python
if __name__ == '__main__':
    # 載入登入資訊
    # .env 檔案內容範例:
    #   ID=A123456789
    #   CERTFILEPATH=C:\\CAFubon\\A123456789\\H123071452.pfx
    #   TRADEPASS=mytradepassword
    #   CERTPASSS=mycertificatepassword
    #   ACTIVEACCOUNT=1111111
    load_dotenv()  # Load .env
    my_id = os.getenv("ID")
    trade_password = os.getenv("TRADEPASS")
    cert_filepath = os.getenv("CERTFILEPATH")
    cert_password = os.getenv("CERTPASSS")
    active_account = os.getenv("ACTIVEACCOUNT")  # 指定使用帳號 by 帳號id

    # 建立新的 SDKManager 物件 instance
    sdk_manager = SDKManager()
    sdk_manager.login(my_id, trade_password, cert_filepath, cert_password)  # 登入
    sdk_manager.set_active_account_by_account_no(active_account)  # 設定指定帳號

    # 策略物件 instance
    my_strategy = MyStrategy()
    my_strategy.set_sdk_manager(sdk_manager)  # 帶入設定好的 SDKManager
    my_strategy.run()  # 執行策略

    # 程式結束
    print("Ending session ...")
    time.sleep(20)
    sdk_manager.terminate()
    print("program ended, press any key to exit ... ")
    input()
```

## 示範策略主要部件

示範策略主體為 ``class MyStrategy``, 其中需要手動修改的有監控商品列表，也就是所謂的選股清單，策略會根據這個清單來決定針對那些商品進行交易。

選股清單就是下面的 ``self.__symbols``, 可設定多個商品，例如 ``["xxxx", "yyyy", "zzzz"]`` 等。

```Disclaimer: 股票列表內容僅為程式撰寫示範之用，無推薦相關標的```

```python
class MyStrategy(Strategy):
    def __init__(self, logger=None, log_level=logging.DEBUG):
        super().__init__(logger=logger, log_level=log_level)

        # Setup target symbols
        self.__symbols = ["0050"]  # 監控股票股號 (上市、上櫃 only)
        ...
```

### 進出場條件
進出場條件分別放在兩個函數 (function) 中，其中一個是:

```python
def __realtime_price_data_processor(self, data):
    ...
```

這個是行情WebSocket訂閱的callback，每次訂閱的商品一有行情訊息，就會執行這個函數(**註:** 為了時效性考量，如果在執行交易邏輯的同時又收到多個行情訊息SDKManager下次會推送最新的行情訊息)，然後檢查是否符合設定的進出場邏輯。

* 進場邏輯:
進場時間設定為 ``self.__strateg_enter_cutoff_time`` 之前，且最佳買價對比昨天收盤的漲跌幅要符合一定的比例。

```python
self.__strateg_enter_cutoff_time = datetime.time(9, 30)

...

now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

# 開盤動作
if now_time < self.__strateg_enter_cutoff_time and \
        symbol not in self.__open_order_placed.keys():
                        
    price_change_pct_bid = \
        100 * (float(data["bid"]) - self.__lastday_close[symbol]) / self.__lastday_close[symbol]                    

    if 1 < price_change_pct_bid < 5:
        self.logger.info(f"{symbol} 進場條件成立 ...")
        ...
```

* 停損、停利出場邏輯:
在全部出場時間點之前 ``self.__strategy_exit_time``，只要用最佳賣價計算的部位賺賠百分比達到一定程度，即停損/停利出場。

```python
self.__strategy_exit_time = datetime.time(13, 15)

...

if now_time < self.__strategy_exit_time and \
        symbol in self.__position_info.keys() and \
        len(self.__on_going_orders[symbol]) == 0:
    info = self.__position_info[symbol]
    sell_price = info["price"]

    ask_price = float(data["ask"])
    current_pnl_pct = 100 * (sell_price - ask_price) / ask_price

    if current_pnl_pct >= 6 or current_pnl_pct <= -3:
        self.logger.info(f"{symbol} 停損/停利條件成立 ...")
        ...
```

### 全部出場
當時間接近收盤，即把今天的部位全部出場，這個功能因為主要是時間條件，與交易行情無關，因此寫在另外一個函數裡面。

這個全出場函數在 ``13:32`` 執行一個無限迴圈，一旦達到全出場時間 ``self.__strategy_exit_time``，如果還有任何部位的話，就利用市價單出場。

```python
def __position_closure_executor(self):
    now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()

    while now_time < datetime.time(13, 32):
        if now_time > self.__strategy_exit_time:
            clean_list = []

            for symbol in self.__position_info.keys():
                with self.__on_going_orders_lock[symbol]:
                    # DayTrade 全部出場
                    if int(self.__position_info[symbol]["size"]) >= 1000 and \
                            (symbol not in self.__closure_order_placed.keys()) and \
                            (len(self.__on_going_orders[symbol]) == 0):
                        self.logger.info(f"{symbol} 全出場條件成立 ...")

                        qty = self.__position_info[symbol]["size"]

                        order = Order(
                            buy_sell=BSAction.Buy,
                            symbol=symbol,
                            price=None,
                            quantity=int(qty),
                            market_type=MarketType.Common,
                            price_type=PriceType.Market,
                            time_in_force=TimeInForce.ROD,
                            order_type=OrderType.Stock,
                            user_def="hvl_close",
                        )

                        response = self.sdk_manager.sdk.stock.place_order(
                            self.sdk_manager.active_account,
                            order,
                            unblock=False,
                        )

                        if response.is_success:
                            self.logger.info(f"{symbol} 全出場下單成功, size {qty}")
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

                            self.logger.debug(
                                f"on_going_orders updated (closure): {self.__on_going_orders}"
                            )
                        else:
                            self.logger.warning(f"{symbol} 全出場下單失敗, size {qty}, msg: {response.message}")

                    else:
                        self.logger.debug(f"全出場條件成立\"未\"成立 ...")
                        self.logger.debug(f"(Closure session) symbol {symbol}")
                        self.logger.debug(f"(Closure session) position info: {self.__position_info}")
                        self.logger.debug(
                            f"(Closure session) closure order placed keys: {self.__closure_order_placed.keys()}")
                        clean_list.append(symbol)

            # Execute position info cleaning
            for symbol in clean_list:
                try:
                    del self.__position_info[symbol]
                finally:
                    self.sdk_manager.unsubscribe_realtime_trades(symbol)

        # sleep
        time.sleep(1)

        # Update the time
        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()
```

### 委託單執行狀況確認
這裡也實作了一個小功能作為委託單執行狀況確認，避免已經下出成功委託單，但還沒成交的時間點中，另外重複下單。

此函數持續更新 ``self.__on_going_orders[symbol]`` 變量，若此變量中有待成交的委託單，其他下單功能及會等待之前的委託單完成後再視情況下單。

```python
def __order_status_updater(self):
    now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()
    self.logger.info(f"Start __order_status_updater")

    while now_time < datetime.time(13, 32):
        # Check if anything to check
        if not all(len(lst) == 0 for lst in self.__on_going_orders.values()):
            # Get order results
            try:
                the_account = self.sdk_manager.active_account
                response = self.sdk_manager.sdk.stock.get_order_results(the_account)

                if response.is_success:
                    data = response.data

                    for d in data:
                        try:
                            order_no = str(d.order_no)
                            symbol = str(d.stock_no)
                            status = int(d.status)

                            with self.__on_going_orders_lock[symbol]:
                                if status != 10:
                                    if order_no in self.__on_going_orders[symbol]:
                                        self.__on_going_orders[symbol].remove(order_no)

                                        self.logger.debug(
                                            f"on_going_orders updated (order updater): {self.__on_going_orders}"
                                        )
                        except Exception as e:
                            self.logger.debug(f"__order_status_updater error (inner loop) - {e}")
                        finally:
                            continue

                else:
                    self.logger.debug(f"__order_status_updater retrieve order results failed, " +
                                      f"message {response.message}")

            except Exception as e:
                self.logger.debug(f"__order_status_updater error - {e}")
            finally:
                time.sleep(1)
                continue

        # sleep
        time.sleep(1)

        # Update the time
        now_time = datetime.datetime.now(ZoneInfo("Asia/Taipei")).time()
```

### 部位資訊更新
這裡使用SDKManager接出來的交易主動回報進行部位更新，出場時會根據現有部位資料決定各商品出場張數。

```python
def __order_filled_processor(self, code, filled_data):
    self.logger.debug(f"__order_filled_processor: code {code}, filled_data\n{filled_data}")

    if filled_data is not None:
        try:
            user_def = str(filled_data.user_def)
            # seq_no = str(filled_data.seq_no)
            order_no = str(filled_data.order_no)
            symbol = str(filled_data.stock_no)
            account_no = str(filled_data.account)
            filled_qty = int(filled_data.filled_qty)
            filled_price = float(filled_data.filled_price)

            target_account_no = str(self.sdk_manager.active_account.account)

            if account_no == target_account_no:
                with self.__on_going_orders_lock[symbol]:
                    if order_no in self.__order_type_enter[symbol]:  # user_def == "hvl_enter":
                        if symbol not in self.__position_info:
                            self.__position_info[symbol] = {
                                "price": filled_price,
                                "size": filled_qty
                            }

                        else:
                            original_price = self.__position_info[symbol]["price"]
                            original_size = self.__position_info[symbol]["size"]

                            new_size = original_size + filled_qty
                            new_price = (original_price * original_size + filled_price * filled_qty) / new_size

                            self.__position_info[symbol] = {
                                "price": new_price,
                                "size": new_size
                            }

                        self.logger.debug(f"position_info updated (enter): {self.__position_info}")

                    elif order_no in self.__order_type_exit[symbol]:  # user_def in ["hvl_stop", "hvl_close"]:
                        if symbol not in self.__position_info:
                            self.logger.debug(f"Symbol {symbol} is not in self.__position_info")

                        else:
                            original_size = self.__position_info[symbol]["size"]

                            if filled_qty >= original_size:  # Position closed
                                # Remove position info for the symbol
                                del self.__position_info[symbol]

                                # Unsubscribe realtime market data
                                self.sdk_manager.unsubscribe_realtime_trades(symbol)

                            else:
                                self.__position_info[symbol]["size"] = original_size - filled_qty

                            self.logger.debug(f"position_info updated (stop/closure): {self.__position_info}")

                    else:
                        self.logger.debug(f"Unregistered order, ignore.\n" + 
                                        f"\tenter orders - {self.__order_type_enter}\n" + 
                                        f"\texit orders - {self.__order_type_exit}")

                    # Update on_going_orders
                    if order_no in self.__on_going_orders[symbol]:
                        self.__on_going_orders[symbol].remove(order_no)

                    self.logger.debug(
                        f"on_going_orders updated (filled data): {self.__on_going_orders}"
                    )
        except Exception as e:
            self.logger.error(f"Filled data processing error - {e}. Filled data:\n{filled_data}")

    else:
        self.logger.error(f"Filled order error event: code {code}, filled_data {filled_data}")
```

## 結語
以上就是這個示範程式碼主要部份說明，希望對於進行程式交易的大家有所幫助，也歡迎多多批評指教。
