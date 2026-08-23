# SkyWatch Flight Monitor

[![Tests](https://github.com/victor781205/skywatch-flight-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/victor781205/skywatch-flight-monitor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-3B82F6.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-2563EB.svg)](https://www.python.org/)

SkyWatch 是一套 local-first、可自行託管的航班票價監控工具。它以 Streamlit、SQLite、SerpAPI 與選用的 Travelpayouts Data API 組成，提供排程查價、歷史紀錄、Telegram／Discord 通知及可恢復的營運紀錄。

> SkyWatch 是獨立開源專案。票價與航班資料可能延遲、不完整或不正確；訂票前請務必在航空公司或訂位服務商頁面確認最終行程、票價、行李與退改條件。

## 3.1.1 重點功能

- 統一淺色主題的響應式營運介面，不混用深色元件。
- 搜尋單程與來回票價，並在 SQLite 保留查價歷史。
- 每筆監控可獨立設定目標價、航空公司規則、直飛／轉機、時段、排程與通知策略。
- 精簡／標準／完整三種額度模式，單次執行最多分別使用 1／2／5 次 SerpAPI 請求。
- 固定時間、每日或間隔排程；監督巡檢對齊時鐘刻度，啟動即檢查漏跑並自動補跑。
- Telegram 指令服務支援 `/status`、`/quota`、`/check`；手動查價需短效確認碼。
- Travelpayouts 彈性日期低價月曆，明確標示為快取市場參考，不作為監控觸發依據。
- 查價執行紀錄、失敗原因、通知 outbox、持久化重試及人工重送。
- 完整資料備份、匯入與復原；備份刻意排除 `.env`、API keys、Telegram token 等憑證。

## 介面

應用程式包含八個頁面：

1. 營運總覽
2. 搜尋與比較
3. 監控策略
4. 價格情報
5. 執行與通知
6. 系統設定
7. 備份與復原
8. 使用說明

桌面版使用固定導覽與營運摘要；小尺寸畫面會改為單欄卡片、可水平捲動的導覽及適合觸控的操作區。

## 查價額度模式

每筆監控可選擇下列模式。上限是保守預算，實際請求數可能較少。

| 模式 | 每次最多請求 | 用途 |
| --- | ---: | --- |
| 精簡 | 1 | 取得主要搜尋結果與來回總價，最節省免費額度 |
| 標準 | 2 | 另補最佳方案的回程航班明細 |
| 完整 | 5 | 另補最佳方案，以及長榮、華航、星宇各自的回程明細 |

SerpAPI 的第一筆來回搜尋可提供總價，但不一定包含可展示的回程航段。三大國籍航空要各自顯示完整去回程，監控必須使用「完整」模式；若某家航空未出現在主要結果、沒有有效 token，或供應商未回傳回程資料，介面與通知會明確顯示資料缺口，不會臆造航班。

系統會記錄所有已嘗試的外部請求、估算本月用量並提供配額預警。提高頻率、增加監控數量或使用完整模式前，請先核對供應商方案限制。

## 排程與通知

監控可使用固定時間、每日或間隔排程，時區為 Asia/Taipei。所有排程只有一個持久化監督流程負責派送，電腦睡眠後仍以原定時段補跑，不會另以喚醒時間重複查價。scheduler 會寫入 heartbeat，並由監督流程比對每筆監控的到期時間；發現漏跑時建立執行紀錄並在安全條件下補跑。新建或改排程只從策略生效時間起算；若程序在執行中崩潰，逾期的 run lease 會被安全回收並補跑。

通知先寫入 SQLite outbox，再由發送器認領。暫時性錯誤會依排程重試；逾期的 `sending` lease 可被回收，手動重試會重新排入。重複防護包含程序鎖、執行時段 claim 與訊息 fingerprint，避免多程序或 Telegram redelivery 重複花費額度及送出相同訊息。

通知策略可設為僅達標、降價、每次回報或摘要，並可設定安靜時段。安靜時段中的訊息會延後至可發送時間，不會直接遺失。

## Telegram 指令服務

先在 `.env` 設定 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 或 `TELEGRAM_ALLOWED_CHAT_IDS`，再另外啟動：

```bash
python telegram_bot.py
```

可用指令：

- `/status`：顯示 scheduler heartbeat 與監控狀態。
- `/quota`：顯示本月使用量與預估。
- `/check`：先回報保守成本並產生短效一次性確認碼；確認後才執行全部查價。

服務會在執行 update 前持久化 Telegram offset，並使用 allowlist、單例鎖與退避重試。私人聊天室會驗證 chat ID；群組與超級群組會同時驗證 chat ID 及 `TELEGRAM_ALLOWED_USER_IDS`，避免群組內其他成員執行管理指令。它也會獨立監測 scheduler heartbeat：超過設定時間未更新會主動發送異常通知，恢復後再回報；每個允許的 chat 都會分別記錄送達，暫時性失敗只重送尚未成功的收件人。因此即使 scheduler 整個停止也能告警。Windows 可使用 `start_telegram_bot.bat` 或 `start_telegram_bot_silent.vbs`。

## Flight-data providers

SerpAPI 是即時搜尋的主要來源。Travelpayouts 僅使用其允許存取的快取 Data API，提供彈性日期低價月曆及市場背景；不使用受限制的即時 Search API，也不以快取價格觸發通知。數字比較僅用於相容的單人經濟艙條件，避免把不同票價基礎誤當成同一商品。

使用任何第三方 API 前，請確認帳號資格、配額、資料授權、顯示規範及服務條款。供應商資料與功能可能隨時變更。

## Requirements

- Python 3.10 或更新版本
- 用於即時航班搜尋的 [SerpAPI](https://serpapi.com/) API key
- 選用的 Travelpayouts Data API token
- 選用的 Telegram bot credentials 或 Discord webhook

## Quick start

```bash
git clone https://github.com/victor781205/skywatch-flight-monitor.git
cd skywatch-flight-monitor
python -m venv .venv
```

啟用虛擬環境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

安裝相依套件並建立設定檔：

```bash
pip install --require-hashes -r requirements.lock
cp .env.example .env
```

Windows Command Prompt 請以 `copy .env.example .env` 取代 `cp`。

編輯 `.env`：

```env
SERPAPI_API_KEY=
TRAVELPAYOUTS_TOKEN=
SERPAPI_MONTHLY_LIMIT=250
API_USAGE_WARNING_PERCENT=80

DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# 多個允許的 chat ID 可使用逗號分隔；設定後會覆蓋單一 TELEGRAM_CHAT_ID allowlist
TELEGRAM_ALLOWED_CHAT_IDS=123456789,-987654321
# 群組／超級群組必填，可下指令的 Telegram user ID
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_COMMANDS_ENABLED=true

ALWAYS_NOTIFY=true
SCHEDULE_HOURS=0,12
CHECK_INTERVAL_MINUTES=
SCHEDULER_SHORT_RETRIES=1
SCHEDULER_RETRY_DELAY_SECONDS=30
AUTO_BACKUP_ENABLED=true
AUTO_BACKUP_HOUR=03:30
AUTO_BACKUP_RETENTION_DAYS=14
```

啟動網頁應用：

```bash
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

開啟 <http://127.0.0.1:8501/>。在另一個終端啟動獨立 scheduler：

```bash
python scheduler.py
```

Windows 也可使用 `start_skywatch.bat`、`start_scheduler.bat` 及對應的靜默 `.vbs` 啟動器。Web 程序不會自行啟動第二份 scheduler。

要讓三個服務在登入 Windows 後自動啟動，於 PowerShell 執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_tasks.ps1
```

安裝器會註冊 Web、scheduler 與 Telegram bot 三個使用者層級工作，限制同一服務只能有一個實例，異常退出後自動重啟。

## 備份與復原

備份 ZIP 只包含應用資料白名單，例如 SQLite snapshot 與監控設定 JSON；不包含 `.env`、API key、bot token、webhook 或其他整合憑證。匯入支援跳過、更新或複製衝突監控。復原前會建立 restore point、檢查 hash 與路徑安全，且操作介面要求明確確認。

復原流程會取得 maintenance lock、拒絕仍有活躍 worker 的操作、驗證 schema 與 hash、先建立 restore point，並將復原檔內未完成的 run、lease、配額保留及通知派送安全隔離。備份檔可能包含個人旅行偏好與價格紀錄，請視為敏感資料妥善保存。

## Airport data

第一次搜尋機場時，系統會從 [OurAirports](https://ourairports.com/data/) 下載並建立本機索引。也可手動更新：

```bash
python scripts/download_airports.py
```

產生的機場索引不會提交至 Git。OurAirports 將資料標示為 Public Domain，且不保證正確性或適用性；詳見 [Third-Party Notices](THIRD_PARTY_NOTICES.md)。

## Testing

```bash
pip install --require-hashes -r requirements-dev.lock
python -m ruff check .
python -m unittest discover -s tests -v
python -m coverage run -m unittest discover -s tests -v
python -m coverage report --fail-under=55 --show-missing
python -m compileall -q app.py scheduler.py telegram_bot.py src tests
```

自動化測試不會發送真實通知。手動工具 `test_telegram.py` 必須由操作人輸入 `SEND` 才會送出網路請求。

## Security and privacy

- 絕勿提交 `.env`、credentials、`data/*.db`、logs 或匯出的使用者資料。
- 任何曾公開、貼到 issue、聊天或截圖中的 token 都應立即撤銷並輪替。
- 預設只綁定 loopback，且不含多使用者驗證；對外開放前必須加入合適的身分驗證、TLS 與網路存取控制。
- Telegram 指令只允許 allowlist 中的 chat ID；不要將管理型 bot 加入不受信任的群組。
- 安全問題請使用 GitHub private vulnerability reporting；詳見 [Security Policy](SECURITY.md)。

完整資料流見 [Architecture](docs/ARCHITECTURE.md)。

## Project structure

```text
skywatch-flight-monitor/
├── .github/                 # CI、issue forms、PR template、ownership
├── docs/                    # Architecture 與 release 文件
├── scripts/                 # 資料維護工具
├── src/                     # 核心應用模組
├── tests/                   # 單元測試
├── app.py                   # Streamlit UI
├── scheduler.py             # 排程與漏跑補跑服務
├── telegram_bot.py          # Telegram 指令 long-polling 服務
├── style.css                # 統一淺色設計系統
├── requirements.txt         # 可讀的直接相依版本範圍
├── requirements.lock        # 可重現、含 hash 的正式環境鎖檔
└── requirements-dev.lock    # 可重現、含 hash 的開發／CI 鎖檔
```

## Contributing

請閱讀 [CONTRIBUTING.md](CONTRIBUTING.md)。較大的變更先開 issue，pull request 保持單一焦點；專案互動遵循 [Code of Conduct](CODE_OF_CONDUCT.md)。

## Maintainer

[Victor (@victor781205)](https://github.com/victor781205)

## License

SkyWatch 原始碼採用 [MIT License](LICENSE)。第三方服務與下載資料仍受各自條款與 notices 約束。
