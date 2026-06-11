---
name: meeting-notes
description: 將會議錄音轉成結構化的會議記錄 .md。流程：用 pyannote.ai 聲紋識別（聲紋庫由 voiceprint-setup skill 建立，沒建立要先警告）+ OpenAI whisper-1 ASR 產生 speaker-labeled 逐字稿（使用者確認後才 fallback 到 gpt-4o-transcribe plain 逐字稿）→ 清理口頭禪、雜訊、ASR 錯字、保留 speaker label → 把原始音檔與清理後的逐字稿存進專案會議目錄 → 產出 `YYYY-MM-DD-{topic-slug}.md` 會議記錄 → 統一回報產出檔案與不確定的點（**不自動 commit、不開 PR、不開 Issue**，後續由使用者自行決定）。當用戶提供本地錄音檔（.m4a / .mp3 / .wav / .webm / .mp4 等），或語意上想「整理會議」「轉逐字稿」「寫會議紀要」「產 Action Items」「會議摘要」「meeting transcript」「meeting notes」「整理開會內容」時觸發。即使用戶沒有明說「會議記錄」四個字，只要丟出錄音檔且暗示要整理內容，就要用此 skill；不要自己用 whisper 或別的方式硬轉。
---

# meeting-notes

把錄音檔變成結構化會議記錄。整體流程是固定的，但**摘要與分節要根據逐字稿內容判斷**——不要硬套模板。

## 路徑設定

預設路徑（在執行專案的根目錄下）：

| 用途 | 預設路徑 |
|------|----------|
| 聲紋庫 | `.tomoaid/voiceprints.json` |
| ASR 詞彙表（選填） | `.tomoaid/asr-glossary.md` |
| 會議記錄輸出目錄 | `meetings/` |

專案根目錄若有 `.tomoaid.json`，以它的設定為準：

```json
{
  "voiceprints": "company/voiceprints.json",
  "glossary": "company/asr-glossary.md",
  "meetings_dir": "meetings"
}
```

下文的 `<voiceprints>`、`<glossary>`、`<meetings>` 都指解析後的實際路徑。音檔放 `<meetings>/recordings/`、逐字稿放 `<meetings>/transcripts/`。

## 前置條件

開工前先檢查（缺一個就停下來告訴用戶，不要硬上）：

- `OPENAI_API_KEY` 已設於環境變數
- `PYANNOTEAI_API_KEY` 已設於環境變數（speaker 識別用）
- 聲紋庫 `<voiceprints>` 已存在（由 voiceprint-setup skill 建立）
- `ffmpeg` / `ffprobe` 在 PATH 裡（`brew install ffmpeg`）

### 聲紋庫沒建立時：先警告，不要無聲 fallback

聲紋庫 `<voiceprints>` 不存在或 `PYANNOTEAI_API_KEY` 沒設時，**先停下來警告使用者**，例如：

> ⚠ 聲紋庫尚未建立，這場會議無法做 speaker 識別。
> 建議先跑 `/tomoaid:voiceprint-setup` 建立團隊聲紋庫（提供一段多人會議錄音即可），再回來跑這份會議記錄。
> 也可以直接繼續，但逐字稿不會有 speaker label，Action Items owner 的推斷會弱很多。

使用者明確說要繼續，才走 §2 的 plain transcript fallback 路徑。

## 用戶輸入

用戶通常只會丟一個檔案路徑，例如 `/meeting-notes ./recordings/2026-05-05.m4a`。
**會議主題與日期**幾乎都要你自己推斷或詢問——不要無聲地猜。

接收到指令後依序處理下面 8 步（§1–§7 把錄音變成本地會議記錄與逐字稿，§8 統一回報）。

---

## 1. 確認音檔存在與基本資訊

```bash
ls -la <audio_path>
ffprobe -v error -show_entries format=duration -of csv=p=0 <audio_path>
```

把檔案大小（MB）、時長（分鐘）回報給用戶，避免後面才發現檔案壞掉。

## 2. 轉錄逐字稿（**直接用 bundled script，不要自己重寫邏輯**）

兩條路徑：有 pyannote 走 diarize（**預設**），沒有走 plain transcribe。

### 預設路徑：speaker-labeled 逐字稿（`diarize_merge.py`）

`PYANNOTEAI_API_KEY` 有設且聲紋庫存在時走這條：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/diarize_merge.py" \
    <audio_path> \
    --voiceprints <voiceprints> \
    --prompt-file <glossary> \
    --out /tmp/<basename>-raw.md \
    --raw-out /tmp/<basename>-segments.json \
    --language zh
```

腳本內部流程：上傳音檔到 pyannote.ai 暫存區 → `/v1/identify` 用 `exclusive=true` 配 voiceprints 識別 → poll 到 succeeded → 同時把音檔送 OpenAI whisper-1（verbose_json，需要時間戳所以**不能用 gpt-4o-transcribe**；prompt 帶入詞彙表 priming 人名與術語）→ **逐字（word-level）**把 ASR 配給對應 speaker，一句橫跨兩人時在字邊界切開 → 連續同 speaker 合併段落 → 輸出 markdown。

`--prompt-file` 一律帶詞彙表 `<glossary>`（檔案不存在就略過這個參數，並在最後回報建議建立——範例見 `${CLAUDE_PLUGIN_ROOT}/examples/asr-glossary.example.md`）。

輸出格式（每塊兩行：speaker header + 內文）：

```
**Alice** [00:00:12–00:01:34]
今天主要想對齊 5/29 上線的範圍...

**Bob** [00:01:35–00:02:10]
我這邊聲紋識別流程已經寫好...

**SPEAKER_00** [00:02:11–00:02:18]
（外部來賓——有人聲但無 voiceprint 對應）

**UNKNOWN** [00:45:02–00:45:06] ⚠ 無語音區段（疑似 ASR 幻覺，清理時預設刪除）
謝謝大家收看
```

- 有人聲但比對不到 voiceprint 的段（外部來賓）會標 `SPEAKER_00` 這類編號 — 不要硬猜成內部人。
- 標 `UNKNOWN` 加 `⚠ 無語音區段` 的段落＝pyannote 沒偵測到任何語音但 whisper 吐了字 — **疑似 ASR 幻覺**，§3 清理時預設刪除。
- 音檔 >25MB whisper 會超限，腳本會自己 ffmpeg 切 10 分鐘段（64 kbps mono 16k MP3）逐段轉錄再合併；每段的 prompt 會自動接上前段結尾維持上下文。
- pyannote identify job 通常 1-3 分鐘；超過 10 分鐘會 timeout。
- 進度印到 stderr，最後一行印 speaker segment 統計。

### Fallback 路徑：plain 逐字稿（`transcribe.py`）

1on1 等不需要 speaker 標記的場合，或缺聲紋環境且**使用者已依前置條件的警告規則確認要繼續**時走這條：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transcribe.py" \
    <audio_path> \
    /tmp/<basename>-raw.txt \
    --prompt-file <glossary> \
    --language zh
```

用 OpenAI gpt-4o-transcribe（品質比 whisper-1 好，但**沒有時間戳所以不能跟 diarization 對齊**），輸出純文字。腳本自己處理 24MB 切段，切段時每段 prompt 自動接上前段結尾。

### 通用規則

- 語言：中文會議用 `zh`、英文會議用 `en`；混雜中英文不要傳 `--language`，讓模型自動偵測。
- 詞彙表 `<glossary>` 是給 ASR 的 priming 文字（人名、產品名、術語；用繁體寫同時把輸出偏向繁體）。**新成員、新產品名、新客戶代號出現時要更新它**——發現逐字稿反覆聽錯某個專有名詞，就是該補詞彙表的訊號。
- 如果腳本回報 OpenAI / pyannote 錯誤（401 / 429 / 400 等），**原樣轉達給用戶不要重試** — 可能是 API key、額度、檔案格式、或 voiceprints.json 內容有問題。

## 3. 清理逐字稿

讀入 §2 輸出（diarize 走 `-raw.md`，plain 走 `-raw.txt`），產出清理版本。原則：

- **去**：「嗯、啊、那個、然後、就是、對對對、欸」這類純贅詞
- **去**：完全聽不懂的雜訊段落（直接刪，不要編造）
- **修**：ASR 錯字，特別是技術名詞與**團隊成員的人名**——對照聲紋庫 `<voiceprints>` 的成員名（JSON keys）與詞彙表修正（例：「Erica」→「Eric」這種高機率 ASR 錯誤可以修；不確定的就保留原樣不要硬猜）
- **保**：所有資訊內容、所有數字、所有觀點。清理是去蕪存菁，**不是壓縮**
- **加**：每換一個話題就空行分段（diarize 版本身已經依 speaker 分段，但話題切換仍可以再加空行）

### Speaker label 處理規則

**diarize 版（有 `**Name** [time]` header）：**
- **直接保留 speaker header 與時間戳**，不要拿掉、不要改寫格式（`**Alice** [00:12:34–00:13:45]` 維持原樣）
- pyannote 在 `exclusive=true` 模式下用 voiceprint 比對，識別出來的名字**可信度高**，不要憑直覺改 speaker 名字
- `SPEAKER_00` 這類編號 header 保留 — 有人聲但無 voiceprint 對應（外部來賓）；**不要硬猜成內部成員**
- `UNKNOWN` 加 `⚠ 無語音區段` 標注的整塊**預設刪除** — 那是 whisper 在 pyannote 認定無語音處吐出的字（ASR 幻覺）。唯一例外：內容明顯是連貫有資訊量的真句子（可能 pyannote 漏偵測），就保留並列入最後回報的不確定點
- 如果整段內文是雜訊（例：「嗯，啊，那個」連續一分鐘），可以把那整塊（header + 內文）刪掉

**plain 版（沒 speaker header）：**
- **不要替任何段落自己加發言人 prefix**（不要寫「Alice：」「Bob：」）— ASR 把人名聽錯的成本太高，標錯一個 owner 全份記錄都會誤導下游

清理後寫到 `/tmp/meeting-notes/<basename>-clean.md`（用 .md 副檔名，因為內容會有 markdown 章節標題與 speaker header）。

## 4. 推斷 metadata（**自己決定，不必中途暫停**）

從清理後的逐字稿推斷下列欄位，**直接寫進去**——使用者會在最後 review 會議記錄時自行修正，不要為了 100% 正確中途來回確認：

| 欄位 | 推斷來源 |
|------|----------|
| `date` | 檔名 → 檔案 mtime → 逐字稿開頭的時間提及 → 否則用 `date +%F` |
| `topic-slug` | kebab-case，3-5 字英文，描述會議核心議題（例：`q2-kickoff`、`pricing-review`） |
| `title` | 一句話總結會議主題 |

## 5. 存檔到專案目錄

音檔與清理後的逐字稿直接放進專案的會議目錄，不上傳任何外部服務：

```bash
mkdir -p <meetings>/recordings <meetings>/transcripts

# 音檔（複製，保留原檔）
cp "<audio_path>" "<meetings>/recordings/<date>-<topic-slug>.<ext>"

# 清理後的逐字稿
cp /tmp/meeting-notes/<basename>-clean.md "<meetings>/transcripts/<date>-<topic-slug>-transcript.md"
```

- **音檔不進 git**——一場會議動輒 30-40MB，git 撐不住。檢查專案 `.gitignore` 是否含 `<meetings>/recordings/`，**沒有就主動加上**；音檔只留在跑這個流程的機器上。
- **逐字稿是文字檔，可進 git**——團隊能搜尋、review；要不要 commit 由使用者自行決定，這個 skill 不碰 git。
- frontmatter 的 `audio` / `transcript` 欄位填 repo 相對路徑（不是外部連結）。

## 6. 產出會議記錄 .md

**格式採混合策略**：如果 `<meetings>` 目錄已有 README 規範或既有會議記錄，**以該專案的慣例為準**（沿用它的 frontmatter 欄位與章節結構）；都沒有才用下面的內建模板。章節要根據內容調整——資訊密度高的會議用內容驅動的多章節寫法，輕量會議用 `## 重點討論` bullet 即可。

**重要：不要把完整逐字稿嵌入會議記錄內文**——逐字稿一律放 `<meetings>/transcripts/` 獨立檔案，用 frontmatter 的 `transcript` 欄位連過去。會議記錄要保持精煉、可掃讀，方便後續團隊 review；20k 字的逐字稿黏進去會把記錄淹掉。要看完整原文就開 transcript 檔。

```markdown
---
title: <一句話總結>
date: YYYY-MM-DD
audio: <meetings>/recordings/<date>-<topic-slug>.<ext>
transcript: <meetings>/transcripts/<date>-<topic-slug>-transcript.md
---

# <Title>

## 會議目標

<這場會議想解決什麼，1-3 句>

---

## <根據內容自訂章節>

<bullet / 表格 / 引用，視內容選擇。表格適合對齊比較；列表適合枚舉觀點>

---

## 決策

1. **<決策一句話>**：<理由與 context>

## Action Items

### <Owner 1>
- [ ] <具體可驗收的動作>

### <待指派 Owner>
- [ ] <逐字稿沒明確指派人時，用「待指派 Owner」群組>

## 待辦 / Open Questions

- <未解的問題>
```

> **不要在這個檔案末尾加 `## 完整逐字稿` 章節**——這是常見錯誤。完整逐字稿在 frontmatter `transcript` 指向的 `meetings/transcripts/` 檔案，不要重複貼一份。

### Action Items 寫作規則

- **不要外推 owner**：逐字稿沒明確說「X 來做」就放在「待指派 Owner」群組，不要從「X 講最多」推斷他是 owner
- **動詞開頭、結尾可驗收**：「整理會議錄音轉錄流程」❌；「將會議錄音轉錄流程寫成 GitHub Issue 並指派 owner」✅
- **不要硬塞**：會議真的沒拍板任何動作就寫「本次會議無 Action Items」，不要編

### 「決策」與「Open Questions」的差別

- 拍板了 → 決策
- 還在問 / 沒共識 / 留待下次 → Open Questions
- 講過但沒人接 → Action Items 的「待指派 Owner」

## 7. 寫入會議記錄檔案

存到 `<meetings>/<date>-<topic-slug>.md`。如果同名檔已存在，加後綴 `-2`、`-3` 而不是覆蓋。

寫完進 §8 統一回報。

## 8. 回報（最後一步，統一輸出給用戶）

產出到本地檔案為止——**不自動 commit、不開 PR、不開 Issue**，後續怎麼進 git、要不要開任務由使用者自行決定。

```
✓ 會議記錄：meetings/2026-05-05-q2-kickoff.md
✓ 逐字稿：meetings/transcripts/2026-05-05-q2-kickoff-transcript.md
✓ 音檔：meetings/recordings/2026-05-05-q2-kickoff.m4a（已 gitignore，僅本機）

⚠ 不確定的點：
  - 12:34 處聽不清，已標 [...]
  - 「待指派 Owner」共 2 條，請決定 owner：
    - 「決定 Phase 2 timeline」
    - 「整理客戶名單分級規則」
  - 名字「Erica」不在聲紋庫成員名單中（可能是「Eric」的 ASR 錯字）
```

如果有不確定的點（聽不清的段、模糊的人名、未指派的 Action Items、不在成員名單中的人名），**一定要列出來**——使用者 review 會議記錄時要靠這份清單補。

---

## 失敗模式提醒

- **不要在沒有逐字稿的情況下「腦補」會議內容**——如果腳本失敗，停下來告訴用戶，不要憑檔名瞎寫
- **不要把口頭禪清掉清到改變語意**——「我不確定但好像可以」不要清成「可以」
- **任務分工不要預先寫死**——Action Items 的 owner 只寫逐字稿明確指派的人，沒講就是「待指派 Owner」，不要替團隊做分工決定
- **聲紋庫沒建立時不要無聲 fallback**——先照前置條件的警告規則停下來，建議 `/tomoaid:voiceprint-setup`，等使用者確認才走 plain 路徑。
- **不要因為 pyannote 中途失敗就放棄整場**——聲紋庫存在但 pyannote 跑到一半失敗（API 掛掉、額度），告知用戶後改走 plain transcribe 路徑跑完，最後說明 speaker label 缺失，請人類補。
- **人名不在聲紋庫成員名單時不要硬猜**——當 ASR 錯字試修，修不出來就保留原樣並列進 §8 回報請用戶澄清。標錯 owner 比留白更貴。

---

## 附錄：如何建立 / 更新聲紋庫

**首選：跑 `/tomoaid:voiceprint-setup`**——給一段多人會議錄音，它會自動 diarize、切出每位 speaker 的樣本、開 HTML 介面讓使用者標記人名，最後 `--merge` 寫進聲紋庫。新成員加入、既有成員聲紋失準（連續幾次被標 `UNKNOWN`）都用它。

手動路徑（已有某成員的乾淨單人錄音時）：把檔名取成他的英文名（`Alice.m4a`——`pyannote_voiceprint.py` 用 filename stem 當 label，**30 秒以內**），跑：

```bash
PYANNOTEAI_API_KEY=... python3 "${CLAUDE_PLUGIN_ROOT}/scripts/pyannote_voiceprint.py" \
    Alice.m4a --out <voiceprints> --merge
```

`--merge` 只新增/覆寫傳入的 label，不動其他成員；不加就是完整覆寫。改完 `git diff <voiceprints>` 確認再 commit。

### 注意

- 上傳到 pyannote 的音檔 **48 小時自動刪除**（暫存區）；voiceprint blob 本身存在 `voiceprints.json`，是 feature vector，**不能還原成原音**，可以放心 commit 進私有 repo。
- `pyannote_upload.py` 是低階上傳工具，正常流程用不到 —— 只有想拿到 `media://` URI 做別的 pyannote 實驗時才會用。
