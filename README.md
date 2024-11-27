# 台股當沖全自動機器人

* 現股當沖先賣
* 收到行情 -> 判斷下單, 耗時 < 1 ms
* 簡單的進場 filters, 包含開盤漲跌幅、短期趨勢檢核等
* 固定比例停損、移動停利
* 總交易額度控管及單商品最大下單張數限制

```Disclaimer: 僅供參考，實務交易應自行評估並承擔相關風險```<br>
```(a.k.a 不分享策略，機器人只純程式分享。我自己在用但是凡事都有萬一，建議要用自己改，但如果直接用，賺錢賠錢都不要找我 XD)```

## 參考連結
富邦新一代API Python SDK載點及開發說明文件
* [新一代API SDK 載點](https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk)<br>

## 如何使用?

主要設定兩個部分，第一個是用 .env 檔案寫入登入資訊

```python
ID=A123456789                                         # 身分證字號
CERTFILEPATH=C:\\CAFubon\\A123456789\\A123456789.pfx  # 憑證檔案位置
TRADEPASS=mytradepassword                             # 交易密碼
CERTPASSS=mycertificatepassword                       # 憑證密碼
ACTIVEACCOUNT=1111111                                 # 下單用帳號 (不包含開戶營業點代碼)
```
第二個是可以改 ``strategy_async_demo.py`` 裡面的主要參數

```python
class TradingHeroAlpha(Strategy):
    # Position sizing algo demonstration
    def position_algo(self, previous_close: float) -> int:
        ...
    
   def __init__(...):

      ...

      # Setup target symbols
      self.__symbols = ['1234', '5678', ...]  # 監控股票列表
   
      ...
   
      # Position sizing
     self.__fund_available = 500000  # 總下單額度控管
     #self.__enter_lot_limit = 6  # (Note. Use self.position_algo instead)
     self.__max_lot_per_round = 1 #min(2, self.__enter_lot_limit)  # 每次觸發下單要下幾張，會一張一張連續下單
   
     ...
   
     self.__strategy_enter_cutoff_time = datetime.time(9, 45)  # 停止進場時間

     ...
```
