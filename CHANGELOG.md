# Changelog

## 0.3.0 — 2026-06-12

- **詞彙表 priming 改 token 預算制**：whisper-1 只保留 prompt 最後 224 tokens，舊版切段模式下前段結尾（300 字）會把詞彙表整個擠掉。現在詞彙表保證完整保留，前段結尾依剩餘預算裁切。
- **所有 pyannote HTTP 呼叫加 timeout**（JSON 30s、上傳 300s），上傳遇網路層錯誤重試一次——舊版單一請求卡住會永久 hang。
- **`pyannote_voiceprint.py` 新增 `--labels` 模式**：直接讀 label_server 的 labels.json 對應 clips 建立聲紋，取代 voiceprint-setup skill 裡手動 cp 改名的步驟。
- **標記介面驗證名字**：格式（英文字母開頭，限 `A-Za-z0-9 . _ -`）與大小寫不敏感的重複檢查；`--labels` 端同樣驗證作為最終防線。
- **新增 `scripts/_common.py`**：HTTP、上傳、poll、prompt 組合等共用邏輯收斂，消除四份 script 間的重複與 drift。

## 0.2.0 — 2026-06-12

- diarization 與 ASR 合併改採 pyannote 官方 segment-level max-overlap 邏輯；無語音區段的 ASR 幻覺標注供清理。
- 切段 offset 用實際 chunk 長度累加；identify job 與 whisper 並行；curl 網路層錯誤重試；API key 走 stdin 不進 argv。
- 時間戳統一 HH:MM:SS；`pyannote_upload.py` 清理。
- 語言自動偵測（混雜中英不傳 `--language`）；空聲紋庫 guard；錯誤訊息更友善。

## 0.1.0 — 2026-06-11

- 初版：`meeting-notes`（錄音 → 聲紋識別 speaker-labeled 逐字稿 → 結構化會議記錄）與 `voiceprint-setup`（diarization 切樣本 → 本地網頁標記 → 建立團隊聲紋庫）兩個 skill。
