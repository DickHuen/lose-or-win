# START_HERE：喺你部 Windows 電腦行 btcperp

呢個 bot 會用真錢，喺 Polymarket Perps 自動交易 BTC-PERP。佢喺你自己部 Windows 電腦上面行：
- 由「工作排程器」（Task Scheduler）定時啟動；
- 喺瀏覽器 dashboard 睇狀態；
- 有交易或者有事就彈 Windows 通知。

所有時間都係香港時間（HKT, UTC+8），除非寫明 UTC。

> 重要規則（一直有效）
> - **主錢包私鑰永遠唔會經過呢部電腦**。bot 只用 proxy key；主錢包只係喺錢包或者另一部電腦簽名授權（第 3 步）。
> - 唔好用 VPN 或者 proxy 去繞過地區限制。
> - 唔好喺同一個 Polymarket Perps 戶口手動落單：bot 會當係佢唔認識嘅倉，接管或者平倉。
> - 持倉期間唔好入金或者提款。
> - 唔好改 `perpbot\`、`config\` 入面任何檔案。要改嘢就搵 Claude 出新版本 zip（版本號會升，CHANGELOG 會寫低）。
> - `.env` 同佢嘅截圖永遠唔好傳畀任何人（包括 Claude）。

---

## 1. 準備部電腦（只做一次）

1. **安裝 Python 3.12**：去 https://www.python.org/downloads/windows/ 下載「Windows installer (64-bit)」。
   安裝時第一版**記得剔「Add python.exe to PATH」**，然後撳「Install Now」。
2. **時區**：
   - 設定 → 時間與語言 → 日期與時間 → 時區揀「(UTC+08:00) 香港特別行政區」。
   - 開「自動設定時間」，再撳一次「立即同步」。
   - bot 每日落單前會同交易所對時間，相差超過 30 秒就唔開新倉。
3. **電源**：一定要，電腦瞓咗 bot 就唔會行。
   - 設定 → 系統 → 電源 → 「插電時，在此時間後讓裝置進入睡眠狀態」揀 **永不**。
   - Notebook：插住電，「合上蓋時」揀「不執行任何動作」。
   - 備用設定：控制台 → 電源選項 → 變更計劃設定 → 變更進階電源設定 → 睡眠 → **允許喚醒計時器 → 啟用**。
4. **保持登入**：
   - bot 只會喺你登入咗 Windows 嘅時候行。鎖螢幕（Win + L）冇問題；**登出或者關機就唔會行**。
   - 設定 → 帳戶 → 登入選項 → 開啟「**使用我的登入資訊，在更新後自動完成設定**」，Windows Update 重新開機之後就會自動登入返。
   - 設定 → Windows Update → 進階選項 → 設定「使用時段」。
   - 做一次測試：重新開機之後，確認 dashboard 自動開返。
5. **網絡**：正常家用網絡，唔好開 VPN 或者 proxy。

## 2. 安裝

1. 將 `btcperp_vX.Y.Z.zip` 放喺「下載」。**先右撳個 zip → 內容 → 剔「解除封鎖」→ 確定**。
2. 右撳 zip → 「解壓縮全部」→ 目的地打 `C:\` → 解壓縮。
   完成之後應該見到 `C:\btcperp\run.py` 同 `C:\btcperp\windows\`。
3. 開 `C:\btcperp\windows\`，雙擊 **`1_Install.bat`**，見到 **`INSTALL PASS (btcperp v…)`** 就得。
   見到 `INSTALL FAIL` 的話，將畫面截圖畀 Claude。

之後升級唔使再解壓，用第 10 步嘅 `Upgrade.bat`。

## 3. Proxy key（主錢包私鑰唔會掂到呢部電腦）

雙擊 **`windows\Proxy_Key.bat`**：

- **N**：喺呢部電腦嘅瀏覽器錢包簽名（例如 MetaMask，最好連住 Ledger/Trezor 之類嘅硬件錢包）。
  1. bot 喺呢部電腦產生 proxy key，然後打開一個本機簽名頁。
  2. 撳「用錢包簽名」。錢包會要求切換到 **Polygon** 網絡，並顯示 CreateProxy 訊息：確認入面嘅 addr 同頁面顯示嘅 proxy 地址一樣，然後簽。
  3. bot 會自動向交易所登記，並寫好 `.env`。
- **O**：喺**另一部電腦**簽名。
  1. bot 產生 proxy key，並寫出 `data\proxykey\sign.html` 同 `sign_request.json`。
  2. 將兩個檔案抄去嗰部電腦簽名。用錢包開 `sign.html`；如果係 email 登入導出嘅私鑰，就用 `offline_sign.py`，詳情 Claude 會教你。
  3. 返嚟雙擊 `Proxy_Key.bat` 揀 **F**，貼上簽名。
  - 簽名要盡快做（幾分鐘內），交易所可能唔收太舊嘅簽名；唔收嘅話重新做一次就得。
- **S**：顯示狀態。

Proxy key 預設 30 日到期，到期前 5 日 bot 會提你重做。**唔知點做就問 Claude，會一步步教你。**

## 4. Smoketest（上實盤之前必做）

雙擊 **`windows\2_Smoketest.bat`**：
- **YES**：完整測試，用最細注碼落**真單**：
  - 開同平幾個好細嘅倉（多、空、flip）；
  - 落單再取消；
  - 試一次 bracket 止損；
  - 記錄交易所對「FOK 未成交」嘅回應；
  - 會用少少手續費。
- **W**：同 YES 一樣，再加**提款探測**：用 proxy key 叫交易所提 1 個最細單位去你自己錢包，**一定要被拒絕**。上實盤之前要做一次。
- **R**：只做唯讀檢查，唔落單。

完成之後將畫面上嘅 **SUMMARY** 截圖畀 Claude（唔好截 `.env`）。完整結果喺 `C:\btcperp\data\smoketest\`。

## 5. 回測（上實盤之前必做，詳情睇 BACKTEST.md）

1. 先將 `config\backtest_criteria.yaml`（合格準則草稿）同 `BACKTEST.md` 畀 Grok 委員會審閱。
2. 委員會同意之後，雙擊 **`windows\Backtest.bat`**：
   1. 佢會下載 Binance 公開數據（第一次要幾分鐘）；
   2. 第一次會要你打 **CONFIRM** 確認準則；確認咗之後準則就唔可以因為結果而改；
   3. 然後用低優先度跑，大約一至兩分鐘。
3. 將 `data\backtest\results_…\summary.md` 同 `summary.json` 畀 Claude 同委員會。

回測唔會落單，亦唔會接觸你個戶口。

## 6. Dashboard

雙擊 **`windows\Dashboard.bat`**，瀏覽器會開 **http://127.0.0.1:8765**，只有呢部電腦睇到。
- 用 Dashboard.bat 開嘅話，黑色視窗要保持開住。
- 上實盤之後，dashboard 會喺你每次登入時自動喺背景行。

| 卡片 | 內容 |
|---|---|
| 頂部 | 狀態（空倉 / 持倉 / 暫停＋原因）、交易所讀取有冇失敗、版本；「從交易所更新」、「標記警報已讀」 |
| 權益 | 權益（交易所 total account value）、錢包、未實現盈虧、高水位 |
| 回撤 / 連虧 / 本金底線 | 三個 kill switch 同佢哋嘅界線（15% 回撤平倉暫停；連虧 8% 停新倉；跌穿淨投入本金 75% 硬停） |
| 倉位 | 方向、數量、入場價、標記價、未實現盈虧、**止損 SL**（冇 SL 會紅字「無！」）、止盈 TP、強平價、累計資金費 |
| 最新決定 | 分數同三個組成部分、方向、注碼級別、閘門、行動、原因 |
| 其他 | 權益走勢、統計、交易紀錄、警報、排程及錯誤、經濟事件、影子追蹤 |

Dashboard 係**唯讀**，亦永遠唔會加交易掣。交易控制只用下面嘅 .bat，重要動作要打字確認。

## 7. 上實盤（GO）：全部 ✓ 先可以開始

- [ ] 第 1 步電腦設定做好，並做過一次重新開機測試。
- [ ] Proxy key 用第 3 步做（主錢包私鑰冇經過呢部電腦）。
- [ ] Smoketest 揀 **W** 全部 PASS，提款探測**被拒絕**。
- [ ] 回測準則已確認，回測結果 **PASS**，委員會睇過。
- [ ] 心跳監察（第 8 步）設定好，並試過收到通知。
- [ ] 你書面確認三個參數：名義上限 30%、連虧 8%、本金底線 75%；同埋打和規則 ±0.1%。

全部 ✓ 之後，雙擊 **`windows\3_Schedule_Install.bat`**，打 `GO`。
- 如果最新嘅完整 smoketest 唔係呢個版本、唔係而家呢條 proxy key，或者冇 PASS，佢會拒絕。
- 佢會喺工作排程器開一個 `btcperp` 資料夾：

| 工作 | 時間（HKT） | 做咩 |
|---|---|---|
| decide_0830、decide_0850 | 每日 08:30、08:50 | 計分數、開倉／平倉／flip（入場窗口 08:30–09:30，過咗唔補入；但平倉規則照做） |
| manage × 5 | 12:30、16:30、20:30、00:30、04:30 | 對數、確保有 SL；如果當日 decide 冇行到，就補做平倉規則；**唔會開新倉** |
| report_daily / weekly / monthly | 08:45 / 星期日 20:00 / 每月第一個星期日 20:30 | 報告 |
| backup | 每日 03:00 | 備份資料庫 |
| dashboard | 每次登入 | 背景 dashboard |

裝完雙擊 **`windows\Schedule_Check.bat`**，睇工作、下次執行時間同電源設定（有 WARNING 就要改）。

## 8. 通知同心跳監察

- 每個警報都會存入資料庫、喺 dashboard 顯示，同埋喺 bot 行完之後彈 **Windows 通知**，顯示為「Windows PowerShell」。
- 通知一次最多彈 5 個，其餘喺 dashboard。
- 冇通知彈出的話：設定 → 系統 → 通知 → 開啟，並容許「Windows PowerShell」；「勿打擾」開咗就會收埋。
- **你已決定唔用 Telegram**，所以人唔喺電腦前面收唔到交易通知。心跳監察可以話你知「bot 停咗」：
  1. 喺 https://healthchecks.io 開免費戶口 → New Check → Period 揀 **6 小時**、Grace 揀 1 小時 → 加手機 App 或者 email 通知。
  2. 複製個 ping 網址（例如 `https://hc-ping.com/xxxxxxxx-…`）。
  3. 雙擊 `windows\Edit_Secrets.bat`，填入 `HEALTHCHECK_PING_URL=`，儲存。
  4. 之後每次 decide / manage 行完都會 ping 一次（唔會傳送任何資料）；出錯會 ping `/fail`。
  5. 電腦熄咗、登出咗或者斷網超過 6 小時，你部手機就會收到通知。

## 9. 日常控制（`C:\btcperp\windows\`）

| 檔案 | 作用 |
|---|---|
| `Dashboard.bat` | 開 dashboard |
| `Status.bat` | 狀態、倉位、SL/TP、權益、kill switch、最後決定 |
| `Pause_New_Entries.bat` | **暫停**：唔再開新倉；現有倉位同 SL/TP 保留 |
| `Unpause.bat` | 只解除你嘅手動暫停（kill switch 同本金底線唔會解除；高水位同連虧計數不變） |
| `Kill_Close_Position.bat` | **即刻平倉**（reduce-only 市價）並暫停；要打 `KILL` |
| `Resume.bat` | 先列出所有暫停原因，打 `RESUME` 解除。如果係回撤或者連虧 kill switch，要再打 `RESET-PEAK`（高水位重設、連虧重新計） |
| `Alerts.bat` | 列出未讀警報 |
| `Report_Daily.bat` | 即刻出日報 |
| `Schedule_Check.bat` / `Schedule_Remove.bat` | 睇／移除排程 |
| `Proxy_Key.bat` | 整新 proxy key（到期前） |
| `Edit_Secrets.bat` | 用記事本改 `.env`（心跳網址） |
| `Upgrade.bat` | 升級（第 10 步） |
| `Backtest.bat` | 回測 |

- **本金底線（EQUITY FLOOR）**觸發之後，Resume 都解除唔到，要新版本 config 寫明新嘅本金基數先得。即刻話 Claude 知。
- 所有 .bat 只可以喺 `C:\btcperp\windows\` 用。喺其他副本撳會被拒絕。
- 記錄檔喺 `C:\btcperp\logs\`（已遮蔽私鑰同 secret）。**永遠唔好傳 `.env`。**

## 10. 升級（收到新版本 zip）

避開 08:20–09:35 HKT。
1. 將新 zip 放喺「下載」，**唔使解壓**。
2. 雙擊 `windows\Pause_New_Entries.bat`。
3. 雙擊 `windows\Upgrade.bat`，打 `UPGRADE`。佢會：
   1. 停排程；
   2. 等行緊嘅指令完成；
   3. 換檔、裝套件、行測試；
   4. 測試通過先重開排程；**測試失敗就保持停止**，並會講清楚。
4. 睇 `Status.bat` 同 dashboard，冇問題就雙擊 **`Unpause.bat`**。注意係 Unpause，**唔好用 Resume**。
5. 得閒再做一次 smoketest。

## 11. 電腦熄咗、瞓咗、斷網，或者你長時間唔喺度

- 交易所上面嘅 **SL / TP 單照樣有效**。
- 錯過 08:30 同 08:50 嘅話，當日唔會開新倉。下一次 decide 或者 manage 會用當日數據**補做平倉規則**（反向訊號、3 日規則、資金費規則），但唔會補入場。
- 電腦熄咗嗰段時間，kill switch 同所有規則都唔會執行，只有 SL / TP 保護。
- **預計離開超過 24 小時**：出門前雙擊 `Pause_New_Entries.bat`，返嚟再 `Unpause.bat`。
- **超過 72 小時，或者未設定心跳監察**：出門前雙擊 `Kill_Close_Position.bat` 平倉。

## 12. 每月檢討

- 每月第一個星期日 20:30 會出月報：`data\reports\monthly\monthly_YYYY-MM.md` 同 `.json`。
- 如果一個月有超過 2 日冇準時做決定，月報會標「INCOMPLETE MONTH」，嗰個月唔應該用嚟評估策略。
- 將月報畀 Claude，佢會用繁體中文寫檢討，並提出改動建議（每項都要有數據）。
- 任何改動都會係一個新版本 zip，由你決定裝唔裝。

## 13. 參考

- 結束代碼：0 成功、1 錯誤、3 設定／secret 錯誤或者用錯資料夾、4 另一個指令行緊、5 單元測試失敗、6 要額外確認。
- 參數：`config\config.yaml`。詳情：`README.md`、`BACKTEST.md`、`API_NOTES.md`、`REVIEW_v1.2.0.md`（委員會建議點處理）。
- 入金／提款時間測試（只喺空倉時做）：喺 `C:\btcperp` 開「命令提示字元」，行
  `venv\Scripts\python.exe run.py flowwatch --minutes 30`，同時入一筆小額，然後將結果檔路徑話畀 Claude 知。
