---
name: voiceprint-setup
description: 建立或更新團隊聲紋庫（voiceprints.json）。流程：使用者提供一段多人會議錄音 → pyannote.ai diarization 把錄音依 speaker 分段 → 為每位 speaker 切出拼接樣本（最長 29 秒，盡量長辨識度才好）→ 開本地 HTML 介面讓使用者試聽並標記人名 → 對標記過的樣本建立 voiceprint 寫入聲紋庫。後續 meeting-notes skill 就用這個聲紋檔做 speaker 識別。當使用者想「設定聲紋」「建立聲紋庫」「voiceprint setup」「新增成員聲紋」「聲紋失準重做」，或 meeting-notes 跑之前發現聲紋庫不存在需要先建立時觸發。
---

# voiceprint-setup

把一段多人會議錄音變成團隊聲紋庫。整個流程中**人名標記必須由使用者親自做**（在 HTML 介面試聽後填名字），你不要替任何 speaker 猜名字。

## 路徑設定

聲紋庫預設寫到專案根目錄的 `.tomoaid/voiceprints.json`；專案根目錄若有 `.tomoaid.json` 設定檔（`{"voiceprints": "..."}` ），以它的 `voiceprints` 為準。下文 `<voiceprints>` 指解析後的實際路徑。

## 前置條件

缺一個就停下來告訴使用者，不要硬上：

- `PYANNOTEAI_API_KEY` 已設於環境變數
- `ffmpeg` / `ffprobe` 在 PATH 裡（`brew install ffmpeg`）
- 使用者提供一個錄音檔（.m4a / .mp3 / .wav 等）。**多人會議錄音最好**——一次就能建立多位成員的聲紋；單人錄音也可以（只會切出一位 speaker）

## 1. 確認音檔

```bash
ls -la <audio_path>
ffprobe -v error -show_entries format=duration -of csv=p=0 <audio_path>
```

回報檔案大小與時長。錄音太短（< 2 分鐘）先提醒使用者：每位成員的可用樣本可能不足 30 秒，聲紋辨識度會打折，但仍可繼續。

## 2. Diarization + 切出每位 speaker 的樣本

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/voiceprint_extract.py" \
    <audio_path> \
    --out-dir /tmp/voiceprint-setup/<basename>
```

腳本內部流程：上傳音檔到 pyannote.ai 暫存區 → `/v1/diarize` 分段（這一步**不需要**既有聲紋）→ poll 到 succeeded → 每位 speaker 挑最長的發言段落、用 ffmpeg 切出並拼接成單一樣本（**上限 29 秒**——pyannote voiceprint API 限 30 秒，樣本越長辨識度越好）→ 輸出 `clips/SPEAKER_XX.wav` 與 `manifest.json`。

- 進度印到 stderr；stdout 最後一行是 manifest 路徑。
- 跑完把每位 speaker 的「總發言時長 / 樣本長度」回報給使用者。某 speaker 總發言 < 10 秒的，提醒：樣本太短，建議之後用更長的錄音重做這個人。
- API 錯誤（401 / 429 / 400）**原樣轉達不要重試**。

## 3. 開標記介面，等使用者標記

組 autocomplete 名單給輸入框用：來源是**既有聲紋庫 `<voiceprints>` 的成員名（JSON keys）**，加上使用者在對話中提過的成員名；都沒有就不傳 `--team`。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/label_server.py" \
    --dir /tmp/voiceprint-setup/<basename> \
    --port 8765 \
    --team "Alice,Bob,Carol"   # ← 以實際成員名為準
```

- **用 run_in_background 跑**，然後 `open http://127.0.0.1:8765/` 幫使用者開瀏覽器。
- 告訴使用者：逐段試聽 → 填成員**英文名**（meeting-notes 的逐字稿 speaker label 與 Action Items owner 都用這個名字，全團隊要用一致的拼法）→ 不是團隊成員/雜訊勾「略過」→ 按「儲存」。
- **同一人出現在多個 speaker cluster**（diarization 偶爾會把一個人切成兩個 cluster）：只標樣本最長的那段，其餘勾略過——介面會擋重複名字。
- 使用者按儲存後 server 自動寫出 `labels.json` 並結束（exit 0）；15 分鐘沒儲存會 timeout（exit 3）。等背景程序結束再繼續。
- port 8765 被占用就換一個（如 8766），`open` 的網址跟著改。

## 4. 建立 voiceprint 並寫入聲紋庫

讀 `labels.json`，把有標名字的 clip 複製成以人名命名的檔案（`pyannote_voiceprint.py` 用 filename stem 當 label），再一次建立：

```bash
mkdir -p /tmp/voiceprint-setup/<basename>/named
cp /tmp/voiceprint-setup/<basename>/clips/SPEAKER_00.wav /tmp/voiceprint-setup/<basename>/named/Alice.wav
cp /tmp/voiceprint-setup/<basename>/clips/SPEAKER_02.wav /tmp/voiceprint-setup/<basename>/named/Bob.wav
# …每個有標名字的 speaker 一個

mkdir -p "$(dirname <voiceprints>)"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pyannote_voiceprint.py" \
    /tmp/voiceprint-setup/<basename>/named/*.wav \
    --out <voiceprints> --merge
```

- **一定加 `--merge`**：只新增/覆寫這次標記的成員，不會弄丟既有成員的聲紋。初次建立（檔案不存在）行為相同。
- 標 `null`（略過）的 speaker 不處理。

## 5. 驗證與回報

```bash
python3 -c "import json; print(sorted(json.load(open('<voiceprints>'))))"
git diff --stat <voiceprints>
```

統一回報格式：

```
✓ 聲紋庫已更新：.tomoaid/voiceprints.json
✓ 本次建立/更新：Alice、Bob、Carol
✓ 略過：SPEAKER_03（標記為非團隊成員）
ℹ 目前聲紋庫成員：Alice、Bob、Carol、Dave

⚠ 注意：
  - Dave 總發言僅 8 秒，樣本偏短，建議下次用他發言較多的錄音重做
```

提醒使用者：voiceprint blob 是 feature vector，**不能還原成原音**，可以放心 commit 進私有 repo。確認 `git diff` 沒問題後由使用者自行 commit。

---

## 失敗模式提醒

- **不要替 speaker 猜名字**——標記只能由使用者在介面上做。diarization 的 cluster 編號（SPEAKER_00）跟人名的對應，你沒有任何依據可以推斷。
- **label timeout（exit 3）不是錯誤**——使用者可能臨時走開。問一聲要重開 server 還是之後再繼續（clips 都還在 /tmp，重跑 §3 即可，不用重做 diarization）。
- **pyannote 失敗就停**——這個 skill 的目的就是建聲紋，沒有 fallback 路徑。原樣轉達錯誤。
- **上傳到 pyannote 的音檔 48 小時自動刪除**（暫存區），不留痕；本地 clips 在 /tmp 重開機就沒了，都不需要清理。

## 什麼時候需要重跑

1. **新團隊成員加入**——拿一段他有發言的會議錄音重跑，`--merge` 會把他加進去
2. **既有成員聲紋失準**——連續幾次會議都被標 `UNKNOWN`（感冒、換麥克風、環境變化），重跑並標記他，新聲紋覆寫舊的
