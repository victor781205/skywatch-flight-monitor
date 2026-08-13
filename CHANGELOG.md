# Changelog

SkyWatch 的重要變更記錄於此。版本編號遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

- 尚無。

## [3.0.0] - 2026-08-13

### Added

- 重新設計為一致淺色主題的八頁響應式營運介面：營運總覽、搜尋與比較、監控策略、價格情報、執行與通知、系統設定、備份與復原、使用說明。
- 每筆監控可個別設定固定時間、每日或間隔排程，以及僅達標、降價、每次回報或摘要通知策略與安靜時段。
- 精簡／標準／完整額度模式，單次執行分別以最多 1／2／5 次 SerpAPI 請求為保守預算。
- 航空公司專屬目標價、直飛、最大轉機數、去回程時段、計價基準與同航空公司來回條件。
- scheduler heartbeat、漏跑偵測、自動補跑、異常通知與每筆監控的下次執行時間。
- 持久化 `check_runs` 執行紀錄，包含狀態、API 用量與失敗原因。
- 持久化通知 outbox，支援 lease 回收、退避重試、人工重送及重新啟動後續送。
- Telegram long-polling 指令服務：allowlisted `/status`、`/quota`、`/check`；`/check` 使用短效一次性確認碼與持久化 update offset。
- Travelpayouts 快取 Data API 低價月曆，提供彈性日期市場參考，且不參與通知判定。
- SQLite snapshot、監控 JSON 匯出／匯入、ZIP 白名單驗證、hash 驗證、restore point 與路徑穿越防護。

### Changed

- 來回票價與航段明確分開呈現：主要搜尋可提供來回總價，但回程明細依額度模式補查。
- 標準模式補查最佳方案回程；完整模式最多再補查長榮、華航與星宇各自的回程，因此三大國籍航空完整去回程比較僅由完整模式保證嘗試取得。
- 所有已嘗試的供應商請求都納入保守額度計數，並顯示月用量、警戒與排程預估。
- 全域排程只執行使用全域設定的監控；個別排程由監督流程依各自到期時段執行，避免重複查價。
- 通知防重改為執行時段 claim、outbox lease 與每通道完整 payload fingerprint 的多層防護。
- 備份介面明確標示只保存應用資料，不會包含 `.env`、API keys、Telegram token、Discord webhook 或其他憑證。
- 移除無法取得個人正式憑證的 Amadeus runtime fallback。

### Fixed

- 修正 00:00／12:00 排程可能漏發、重複送達或因程序重啟遺失的問題。
- 修正 Telegram 報價只顯示去程，以及國籍航空回程未依模式補查的問題。
- 修正手動重試未重設嘗試次數、意外發送例外未返回重試佇列，以及過期 `sending` 工作無法回收的問題。
- 修正跨程序同時執行造成重複 API 花費與重複通知的競態條件。
- 修正新建或改排程監控誤補策略生效前時段，以及崩潰後 `running` 執行紀錄永久阻擋補跑的問題。
- 修正 Windows scheduler 啟動器覆蓋 `.env` 自訂固定時間或間隔排程的問題。
- 修正睡眠喚醒後 cron misfire 與漏跑監督同時派送不同時段，造成重複查價與 Telegram 回報的問題。
- Telegram 指令服務新增獨立 heartbeat watchdog；scheduler 整個停止或恢復時會主動通知，`/status` 也不再把陳舊 heartbeat 顯示為正常。

### Security

- Telegram 指令增加 chat allowlist、單例鎖、一次性確認 token 與 anti-replay 保護。
- 備份及復原加入白名單、路徑與 checksum 驗證；機密設定永不進入匯出檔。

## [2.4.0] - 2026-08-11

### Added

- 本機每月 API 使用量、供應商限制與 80% 一次性預警。
- 系統設定頁的排程月用量估算。
- Travelpayouts 快取市場資料的初始整合。

### Changed

- 將網頁程序與正式 scheduler 分離，並持續強化查價與通知的跨程序防重。
- 停用無法供專案維護者取得正式憑證的 Amadeus fallback。

## [0.2.3] - 2026-08-11

### Added

- Asia/Taipei scheduler 時區與 machine-readable heartbeat。
- Telegram／Discord 的每通道精確訊息 fingerprint。

### Fixed

- 防止多個程序對同一筆監控送出重複通知。
- 在 UI 顯示最近通知與下次排程。

## [0.2.2] - 2026-08-10

- 將 web 與 production scheduler 分離。
- 加入跨程序 job lock 與通知 claim。
