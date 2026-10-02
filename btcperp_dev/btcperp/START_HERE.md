# START_HERE：喺你部 Windows 電腦行 btcperp

呢個 bot 會用真錢，喺 Polymarket Perps 自動交易 BTC-PERP。

**v1.5.0 起（你揀咗方案 B）**：bot **每 4 個鐘決定一次**（香港時間 00:30、04:30、08:30、12:30、16:30、20:30）。
- 每次都用「截至嗰一刻嘅 24 小時」做日線計分數，規則同參數同 v1.4 一樣，只係更新密咗 6 倍。
- 每個 4 小時時段最多入場一次。
- 「連續 3 日反方向就平倉」而家即係連續 18 個時段（72 個鐘）。
- 想改返每日一次，config 入面 `strategy.cadence` 改做 `daily`，要出新版本。

佢喺你自己部 Windows 電腦上面行：
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

bot 會喺呢部電腦產生 proxy key，你嘅**主錢包**只係簽一個 CreateProxy 訊息授權佢。
- 每條 proxy key 最長 30 日。
- 開始之後要喺 **1 小時內**完成，否則個請求會自動刪除，要重新做。
- 開始時 bot 會問你**主錢包地址**（0x…，只係公開地址）。之後只接受呢個地址嘅簽名。

雙擊 **`windows\Proxy_Key.bat`**，揀其中一個：

- **P**：用**手機**簽名（v1.5.1）。適合冇硬件錢包、亦冇第二部電腦嘅情況。主錢包私鑰只會喺手機。
  1. **準備手機**（第一次先要做）：
     1. 喺 App Store 或者 Google Play 裝 **MetaMask**。認清楚開發者係 MetaMask／Consensys。
     2. 開 MetaMask，建立錢包並設定密碼。
     3. 撳帳戶 → 「新增帳戶或硬件錢包」→「匯入帳戶」，貼上你嘅主錢包私鑰。
     4. 記低 MetaMask 顯示嘅帳戶地址（0x 加 40 個字元）。
  2. 手機同 bot 電腦要連**同一個 Wi-Fi**。
  3. 喺 bot 電腦揀 **P**，輸入上面嗰個**地址**（唔係私鑰）。
  4. 畫面會顯示一個網址，例如 `http://192.168.1.23:8766/k7m2p9qahx/`。
     - 如果 Windows 防火牆問 Python 可唔可以連網，撳「允許」（私人網絡）。
  5. 手機開 MetaMask → 撳下面嘅「瀏覽器」→ 喺網址欄打入個網址。**要喺 15 分鐘內**完成。
  6. 撳「用錢包簽名」：
     - 如果 MetaMask 問你切換或者新增 Polygon 網絡，同意；
     - 簽名畫面一定要係 **CreateProxy**、**Polymarket**，而且 addr 同 bot 電腦顯示嘅 proxy 地址一樣；
     - 見到 Permit、Approve、轉賬或者其他內容，**一定唔好簽**，話 Claude 知。
  7. 頁面顯示「完成」，bot 電腦亦會顯示 `DONE`，`.env` 就自動填好。
  8. 之後可以喺 MetaMask 刪走嗰個匯入咗嘅帳戶。唔刪都得，但部手機要設密碼鎖。
- **N**：喺呢部電腦嘅瀏覽器簽名，**只限硬件錢包**（Ledger / Trezor，經 MetaMask 或 Rabby）。
  - 如果用普通軟件錢包，主錢包私鑰就會加密存喺呢部電腦，唔准咁做。
  - 冇硬件錢包就用 **P**（手機）或者 **O**（另一部電腦）。
  1. 打 `HARDWARE` 確認你用硬件錢包。
  2. 輸入主錢包地址。瀏覽器會開一個本機簽名頁。
  3. 撳「用錢包簽名」：
     - 錢包帳戶一定要係主錢包；
     - 網絡要係 **Polygon**；
     - 硬件錢包畫面會顯示 CreateProxy，入面 addr 要同頁面嘅 proxy 地址一樣先好簽。
  4. bot 會自動向交易所登記，並寫好 `.env`。
- **O**：喺**另一部電腦**簽名。
  1. 輸入主錢包地址。bot 會寫出一個檔案：`data\proxykey\sign_fields.txt`。
     - 入面只有 5 行純文字：addr、exp、salt、ts、owner。冇任何秘密。
     - 將**呢個檔案**抄去另一部電腦。
  2. 另一部電腦要用**佢自己下載嘅 release zip**，唔好用由 bot 電腦抄過去嘅程式：
     1. 下載 zip 之後，用 `certutil -hashfile btcperp_vX.Y.Z.zip SHA256` 核對 SHA-256，要同 Claude 發佈時畀你嘅一樣。
     2. 然後揀一個方法簽：
        - **瀏覽器錢包**：喺解壓後嘅 `offline_sign` 資料夾開「命令提示字元」，行 `python -m http.server 8000 --bind 127.0.0.1`。然後用瀏覽器開 `http://127.0.0.1:8000/offline_sign.html`，貼上 sign_fields.txt 內容，撳「檢查」，再撳「用瀏覽器錢包簽名」。
        - **私鑰（例如 email 登入導出嘅 key）**：先 `pip install eth-account`，然後行 `python perpbot\offline_sign.py sign_fields.txt`。確認 proxy 地址同到期日（HKT）之後打 `YES`，再輸入私鑰（唔會顯示、唔會儲存）。
     - 兩個方法都只會簽 CreateProxy（Polymarket，chain 137）。任何其他訊息都會被拒絕。
  3. 返嚟 bot 電腦，雙擊 `Proxy_Key.bat` 揀 **F**，貼上簽名（0x…，132 個字元）。
- **S**：狀態。會顯示：
  - 未完成嘅請求；
  - `.env` 入面嘅 proxy key；
  - 交易所登記咗嘅**所有** proxy key 同佢哋嘅到期日。

注意：
- 舊 proxy key 會一直有效，直至到期（最多 30 日）。撤銷要主錢包簽名，所以唔會喺呢部電腦做。
- 到期前 5 日，bot 會提你重做。
- **上實盤清單要寫低你用咗 P、N 定 O**（第 7 步）。
- 唔知點做就問 Claude，會一步步教你。

## 4. Smoketest（上實盤之前必做）

雙擊 **`windows\2_Smoketest.bat`**：
- **YES**：完整測試，用最細注碼落**真單**：
  - 開同平幾個好細嘅倉（多、空、flip）；
  - 落單再取消；
  - 試一次 bracket 止損；
  - 記錄交易所對「FOK 未成交」嘅回應；
  - 記錄真實 taker 手續費（回測費率唔會低過佢）同 Polymarket／Binance 價差；
  - 會用少少手續費。
- 之後 bot 每次 manage 都會記錄價差，月報會列出 p99。
- **W**：同 YES 一樣，再加**提款探測**：用 proxy key 叫交易所提 1 個最細單位去你自己錢包，**一定要被拒絕**。上實盤之前要做一次。
- **R**：只做唯讀檢查，唔落單。
- **region（地區）FAIL**（`"blocked": true`）：Polymarket 唔開放你而家嘅地區，bot 唔會開倉。
  - 先確認 VPN 完全熄咗（Quit，唔係淨係 Disconnect），再行一次 R。
  - 用你真實嘅網絡都係 blocked，就唔可以上實盤。**唔可以用 VPN 或者 proxy 繞過。**
- **fees**：交易所有時冇列出 BTC 嗰類嘅手續費（v1.5.2）。咁 bot 會用較高嘅估計，完整 smoketest 會記錄實際收咗幾多。

完成之後將畫面上嘅 **SUMMARY** 截圖畀 Claude（唔好截 `.env`）。完整結果喺 `C:\btcperp\data\smoketest\`。

## 5. 回測（上實盤之前必做，詳情睇 BACKTEST.md）

1. 打 CONFIRM 之前，**你（批准人）**先睇過以下三個檔案（2026-09-30 你決定唔再等委員會）：
   - `config\backtest_criteria.yaml`（準則 draft-3：C0 至 C7，主要變體係方案 B `R4h_live`）；
   - `REVIEW_v1.3.0.md`（今次點樣處理委員會建議，包括 C0 同 BT3 兩處唔同）；
   - `BACKTEST.md`。
2. 你睇過、同意之後，雙擊 **`windows\Backtest.bat`**：
   1. 佢會下載 Binance 公開數據（第一次要幾分鐘），同埋 Polymarket 有嘅 1 小時 K 線；
   2. 第一次會要你打 **CONFIRM**。確認會鎖死：
      - 準則、config、日曆同程式；
      - 數據截止日同回測費率。
   3. 然後用低優先度跑。
3. 報告第一行會寫「Run #N under this confirmation」。**以第 1 次為準**，重跑唔會改變結果。
4. 將 `data\backtest\results_…\summary.md` 同 `summary.json` 畀 Claude。
5. 如果 `run` 話「NEEDS CONFIRMATION: changed since the confirmation」，唔好自己再 CONFIRM，先問 Claude。

- 回測唔會落單，亦唔會接觸你個戶口。
- 實盤用方案 B（`R4h_live`，每 4 個鐘）。回測亦會同時跑 v1.4 嘅每日版（`A_live`）做比較（I1）。其他變體要符合 BACKTEST.md 入面 S5 嘅四個條件，再經覆核，先可以取代佢。
- 回測之後，Claude 會出新版本 config，將 I6 嘅第 5 百分位寫入「實盤檢討線」（第 9 步）。

## 6. Dashboard

雙擊 **`windows\Dashboard.bat`**，瀏覽器會開 **http://127.0.0.1:8765**，只有呢部電腦睇到。
- 用 Dashboard.bat 開嘅話，黑色視窗要保持開住。
- 上實盤之後，dashboard 會喺你每次登入時自動喺背景行。

| 卡片 | 內容 |
|---|---|
| 頂部 | 狀態（空倉 / 持倉 / 暫停＋原因）、交易所讀取有冇失敗、版本；「從交易所更新」、「標記警報已讀」 |
| 權益 | 權益（交易所 total account value）、錢包、未實現盈虧、高水位 |
| 回撤 / 連虧 / 本金底線 | kill switch 同底線：25% 回撤平倉暫停；連虧 20% 停新倉；跌穿淨投入本金 75% 硬停；跌穿累計投入本金 50%（永久底線）平倉永久停 |
| 倉位 | 方向、數量、入場價、標記價、未實現盈虧、**止損 SL**（冇 SL 會紅字「無！」）、止盈 TP、強平價、累計資金費 |
| 最新決定 | 分數同三個組成部分、方向、注碼級別、閘門、行動、原因，同埋一段中文分析（每 4 個鐘更新；亦會彈一個簡短通知） |
| 其他 | 權益走勢、統計、交易紀錄、警報、排程及錯誤、經濟事件、影子追蹤 |

Dashboard 係**唯讀**，亦永遠唔會加交易掣。交易控制只用下面嘅 .bat，重要動作要打字確認。

## 7. 上實盤（GO）：全部 ✓ 先可以開始

- [ ] **回測**：第一次運行（Run #1），`R4h_live` 同 `R4h_live_stress` 都 PASS C0 至 C7。睇過報告、I1（4 小時對每日）同 I5 多空分拆。
- [ ] **心跳監察**（第 8 步）：decide 同 manage 兩個 check 設定好。三種情況都試過手機收到通知：熄機、卡住、嚴重警報。
- [ ] **升級還原測試**：`Upgrade.bat` 揀 `TEST-RESTORE` 做一次，見到 `RESTORE TEST PASSED`（第 10 步）。
- [ ] **Proxy key**：用第 3 步做，主錢包私鑰冇經過呢部電腦。寫低：用咗 **P（手機）**、**N（硬件錢包）** 定 **O（另一部電腦）**：______
- [ ] **Smoketest 揀 W** 全部 PASS：
  - 包括多、空、flip、bracket 部分被拒、兩種 SL 並存；
  - 「錯過 decide 補做平倉」由單元測試覆蓋（L1），smoketest 唔會刻意製造；
  - **提款探測被拒絕**；
  - 記錄咗真實 taker 手續費同 Polymarket／Binance 價差。
- [ ] **電腦設定**：永不睡眠、Windows Update 使用時段、更新後自動登入，並做過一次重新開機測試。
- [ ] **你書面確認**：
  1. proxy key 用 P、N 定 O；
  2. 永久底線：累計投入本金嘅 **5%**（2026-10-02 v1.7.0 由 50% 降低，你決定；只准 config 1.7.0 降一次）；
  3. 2026-10-02 起（v1.7.0）**孤注模式**：每次入場全部權益 ×19 倉位、**20 倍逐倉**；止賺 = 權益 ×2、止損 = 蝕權益約 70%（都已計手續費）；持倉期間唔反手、唔提早平倉，只等止賺或止損；回撤停機同連虧暫停 95%（等於冇）、本金底線 5%、回顧線關閉 —— **唔會自動停，你話停先停**（Pause.bat 或 Telegram /pause）。

全部 ✓ 之後，雙擊 **`windows\3_Schedule_Install.bat`**，打 `GO`。
- 如果最新嘅完整 smoketest 唔係呢個版本、唔係而家呢條 proxy key，或者冇 PASS，佢會拒絕。
- 佢會喺工作排程器開一個 `btcperp` 資料夾：

| 工作 | 時間（HKT） | 做咩 |
|---|---|---|
| decide × 12 | 00:30、04:30、08:30、12:30、16:30、20:30，每個再加 :50 重試 | 計分數、開倉／平倉／反手（每個時段嘅入場窗口係 HH:30 至 HH+1:30；過咗唔補入，但平倉規則照做） |
| manage × 6 | 02:30、06:30、10:30、14:30、18:30、22:30 | 對數、確保有 SL；如果嗰個時段嘅 decide 冇行到，就補做平倉規則；**唔會開新倉** |
| report_daily / weekly / monthly | 08:45 / 星期日 20:00 / 每月第一個星期日 20:30 | 報告 |
| backup | 每日 03:00 | 備份資料庫 |
| dashboard | 每次登入 | 背景 dashboard |

裝完雙擊 **`windows\Schedule_Check.bat`**，睇工作、下次執行時間同電源設定（有 WARNING 就要改）。

## 8. 通知同心跳監察

- 每個警報都會存入資料庫、喺 dashboard 顯示，同埋喺 bot 行完之後彈 **Windows 通知**，顯示為「Windows PowerShell」。
- 通知一次最多彈 5 個，其餘喺 dashboard。
- 冇通知彈出的話：設定 → 系統 → 通知 → 開啟，並容許「Windows PowerShell」；「勿打擾」開咗就會收埋。

**你已決定唔用 Telegram**，所以人唔喺電腦前面，要靠心跳監察通知你部手機。設定兩個 check：

1. 喺 https://healthchecks.io 開免費戶口，裝佢嘅手機 App（或者用 email 通知）。
2. **New Check** → 名 `btcperp decide` → Schedule 揀 **Cron**：
   - Cron expression：`30,50 0,4,8,12,16,20 * * *`
   - Time zone：`Asia/Hong_Kong`
   - Grace time：**20 分鐘**
3. **New Check** → 名 `btcperp manage` → **Cron**：
   - Cron expression：`30 2,6,10,14,18,22 * * *`
   - Time zone：`Asia/Hong_Kong`
   - Grace time：**20 分鐘**
4. 複製兩個 ping 網址（例如 `https://hc-ping.com/xxxxxxxx-…`）。雙擊 `windows\Edit_Secrets.bat`，填入：
   - `HEALTHCHECK_DECIDE_URL=`（decide 嗰個）
   - `HEALTHCHECK_MANAGE_URL=`（manage 嗰個）
   - 然後儲存。
5. 之後每次 decide / manage：
   - 開始時 ping `/start`；
   - 完成時 ping 成功，或者 `/fail`。唔會傳送任何資料。

以下情況 bot 會 ping **`/fail`**，你部手機會收到通知：
- 指令出錯；
- 有**未讀嘅嚴重警報**，例如：SL 補唔到、平倉失敗、kill switch、本金底線、永久底線、日曆過期、時鐘偏差、補做決定失敗、權益讀唔到、實盤檢討線、proxy key 就到期。
  - 喺 dashboard 撳「標記警報已讀」，或者行 `Alerts.bat`，下一次運行先會變返成功。
- 有 kill switch、底線或者檢討線生效：每次運行都會 `/fail`，直至你處理好。

另外兩種情況，healthchecks.io 自己會通知你：
- **電腦熄咗、登出咗、斷網**：到時候冇 ping，過咗 20 分鐘就通知。
- **卡住**：有 `/start` 但 20 分鐘內冇完成，就通知。

**上實盤前三個測試**（第 7 步）：
1. **熄機**：喺 manage 時間（例如 14:30）之前熄機，14:50 左右手機應該收到通知。之後開返機。
2. **卡住**：喺「命令提示字元」行 `curl.exe -fsS https://hc-ping.com/<manage 嗰個 uuid>/start`，然後乜都唔做。20 分鐘後手機應該收到通知。下一次 manage 會變返正常。
3. **嚴重警報**：行 `curl.exe -fsS https://hc-ping.com/<manage 嗰個 uuid>/fail`，手機應該即刻收到通知。bot 喺嚴重警報時 ping `/fail` 嘅邏輯，已經有單元測試。

## 9. 日常控制（`C:\btcperp\windows\`）

| 檔案 | 作用 |
|---|---|
| `Dashboard.bat` | 開 dashboard |
| `Status.bat` | 狀態、倉位、SL/TP、權益、kill switch、最後決定 |
| `Preview.bat` | **預覽**：如果而家決定，bot 會點做同點解（分數、閘門、入場、止損、止賺、注碼）。只用公開數據，**唔落單**，唔使 key |
| `Pause_New_Entries.bat` | **暫停**：唔再開新倉；現有倉位同 SL/TP 保留 |
| `Unpause.bat` | 只解除你嘅手動暫停（kill switch 同本金底線唔會解除；高水位同連虧計數不變） |
| `Kill_Close_Position.bat` | **即刻平倉**（reduce-only 市價）並暫停；要打 `KILL` |
| `Resume.bat` | 先列出所有暫停原因，打 `RESUME` 解除。如果係回撤或者連虧 kill switch，要再打 `RESET-PEAK`（高水位重設、連虧重新計） |
| `Alerts.bat` | 列出未讀警報 |
| `Report_Daily.bat` | 即刻出日報 |
| `Schedule_Check.bat` / `Schedule_Remove.bat` | 睇／移除排程 |
| `Proxy_Key.bat` | 整新 proxy key（到期前） |
| `Edit_Secrets.bat` | 用記事本改 `.env`（兩個心跳網址） |
| `Upgrade.bat` | 升級；`TEST-RESTORE` 做還原測試（第 10 步） |
| `Backtest.bat` | 回測 |

- **本金底線（EQUITY FLOOR，淨投入本金 75%）**：觸發之後會平倉。Resume 解除唔到，要新版本 config 寫明兩樣嘢先得：
  - 新嘅本金基數（唔可以高過當時權益）；
  - 觸發日期。

  每個觸發日期只可以用一次。即刻話 Claude 知。
- **永久底線（PERMANENT FLOOR，累計投入本金 50%）**：
  - 累計投入本金 = 第一次權益 + 之後所有入金 − 提款，永遠唔會重設。
  - 跌穿就平倉，並**永久停止**。
  - 要重開，一定要你親自決定，由 Claude 出新版本 config，寫明觸發日期（只用一次）。
  - 新 config 唔可以調低呢個百分比；調低嘅話，bot 會拒絕運行。
  - Resume 嘅訊息會顯示由第一次入金計起嘅累計盈虧。
- **實盤檢討線（LIVE REVIEW）**：
  - 回測之後由新版本 config 設定（I6 嘅第 5 百分位）。
  - 實盤滿 30 筆之後，如果最近 30 筆期望值低過呢條線，就停開新倉；現有倉位同 SL 保留。
  - 你同 Claude 檢討之後，先用 Resume。
- 所有 .bat 只可以喺 `C:\btcperp\windows\` 用。喺其他副本撳會被拒絕。
- 記錄檔喺 `C:\btcperp\logs\`（已遮蔽私鑰同 secret）。**永遠唔好傳 `.env`。**

## 10. 升級（收到新版本 zip）

避開決定時間（HKT 00:30、04:30、08:30、12:30、16:30、20:30，各自前後約半個鐘）。
1. 將新 zip 放喺「下載」，**唔使解壓**。
2. 雙擊 `windows\Pause_New_Entries.bat`。
3. 雙擊 `windows\Upgrade.bat`。
   - 佢會顯示 zip 嘅 **SHA-256**。要同 Claude 發佈時畀你嘅數值一樣，唔同就唔好繼續。
   - 打 `UPGRADE`。佢會：
     1. 停排程；如果讀唔到工作排程器，就乜都唔改；
     2. 等行緊嘅指令完成；
     3. 備份現有版本去 `data\upgrade_backup_<舊版本>_<時間>\`；
     4. 換檔、裝套件、行測試；
     5. 全部通過先重開排程。
   - **任何一步失敗都會自動還原舊版本，並重開排程**，畫面會寫 `UPGRADE FAILED - version X was restored`。
   - 如果連還原都失敗，排程會保持停止，畫面會用 `!!!` 顯示倉位同 SL。即刻話 Claude 知。
   - 版本號唔高過現有版本嘅 zip 會被拒絕。
4. 睇 `Status.bat` 同 dashboard，冇問題就雙擊 **`Unpause.bat`**。注意係 Unpause，**唔好用 Resume**。
5. 得閒再做一次 smoketest。

**還原測試**（上實盤前做一次，第 7 步）：
1. 將**同一個版本**嘅 zip 放喺「下載」。
2. 雙擊 `Upgrade.bat`，打 `TEST-RESTORE`。佢會：
   1. 裝一次個 zip；
   2. 扮失敗；
   3. 還原返原本版本。
3. 最後要見到 **`RESTORE TEST PASSED`**。

## 11. 電腦熄咗、瞓咗、斷網，或者你長時間唔喺度

- 交易所上面嘅 **SL / TP 單照樣有效**。
- 錯過某個時段嘅 HH:30 同 HH:50 嘅話，嗰個時段唔會開新倉。之後嘅 manage 會用嗰個時段嘅數據**補做平倉規則**（反向訊號、3 日規則、資金費規則），但唔會補入場。下一個時段照常決定。
- 電腦熄咗嗰段時間，kill switch 同所有規則都唔會執行，只有 SL / TP 保護。
- **預計離開超過 24 小時**：出門前雙擊 `Pause_New_Entries.bat`，返嚟再 `Unpause.bat`。
- **超過 72 小時，或者未設定心跳監察**：出門前雙擊 `Kill_Close_Position.bat` 平倉。
- 心跳監察會通知你「bot 停咗」或者「有嚴重警報」，但唔會通知每一筆交易。

## 12. 每月檢討

- 每月第一個星期日 20:30 會出月報：`data\reports\monthly\monthly_YYYY-MM.md` 同 `.json`。
- 如果一個月有超過 12 個 4 小時時段（即 2 日）冇準時做決定，月報會標「INCOMPLETE MONTH」，嗰個月唔應該用嚟評估策略。
- 將月報畀 Claude，佢會用繁體中文寫檢討，並提出改動建議（每項都要有數據）。
- 任何改動都會係一個新版本 zip，由你決定裝唔裝。

## 13. 參考

- 結束代碼：0 成功、1 錯誤、3 設定／secret 錯誤或者用錯資料夾、4 另一個指令行緊、5 單元測試失敗、6 要額外確認。
- 參數：`config\config.yaml`。詳情：`README.md`、`BACKTEST.md`、`API_NOTES.md`、`REVIEW_v1.2.0.md` 同 `REVIEW_v1.3.0.md`（委員會建議點處理）。
- 入金／提款時間測試（只喺空倉時做）：喺 `C:\btcperp` 開「命令提示字元」，行
  `venv\Scripts\python.exe run.py flowwatch --minutes 30`，同時入一筆小額，然後將結果檔路徑話畀 Claude 知。
