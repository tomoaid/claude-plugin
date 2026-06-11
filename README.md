# TomoAid Meeting Tools

把會議錄音變成「知道誰說了什麼」的結構化會議記錄，全程在 Claude Code 裡完成。

兩個 skill：

| Skill | 做什麼 |
|-------|--------|
| `/tomoaid:meeting-notes` | 錄音 → 聲紋識別 speaker-labeled 逐字稿 → 清理 → 結構化會議記錄 .md（含 Action Items），產出到本地檔案為止 |
| `/tomoaid:voiceprint-setup` | 用一段多人會議錄音建立團隊聲紋庫：diarization 自動切出每位 speaker 的樣本 → 本地網頁試聽標記人名 → 寫入聲紋庫 |

## 特色

- **具名 speaker**：用 pyannote.ai voiceprint 比對，逐字稿直接標 `**Alice** [00:12–00:34]`，不是匿名的 SPEAKER_00
- **Word-level 對齊**：一句話橫跨兩位講者時在字邊界切開；無語音區段的 ASR 幻覺會被標注供清理
- **詞彙表 priming**：人名、產品名、術語透過 prompt 餵給 ASR，大幅減少專有名詞聽錯；用繁體撰寫同時把輸出偏向繁體
- **長音檔自動切段**：超過 API 上限自動切段轉錄，段與段之間傳遞上下文
- **不碰 git**：產出只到本地檔案，commit / PR 由你自己決定

## 安裝

```
/plugin marketplace add tomoaid/claude-plugin
/plugin install tomoaid@tomoaid
```

### 需求

- `OPENAI_API_KEY`（whisper-1 / gpt-4o-transcribe 轉錄）
- `PYANNOTEAI_API_KEY`（diarization 與 voiceprint，https://pyannote.ai）
- `ffmpeg` / `ffprobe`（`brew install ffmpeg`）
- Python 3.10+（僅用標準庫，無需 pip install）

## 使用

第一次先建聲紋庫（拿一段大家都有發言的會議錄音）：

```
/tomoaid:voiceprint-setup ./recordings/last-meeting.m4a
```

瀏覽器會開一個標記頁面，逐段試聽、填上成員英文名、儲存。之後整理會議就一句話：

```
/tomoaid:meeting-notes ./recordings/2026-06-11.m4a
```

產出三個檔案：`meetings/<date>-<topic>.md`（會議記錄）、`meetings/transcripts/`（清理後逐字稿）、`meetings/recordings/`（音檔，自動 gitignore）。

## 設定（選填）

預設路徑：聲紋庫 `.tomoaid/voiceprints.json`、詞彙表 `.tomoaid/asr-glossary.md`、輸出 `meetings/`。要改就在專案根目錄放 `.tomoaid.json`：

```json
{
  "voiceprints": "company/voiceprints.json",
  "glossary": "company/asr-glossary.md",
  "meetings_dir": "meetings"
}
```

**詞彙表**強烈建議建立（範例見 `examples/asr-glossary.example.md`）：一段包含團隊成員名、產品名、客戶代號、常用術語的繁體文字，會餵給 ASR 當 priming，是改善專有名詞辨識最有效的手段。

## 隱私與成本

- **音檔會離開你的機器**：轉錄送 OpenAI Audio API；diarization 送 pyannote.ai（暫存區檔案 48 小時自動刪除）。內容敏感的會議請自行評估。
- **Voiceprint 是不可逆的 feature vector**，無法還原成原音，但屬於可識別個人的生物特徵資料——建議存在**私有** repo，且取得團隊成員同意後再建立。
- 成本量級：一小時會議約一次 pyannote diarization/identify job + 一小時 whisper-1 轉錄，依兩家 API 定價計費。

## License

MIT
