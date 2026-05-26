# "Apple Notes → Notion 移行スクリプト

## そもそも

```bash
#claude codeに下記プロンプトで命令し、プログラム作成
#References:https://note.com/ai_eng_tech/n/n0b0614c36cc7
###
###

#Apple純正のメモ(Notes)に入っている内容をNotionにデータ移行をしようと考えています。
#具体的には下記手順で検討をしてます。
#AppleScriptでメモのHTMLを取得
#HTMLをMarkdownに変換（画像・テーブルも抽出）
#Notion APIでページを作成・ブロックを追加
#以下のルールに則り、移行スクリプトを作成してください。

#ネストの深さ制限：Apple純正メモは6階層のリストも普通にありますが、Notion APIは一度に2レベルまでしか子ブロックを入れられません。多段階のAPI呼び出しで回避してください。
#テーブルが空になる：Apple純正メモのHTMLは、テーブルセルの中に余計なタグが入っていて、単純に変換するとセルが全部空になります。注意してくだい。
#画像サイズ：Notion APIには5MBの制限がありますが、Apple純正メモのPNG画像は軽く超えます。段階的な圧縮処理を組み込んでください。
#AppleScriptでの書き戻し厳禁：前述の通り、HTMLを書き戻すとメモが壊れます。読み取り専用で使うのが鉄則です。

###
###
```

## セットアップ

```bash
# 1. 仮想環境を作成 (推奨)
python3 -m venv .venv
source .venv/bin/activate

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# 3. Notion Integration Token を取得
#    https://www.notion.so/my-integrations で「New integration」を作成し
#    移行先データベース／ページを「Connections」から接続する

# 4. 移行先 Database ID を確認
#    ブラウザで対象データベースを開き、URL の末尾の英数字部分をコピー
#    https://www.notion.so/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...
#                            ↑ これが Database ID
#NOTE'
```

## 基本的な使い方

```bash
# 全ノートをデータベースへ移行
python migrate.py \
  --api-key  secret_xxxxxxxxxxxxxxxxxxxx \
  --database-id  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 環境変数で指定する場合
export NOTION_API_KEY=secret_xxxxxxxxxxxxxxxxxxxx
export NOTION_DATABASE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
python migrate.py

# 特定フォルダのみ移行
python migrate.py --folder "仕事"

# 先頭10件だけ試す (動作確認)
python migrate.py --limit 10 --dry-run

# データベースではなく通常ページ配下に作成
python migrate.py --parent-page-id xxxxxxxx...

# 詳細ログ付き
python migrate.py --verbose
```

## オプション一覧


| オプション              | 説明                                      |
| ------------------ | --------------------------------------- |
| `--api-key`        | Notion Integration Token (`secret_xxx`) |
| `--database-id`    | 移行先データベース ID                            |
| `--parent-page-id` | 移行先ページ ID（DB の代わり）                      |
| `--title-property` | DB タイトル列名 (デフォルト: `Name`)               |
| `--folder`         | 特定フォルダのみ移行                              |
| `--limit`          | 最大移行件数                                  |
| `--skip`           | 先頭 N 件をスキップ (途中再開に使用)                   |
| `--image-dir`      | 画像保存先 (デフォルト: `./migration_images`)     |
| `--dry-run`        | Notion に書き込まずノート一覧だけ表示                  |
| `--verbose`        | 詳細ログ                                    |


## Notion データベースの事前設定

```
必須プロパティ:
  Name (またはタイトル列)  ← --title-property で変更可能

任意プロパティ (あると folder 情報が記録される):
  フォルダ: Select 型
```

## 既知の制限

### 画像

- Notion Files API が利用できない場合、画像は `./migration_images/` に
保存され、ノート内にローカルパスを示す callout が挿入されます。
- 移行後、手動で画像を Notion にアップロードしてください。
- PNG の透過は保持されます。5MB 超の画像は自動圧縮されます。

### チェックリスト

- Apple Notes のチェックボックスは `ul` タグとして扱われる場合があります。
Notion の `to_do` ブロックには自動変換されません。

### 添付ファイル (画像以外)

- PDF・動画・音声等の非画像添付は移行されません。

## トラブルシューティング


| エラー                               | 原因と対処                                                               |
| --------------------------------- | ------------------------------------------------------------------- |
| `JXA エラー`                         | System Settings > Privacy > Automation で Terminal に Notes アクセス権限を付与 |
| `Notion API 403`                  | Integration が対象 DB/ページに接続されているか確認                                   |
| `Notion API 400 validation_error` | `--title-property` の列名を DB に合わせて変更                                  |
| `画像が見つかりません`                      | 添付ファイルの AppleScript 保存に失敗。手動確認が必要                                   |


