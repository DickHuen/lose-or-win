# START_HERE：喺你部 Windows 電腦行 btcperp

呢個 bot 會用真錢，喺 Polymarket Perps 自動交易 BTC-PERP。佢喺你自己部 Windows 電腦上面行：
由「工作排程器」（Task Scheduler）定時啟動，喺瀏覽器 dashboard 睇狀態，有交易或者有事就彈 Windows 通知。
所有時間都係香港時間（HKT, UTC+8），除非寫明 UTC。

> 重要規則（一直有效）
> - 主錢包私鑰永遠唔好放入 `.env`，亦唔好畀任何人。bot 只用 Polymarket Perps 嘅 **proxy key**。
> - 唔好用 VPN 或者 proxy 去繞過地區限制。
> - 唔好喺同一個 Polymarket Perps 戶口手動落單：bot 會當係佢唔認識嘅倉，接管或者平倉。
> - 持倉期間唔好入金或者提款。
> - 唔好改 `perpbot\`、`config\` 入面任何檔案。要改嘢就搵 Claude 出新版本 zip（版本號會升，CHANGELOG 會寫低）。
> - `.env` 同佢嘅截圖永遠唔好傳畀任何人（包括 Claude）。

---

## 1. 準備部電腦（只做一次）

1. **安裝 Python 3.12**：去 https://www.python.org/downloads/windows/ 下載 「Windows installer (64-bit)」。
   安裝第一版**記得剔「Add python.exe to PATH」**，然後撳「Install Now」。
2. **時區**：設定 → 時間與語言 → 日期與時間 → 時區揀「(UTC+08:00) 香港特別行政區」，並開「自動設定時間」。
   （唔係香港時區都行得，bot 會自動換算，但建議用香港時區。）
3. **電源**（好重要，電腦瞓咗 bot 就唔會行）：
   - 最簡單：設定 → 系統 → 電源 → 「插電時，在此時間後讓裝置進入睡眠狀態」揀 **永不**。
   - 如果一定要瞓：控制台 → 電源選項 → 變更計劃設定 → 變更進階電源設定 → 睡眠 →
     **允許喚醒計時器 → 啟用**。排程設定咗「喚醒電腦執行」。
   - Notebook：插住電，「合上蓋時」揀「不執行任何動作」。
4. **保持登入**：bot 只會喺你登入咗 Windows 嘅時候行。鎖螢幕（Win + L）冇問題；**登出或者關機就唔會行**。
   Windows Update 自動重新開機之後，要登入一次 bot 先會繼續（建議喺 Windows Update 設定「使用時段」）。
5. **網絡**：正常家用網絡。唔好開 VPN / proxy。

## 2. 安裝（或者升級）

1. 將 `btcperp_vX.Y.Z.zip` 放喺「下載」。**先右撳個 zip → 內容 → 剔「解除封鎖」→ 確定**
   （唔剔的話，之後每次撳 .bat 都可能彈安全警告）。
2. 右撳 zip → 「解壓縮全部」→ 目的地打 `C:\` → 解壓縮。
   完成之後應該有 `C:\btcperp\run.py` 同 `C:\btcperp\windows\` 呢個資料夾。
3. 開 `C:\btcperp\windows\`，雙擊 **`1_Install.bat`**。
   佢會建立 `venv`、安裝指定版本嘅套件，再行單元測試。最後見到 **`INSTALL PASS (btcperp v…)`** 就得。
   如果見到 `INSTALL FAIL`，將個畫面截圖畀 Claude。

## 3. 填 proxy key（`.env`）

雙擊 **`windows\Edit_Secrets.bat`**，會用記事本開 `C:\btcperp\.env`。要填四個值（格式係 `名=值`，一行一個）：

| 名 | 係咩 |
|---|---|
| `PM_PROXY_PRIVATE_KEY` | Polymarket Perps **proxy signer** 嘅私鑰（0x 加 64 個字） |
| `PM_PROXY_SECRET` | 開 proxy 嗰陣一齊攞到嘅 API secret |
| `PM_WALLET_ADDRESS` | 你主錢包嘅**地址**（公開地址，唔係私鑰） |
| `PM_PROXY_EXPIRES_AT` | proxy 到期時間（例如 `2026-10-26T00:00:00Z`，可以留空） |

撳 Ctrl + S 儲存，然後關記事本。**Proxy key 點樣整：裝好之後問 Claude，會一步步教你。**
Proxy key 到期前 5 日 bot 會開始提你換。

## 4. Smoketest（上實盤之前必做）

雙擊 **`windows\2_Smoketest.bat`**：
- 打 `YES`：完整測試，用最細注碼落**真單**。會開同平幾個好細嘅倉（多、空、flip），落單再取消，
  亦會試一次 bracket 止損。會用少少手續費。
- 打 `R`：只做唯讀檢查（價錢、地區、key、戶口），唔落單。

完成之後將畫面上嘅 **SUMMARY** 截圖畀 Claude（唔好截 `.env`）。完整結果喺 `C:\btcperp\data\smoketest\`。
Smoketest 未 PASS 之前唔好做第 6 步。

## 5. Dashboard

雙擊 **`windows\Dashboard.bat`**，瀏覽器會開 **http://127.0.0.1:8765**。只有呢部電腦睇到。
用 Dashboard.bat 開嘅話，黑色視窗要保持開住，關咗佢 dashboard 就停。做完第 6 步之後，
dashboard 會喺你每次登入時自動喺背景行，雙擊 Dashboard.bat 就只會開瀏覽器。

| 卡片 | 內容 |
|---|---|
| 頂部 | 狀態（空倉 / 持倉 / 暫停＋原因）、更新時間、版本；「從交易所更新」、「標記警報已讀」 |
| 權益 | 權益（交易所 total account value）、錢包、未實現盈虧、高水位 |
| 回撤 / 連虧 / 本金底線 | 三個 kill switch 同佢哋嘅界線（15% 回撤平倉暫停；連虧 8% 停新倉；跌穿淨投入本金 75% 硬停） |
| 倉位 | 方向、數量、入場價、標記價、未實現盈虧、**止損 SL**（冇 SL 會紅字「無！」）、止盈 TP、強平價、累計資金費 |
| 最新決定 | 日期、分數同三個組成部分（趨勢、突破、收市位置）、方向、注碼級別、閘門（有觸發會打 ✓）、行動、原因 |
| 權益走勢 | 權益（實線）同高水位（虛線） |
| 統計 | 交易數、勝率、淨盈虧、總 R、期望值、手續費、資金費、平均持倉時間 |
| 交易紀錄 | 每筆已平倉交易：入場日、方向、入場價、出場價、原因（TP / SL / flip…）、淨盈虧、R、持倉小時 |
| 警報 | 所有警報，未讀嘅用粗體 |
| 排程及錯誤 | 每個指令最後一次完成時間同結果、48 小時內漏跑或遲跑、7 日內錯誤 |
| 經濟事件 / 影子追蹤 | 30 日內 FOMC / CPI / NFP、日曆到期提示；影子版本嘅模擬結果 |

Dashboard 係**唯讀**：佢唔會落單。「從交易所更新」只會讀交易所（`snapshot`），開住頁面時每 5 分鐘自動讀一次。
交易控制只用下面嘅 .bat。

## 6. 上實盤（GO）

Smoketest PASS、你決定開始之後，雙擊 **`windows\3_Schedule_Install.bat`**，打 `GO`。佢會喺工作排程器
開一個叫 `btcperp` 嘅資料夾，加入以下工作：

| 工作 | 時間（HKT） | 做咩 |
|---|---|---|
| decide_0830、decide_0850 | 每日 08:30、08:50 | 計分數、開倉 / 平倉 / flip（入場窗口 08:30–09:30，過咗唔補入） |
| manage_1230 / 1630 / 2030 / 0030 / 0430 | 每日 5 次 | 對數、確保有 SL、完成未做完嘅平倉；**唔會開新倉** |
| report_daily | 每日 08:45 | 日報（`data\reports\`） |
| report_weekly | 星期日 20:00 | 週報同所有紀錄嘅 CSV zip |
| report_monthly | 每月第一個星期日 20:30 | 月報 |
| backup | 每日 03:00 | 備份資料庫（`data\backups\`） |
| dashboard | 每次登入 | 背景 dashboard |

裝完雙擊 **`windows\Schedule_Check.bat`** 睇吓全部工作都喺度，同埋下次執行時間。
想停止自動交易：`windows\Schedule_Remove.bat`（倉位同交易所上面嘅 SL/TP 會留住）。

## 7. 通知

每個警報都會存入資料庫、喺 dashboard 顯示，同埋彈 **Windows 通知**（顯示為「Windows PowerShell」）：
每次開倉、平倉、flip、SL/TP 改動、kill switch、警告、入場被擋、漏跑 08:30、SL 補唔到、平倉失敗、
proxy key 就到期，同所有錯誤。

- 如果冇通知彈出：設定 → 系統 → 通知 → 開啟通知，並容許「Windows PowerShell」；
  「勿打擾」/「專注輔助」開咗就會收埋。
- 通知只會喺呢部電腦彈。人唔喺電腦前面嘅話，返嚟睇 dashboard 嘅「警報」，
  或者雙擊 `windows\Alerts.bat`（列出未讀警報，然後標記已讀）。

## 8. 日常控制（`C:\btcperp\windows\`）

| 檔案 | 作用 |
|---|---|
| `Dashboard.bat` | 開 dashboard |
| `Status.bat` | 狀態、倉位、SL/TP、權益、kill switch、最後決定 |
| `Pause_New_Entries.bat` | **暫停**：唔再開新倉；現有倉位同 SL/TP 保留 |
| `Kill_Close_Position.bat` | **即刻平倉**（reduce-only 市價）並暫停；要打 `KILL` 確認 |
| `Resume.bat` | 解除暫停 / kill switch（要打 `RESUME` 確認）；回撤高水位重設，連虧重新計 |
| `Alerts.bat` | 列出未讀警報 |
| `Report_Daily.bat` | 即刻出一份日報 |
| `Schedule_Check.bat` | 睇排程工作 |
| `Schedule_Remove.bat` | 移除排程（停止自動交易） |
| `Edit_Secrets.bat` | 用記事本改 `.env`（換 proxy key 時用） |

- **本金底線（EQUITY FLOOR）**觸發之後，`Resume.bat` 都解除唔到，只有新版本 config 先可以。即刻話 Claude 知。
- 任何錯誤：dashboard「排程及錯誤」會顯示，亦會彈通知。記錄檔喺 `C:\btcperp\logs\btcperp_YYYY-MM-DD.log`
  （已遮蔽私鑰同 secret），有需要可以畀 Claude 睇。**永遠唔好傳 `.env`。**

## 9. 電腦熄咗、瞓咗或者斷網會點？

- 交易所上面嘅 **SL / TP 單照樣有效**：你部電腦熄咗，倉位都有止損保護。
- 08:30 同 08:50 都行唔到嘅話，當日唔會開新倉。下次再行嘅時候會出「missed」警報，**唔會補入場**。
- 錯過 manage 冇問題：下一次 manage / decide 會先對數（reconcile），將期間 TP / SL 觸發嘅平倉記錄返。
- 但係電腦熄咗嗰段時間，flip、3 日規則、資金費規則同 kill switch 都**唔會執行**，只有 SL / TP 保護。
  所以盡量保持電腦開住、登入咗、有網絡。

## 10. 升級（收到新版本 zip）

避開 08:20–09:35 HKT（decide 時段）。

1. 雙擊 `windows\Pause_New_Entries.bat`。
2. 新 zip 同樣先「解除封鎖」，然後「解壓縮全部」去 `C:\`，揀**取代**所有檔案。
   （`data\`、`logs\`、`venv\`、`.env` 唔喺 zip 入面，唔會被改動。）
3. 雙擊 `windows\1_Install.bat`，要見到 `INSTALL PASS`。
4. 如果新版本嘅 `CHANGELOG.md` 話要重新裝排程，就雙擊 `windows\3_Schedule_Install.bat` 再打 `GO`。
5. 雙擊 `windows\Status.bat` 同開 dashboard 檢查，冇問題先雙擊 `windows\Resume.bat`。

## 11. 每月檢討

每月第一個星期日 20:30 會出月報：`C:\btcperp\data\reports\monthly\monthly_YYYY-MM.md`（同一個 `.json`）。
將兩個檔案畀 Claude，佢會用繁體中文寫檢討，內容包括：
- 整體表現，同埋按分數級別、閘門、出場原因、多空分開嘅表現
- MAE / MFE 對比 SL / TP 距離、滑價、資金費成本
- 影子追蹤對比實盤
- 漏跑、錯誤、日曆提示
- 建議嘅改動（每項都要有數據支持）

每個版本嘅數字分開計，唔會混埋。任何改動都會係一個新版本 zip，由你決定裝唔裝。

## 12. 參考

- 結束代碼：0 成功、1 錯誤、3 設定或 secret 錯誤、4 另一個指令仲行緊、5 單元測試失敗。
- 所有參數：`config\config.yaml`。策略、指令同資料詳情：`README.md`。交易所 API 筆記：`API_NOTES.md`。
- 入金 / 提款時間測試（只喺空倉時做）：喺 `C:\btcperp` 開「命令提示字元」，行
  `venv\Scripts\python.exe run.py flowwatch --minutes 30`，同時入一筆小額，然後將佢印出嘅結果檔路徑話畀 Claude 知。
