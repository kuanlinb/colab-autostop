# colab-autostop

讓 Google Colab 上的無人值守工作**自己上傳結果、自己釋放 VM**。工作跑完、卡住或碰到時間上限，VM 都會自己收尾；開工作的那台電腦全程睡著、重開或斷網都沒關係。

[English](README.md)

## 要解決的問題

Colab 的機器只要還掛著就會一直扣運算單元。官方 `colab` CLI 和筆記本介面都會在 kernel 有活動時幫你維持連線，工作在跑的時候這是好事；但工作跑完或卡住以後，沒有人去關，它就一直扣點。

常見的做法是在筆電上放一個監看程式，定時查 VM，結束時下 `colab stop`。問題是它偏偏在最需要的時候失靈：筆電睡著、重開、斷網，或監看程式自己卡住。這個專案的起點就是一次這樣的事故：筆電監看程式的 `colab exec` 卡住，一台工作早就跑完的 G4 GPU 機器空轉了 3 小時 40 分，多扣了大約 37 個運算單元。

`colab-autostop` 把「什麼時候該關」這個判斷搬到 VM 上。一支小小的 bash 守衛跟工作一起跑，時候到了就用預先簽好的網址把結果傳到物件儲存空間，然後自己釋放 VM。

## 運作方式

```
筆電：colab-autostop launch                          Colab VM
  1. colab new ......................................> 拿到 VM
  2. 簽兩個 PUT 網址（results.tgz、status.txt）
  3. colab upload：守衛、設定、網址、工作指令 ........> /content/autostop/
  4. colab exec 一支一秒鐘的啟動程式 ................> 在背景啟動 box_guard.sh，等它回報 armed，
     （馬上返回；每個 CLI 呼叫都有時間上限，             再在背景啟動你的工作
       超時就連同整個子行程樹一起砍掉）
                                                       守衛每 30 秒檢查一次：
                                                         DONE？STUCK？CAP？
  ……之後筆電可以關機……                                 只要碰到其中一種：
                                                           打包結果 -> curl PUT results.tgz、status.txt
                                                           POST http://$TBE_RUNTIME_ADDR/unassign（釋放 VM）
任何一台有儲存空間金鑰的電腦，隨時：
  colab-autostop fetch  <................................ 儲存空間
```

如果 VM 已經開好，但後面的步驟失敗（上傳失敗、守衛沒有就位、`exec` 卡住），launcher 會先把 VM 關掉再結束，所以啟動失敗不會留下一台還在扣點的機器。

### 三種結束狀態

| 狀態 | 什麼時候 | 傳出去的東西 |
|---|---|---|
| `DONE` | 出現完成標記：預設是工作指令結束（會附上結束碼），或任何一個 `--done-file` 出現，或 `--done-regex` 對到 log 裡的一行 | 結果路徑、工作 log、守衛 log |
| `STUCK` | 連續 `--idle-min` 分鐘（預設 20）都沒有進度：監看路徑底下沒有檔案被寫入、GPU 使用率一直低於 5 %、工作自己的行程用的 CPU 一直低於一顆核心的 30 % | 目前有的東西，同一個壓縮檔 |
| `CAP` | 守衛啟動後滿 `--cap-min` 分鐘（預設 720），不管工作在做什麼 | 目前有的東西 |

`status.txt` 只有一行，例如 `DONE 2026-11-10T08:12:40Z done marker found, exit code 0`。它在 `results.tgz` 之後才上傳，所以看得到狀態檔，就代表壓縮檔已經完整。壓縮檔裡的結果路徑和 VM 上一樣（`content/out/...`），另外放一份守衛自己的 log，檔名是 `guard.log`，每 10 次檢查會記一行各項進度訊號的讀數。守衛資料夾本身（裡面有上傳網址）永遠不會被打包。

工作的輸出寫在 `/content/autostop-job.log`，這個檔也在監看範圍內，所以有在印東西的工作就算活著。launcher 會設 `PYTHONUNBUFFERED=1`，讓 Python 的輸出一印出來就寫進 log。

如果上傳連續失敗 5 次，守衛會把 VM 留 `--fallback-wait-min` 分鐘（預設 45），讓你還能用 `colab download /content/autostop/results.tgz` 拿回來，時間到一樣釋放。如果根本沒設定上傳網址，就直接釋放。如果釋放請求失敗，守衛會先試一次 Colab 自己的 `runtime.unassign()`，然後每分鐘重送一次，連續 30 分鐘才放棄。每個探測指令（`nvidia-smi`、`find`、`tar`）都有時間上限，所以 GPU 卡死或網路磁碟沒回應，也不會讓守衛停住。

## 安裝

需要 Python 3.8 以上，沒有其他相依套件。另外要裝好官方的 [Colab CLI](https://pypi.org/project/google-colab-cli/) 並登入。

```bash
git clone <這個 repo> && cd colab-autostop
pip install .
```

## 快速上手

1. 開一個私有 bucket，和一把只能讀寫這個 bucket 的金鑰（R2 的話就是對單一 bucket 有「Object Read & Write」權限的 API token）。
2. 告訴工具儲存空間在哪裡。每個設定都可以直接給值，也可以用 `NAME_FILE` 指向存著值的檔案，這樣金鑰不會留在 shell 歷史紀錄裡：

   ```bash
   export AUTOSTOP_PROVIDER=r2                 # r2 | s3 | gcs | custom
   export AUTOSTOP_BUCKET=colab-results
   export AUTOSTOP_ACCOUNT_ID=<Cloudflare 帳號 id>             # 只有 R2 需要
   export AUTOSTOP_ACCESS_KEY_ID_FILE=~/.secrets/r2_key_id
   export AUTOSTOP_SECRET_ACCESS_KEY_FILE=~/.secrets/r2_secret
   ```

3. 啟動：

   ```bash
   colab-autostop launch --session train1 --gpu G4 \
     --upload ./train.tgz=/content/train.tgz \
     --job "tar xzf train.tgz && python train.py --out /content/out" \
     --results /content/out --idle-min 20 --cap-min 360
   ```

4. 之後在任何地方取回：

   ```bash
   colab-autostop fetch --session train1 --dest ./pulled          # 查一次
   colab-autostop fetch --session train1 --dest ./pulled --wait   # 一直等到工作結束
   ```

   結束碼：`DONE` 且工作結束碼是 0 時回傳 0，`DONE` 但工作結束碼不是 0 時回傳 4，`STUCK` 或 `CAP` 是 3，還沒結束是 2，出錯是 1。在沒有啟動紀錄的電腦上，可以改用 `fetch --key <前綴>`。

如果你的 `colab` CLI 包在另一支程式裡（例如切換帳號用的腳本），用 `--colab` 或 `AUTOSTOP_COLAB` 指定，例如 `--colab "powershell -NoProfile -File C:\path\colab_b.ps1"`。

在 Windows 上，Git Bash 會先把 `/content/out` 這類參數改寫成 `C:/Program Files/Git/content/out` 才交給 Python。請在 PowerShell 或 cmd 執行 launcher，或在 Git Bash 設 `MSYS_NO_PATHCONV=1`。launcher 會拒絕不是 `/` 開頭的 VM 路徑，所以遇到這種情況會直接報錯，不會默默打包到空的東西。

`status.txt` 是在送出釋放請求之前上傳的，所以它沒辦法告訴你釋放有沒有成功。要確定沒有機器還在扣點，請看 `colab sessions`（下一次執行任何 `colab` 指令時，也會清掉 Colab 已經結束的 session）。

### 支援的儲存空間

| `AUTOSTOP_PROVIDER` | 另外需要 | 網址格式 |
|---|---|---|
| `r2`（預設） | `AUTOSTOP_ACCOUNT_ID` | `https://<帳號>.r2.cloudflarestorage.com/<bucket>/<key>` |
| `s3` | `AUTOSTOP_REGION`（預設 `us-east-1`）；要 path-style 就設 `AUTOSTOP_PATH_STYLE=1` | `https://<bucket>.s3.<region>.amazonaws.com/<key>` |
| `gcs` | [HMAC 金鑰](https://cloud.google.com/storage/docs/authentication/hmackeys) | `https://storage.googleapis.com/<bucket>/<key>`，用 `GOOG4-HMAC-SHA256` |
| `custom` | `AUTOSTOP_ENDPOINT`，例如 `https://minio.example.com:9000` | path-style，SigV4 |

`colab-autostop presign put|get KEY` 會用同一組設定印出一個預先簽好的網址。

### 常用的啟動選項

| 選項 | 預設 | 意思 |
|---|---|---|
| `--watch PATH` | `/content` | 這裡有檔案被寫入就算有進度（可以給多次） |
| `--ignore PATH` | 無 | 另外指定寫入不算進度的路徑；Colab 自己會動的 `/content/.config`、`/content/drive`、`/content/sample_data` 一律不算 |
| `--gpu-busy-pct N` | 5 | GPU 使用率達到 N % 就算有進度；設 101 等於不看 GPU |
| `--cpu-busy-pct N` | 30 | 工作自己的行程在一次檢查間隔內平均用到一顆核心的 N % 就算有進度；設 `off` 等於不看 CPU |
| `--done-file PATH` | 無 | VM 上額外的完成標記檔 |
| `--done-regex RE` | 無 | `--done-log`（預設是工作 log）有一行符合 RE 就算 DONE |
| `--no-exit-done` | 關 | 給那種啟動背景服務後馬上返回的工作用 |
| `--poll-sec N` | 30 | 守衛多久檢查一次 |
| `--new-timeout`、`--upload-timeout`、`--cli-timeout` | 300、600、150 秒 | 每個 CLI 呼叫的時間上限 |
| `--keep-on-error` | 關 | 啟動失敗時不關 VM，方便除錯 |

## 只用守衛

`colab_autostop/box_guard.sh` 是一支獨立的 bash 腳本，不靠 launcher 也能在筆記本裡用。把它放到 VM 上，在自己電腦簽兩個 PUT 網址（`colab-autostop presign put <前綴>/results.tgz --expires 86400`），然後在背景啟動：

```python
import os, subprocess
os.makedirs("/content/autostop", exist_ok=True)
open("/content/autostop/put_results.url", "w").write("<results.tgz 的 PUT 網址>")
open("/content/autostop/put_status.url", "w").write("<status.txt 的 PUT 網址>")
env = dict(os.environ, AUTOSTOP_RESULTS="/content/out", AUTOSTOP_DONE_FILE="/content/out/DONE",
           AUTOSTOP_IDLE_MIN="20", AUTOSTOP_CAP_MIN="360")
subprocess.Popen(["bash", "/content/box_guard.sh"], env=env, start_new_session=True)
```

你的程式跑完時寫一個 `/content/out/DONE` 就好。如果想把 CPU 當成進度訊號，把工作所在 session 的帶頭行程 pid 寫到 `/content/autostop/job.pid`（或用 `AUTOSTOP_JOB_PID_FILE` 指定）；沒有這個檔，就只看寫檔和 GPU。所有設定都列在腳本開頭。

## 安全設計

- **長期有效的金鑰從來不會出現在 VM 上。** VM 只拿到兩個預先簽好的 PUT 網址，每個只能寫一個固定的物件、只能用 PUT，而且只在時間上限再加 24 小時內有效（最多 7 天，這是 SigV4 的上限）。就算網址外流，最糟的情況是有人在過期前覆寫這兩個物件，bucket 裡其他東西都讀不到、列不出來，也刪不掉。
- 下載用的 GET 網址在你自己的電腦上簽，15 分鐘後失效。
- 金鑰從環境變數或檔案讀取，工具不會把它寫到任何地方。本機的啟動紀錄（`~/.colab-autostop/runs/<session>.json`）只有服務商、bucket 名稱和物件前綴。
- 建議用專用的 bucket，配一把只能存取這個 bucket 的金鑰。
- 網址放在 VM 的 `/content/autostop`，VM 釋放時就跟著消失。

## 限制

- **釋放 VM 用的是沒有公開文件的端點。** 守衛送 POST 到 `http://$TBE_RUNTIME_ADDR/unassign`，和 `google.colab.runtime.unassign()` 送的是同一個請求。這是 Colab 內部介面，沒有公開文件，隨時可能改掉或拿掉。如果它失敗，守衛會記 log、重試三次後結束；結果還是會上傳，但 VM 會繼續扣點，直到 Colab 自己收回或你手動關掉。跑貴的機器時，請自己再準備一道保險，例如排程在上限加緩衝時間後執行 `colab stop`。
- **安靜太久會被當成卡住。** 「有進度」的定義是監看路徑底下有檔案被寫入，或 GPU、CPU 在忙。如果工作只是在等（等遠端 API、慢慢下載到沒被監看的路徑），又超過 `--idle-min` 都沒寫檔，就會被判成 `STUCK`。可以調高 `--idle-min`、多監看幾個路徑，或讓工作定期更新一個心跳檔。
- **CPU 只算工作自己的行程**，也就是 launcher 啟動工作時開的那個 session 裡的所有行程。第一版看的是整台 VM 的 CPU，結果死掉的工作永遠不會被判成 `STUCK`：一台閒著的 Colab CPU 機器，光 Colab 自己的服務就用掉一顆核心的 17% 到 29%，每兩三分鐘還會衝過 30%。工作自己另開新 session 的背景程式（`setsid`、`start_new_session=True`）不會算進去。
- **在忙不代表正常。** 死結但 GPU 或 CPU 一直在轉的程式，或不斷印同一個錯誤的程式，都不會被判成 `STUCK`，只有 `CAP` 能結束它。所以一定要設一個你付得起的上限。
- 結果用一個 PUT 請求上傳。S3 和 R2 單次 PUT 最多 5 GiB；很大的存檔點請另外放或先切開。
- `colab upload` 會把整個檔案放在一個請求裡送出；大的輸入檔請改成在 VM 上下載。
- 清單類設定用空白分隔，所以不支援含空白的路徑。
- 簽名帶有時間戳記，負責簽名的那台電腦時間要大致正確。
- `tar` 會把壓縮檔寫在 VM 的磁碟上，所以需要大約等於壓縮後結果大小的空間。
- 自訂端點如果寫明預設連接埠（`https://host:443`），簽進去的 host 可能和 curl 實際送出的不同；請不要寫預設連接埠。
- 守衛需要 bash、`find`、`tar`、`curl`，Colab 的映像檔都有。

## 和其他做法比較

| | colab-autostop | 筆記本最後一格呼叫 `runtime.unassign()`，結果存 Drive | SkyPilot autostop | `colab run` |
|---|---|---|---|---|
| 工作結束後關機 | 會 | 會，前提是最後一格有跑到 | 會 | 會 |
| 工作卡住時關機 | 會，看進度判斷（`STUCK`）加硬上限 | 不會：最後一格永遠跑不到 | 只有硬上限（`wait_for: none`）；卡住但還在跑的工作算忙碌 | 不會 |
| 你的電腦關機也有效 | 是 | 是 | 是 | 否：靠本機的 CLI 行程推動 |
| 把結果送出 VM | 物件儲存空間，預先簽好的網址，VM 上沒有金鑰 | 掛載 Drive，需要在 session 裡互動式登入 Google | 它自己的儲存掛載 | 要自己複製 |
| 能用在 Colab | 能 | 能 | 不能，Colab 不在它支援的雲端裡 | 能 |

在 SkyPilot 支援的雲端上，用 SkyPilot 就對了；這個專案存在，是因為 Colab 不在其中，而 Colab 的機器本身也沒有自動停機功能。

## 測試

```bash
python -m unittest -v
```

全部都不需要 Colab，也不需要網路：

- `tests/test_sign.py`：簽名，包括 AWS SigV4 查詢字串文件裡的範例，以及 R2、S3、GCS、自訂端點的網址格式。
- `tests/test_guard.py`：用假的 `curl`、假的 `nvidia-smi` 和假的 `/proc/stat` 測守衛的狀態機（用結束碼或 regex 判斷 DONE、STUCK、CAP、守衛自己的寫入和忽略路徑不算進度、GPU 忙碌、工作 session 的 CPU 每次檢查分開算而且不算其他 session（另外也用真的 `/proc` 測一個吃滿 CPU 和一個在睡的程式）、`nvidia-smi` 卡住、上傳失敗、釋放失敗、上傳網址不會被打包、不在 Colab 上），另外把整個 VM 端的啟動流程放在暫存資料夾裡跑一遍。這部分需要 bash，所以在 Linux、macOS 或 WSL 上跑，原生 Windows 會略過。
- `tests/test_launch.py`：用一個假的 `colab` CLI 測 launcher，而且像 shell 包裝腳本那樣多包一層：卡住的 `exec` 和 `new` 會連同整個子行程樹被砍掉、VM 會被關掉；上傳失敗、守衛沒就位、按 Ctrl-C 都會關掉 VM；同名 session 已存在會拒絕啟動；確認金鑰本身從來沒有被上傳。

目前做過的實機驗證（2026-11-10，Colab CPU 機器和一台 G4 GPU 機器、Cloudflare R2、`colab` CLI 0.7.5；每台測試機都自己釋放了，之後 `colab sessions` 是空的；啟動一次大約 30 秒）：

| 測試 | 工作內容 | 結果 |
|---|---|---|
| DONE | 睡 75 秒、寫 `out/acc.txt`、結束碼 0 | 啟動後 1.5 分鐘判定 `DONE`，結束碼 0，結果檔有在壓縮檔裡 |
| STUCK | 寫一個檔後就一直睡 | 最後一次寫檔後剛好 3 分鐘判定 `STUCK`（`--idle-min 3`），工作的 CPU 讀數是 0% |
| CAP | 每 20 秒寫一行 | 在 4 分鐘上限判定 `CAP` |
| 只吃 CPU | `while True: pass`，完全不寫檔 | 工作 CPU 讀數是一顆核心的 98% 到 99%，沒有被判成 `STUCK`，在 6 分鐘上限判定 `CAP` |
| G4 GPU | 先做 5 分鐘矩陣乘法、完全不寫檔，之後佔著 GPU 記憶體睡覺（`--cpu-busy-pct off`，只剩 GPU 讀數能讓它不被判卡住） | GPU 讀數先是 100%、後來 0%；撐過不寫檔的 5 分鐘，在 GPU 閒下來後剛好 3 分鐘判定 `STUCK`（啟動後 8 分鐘）；花了約 1.2 個運算單元 |

STUCK 這項在第一版是失敗的，因為當時看的是整台 VM 的 CPU：閒著的機器光 Colab 自己的服務就一直把閒置計時歸零，死掉的工作一路跑到上限。所以現在 CPU 只算工作自己的行程。

還沒有實機測過的：AWS S3、GCS，以及 G4 以外的 GPU 型號。S3 和 GCS 的簽名只對過官方的測試範例和格式規則。

## 授權

MIT。本專案和 Google、Colab 沒有任何關係。
