#!/usr/bin/env python3
"""
Apple Notes → Notion 移行スクリプト

対応課題:
  1. ネスト制限   : Notion API は1リクエストで子ブロックを2階層まで。
                   「親を先に作成 → 子を別APIで追加」を再帰的に繰り返す。
  2. テーブル空セル: Apple Notes の <td><p><span>text</span></p></td> を
                   get_text() / _get_rich_text() で正しく抽出。
  3. 画像サイズ   : PNG→JPEG 段階圧縮で 4MB 以内に収める。
  4. 書き戻し厳禁 : JXA / AppleScript は読み取りのみ。
"""

from __future__ import annotations
import subprocess, json, os, sys, re, base64, time, argparse, tempfile, logging
from pathlib import Path
from io import BytesIO
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

# ─── ログ設定 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── 定数 ──────────────────────────────────────────────────────────────────────
NOTION_API_BASE    = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
MAX_IMAGE_BYTES    = 4 * 1024 * 1024   # 4 MB (Notion 上限 5 MB の安全マージン)
MAX_RICH_TEXT_LEN  = 2000              # Notion リッチテキスト1要素あたりの文字数上限
BATCH_SIZE         = 100               # append_block_children の1回あたりの最大数
MAX_RETRIES        = 5
BASE_RETRY_DELAY   = 1.0


# ══════════════════════════════════════════════════════════════════════════════
# 1. APPLE NOTES EXPORTER  (読み取り専用)
# ══════════════════════════════════════════════════════════════════════════════

class AppleNotesExporter:
    """
    JXA (JavaScript for Automation) と AppleScript で Apple Notes を読み取る。
    Notes への書き戻しは一切行わない。
    """

    # ── JXA: ノード一覧 (軽量メタデータのみ) ──────────────────────────────
    _JXA_LIST_NOTES = """
    const Notes = Application('Notes');
    const result = Notes.notes().map((n, i) => {
        let folder = 'Notes';
        try { folder = n.container().name(); } catch(e) {}
        return {
            idx      : i,
            id       : n.id(),
            name     : n.name() || '(無題)',
            folder   : folder,
            created  : n.creationDate().toISOString(),
            modified : n.modificationDate().toISOString()
        };
    });
    JSON.stringify(result);
    """

    def _run_jxa(self, script: str, timeout: int = 120) -> str:
        """JXA スクリプトを実行し stdout を返す。エラー時は例外。"""
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"JXA エラー:\n{r.stderr.strip()}")
        return r.stdout.strip()

    def _run_applescript(self, script: str, timeout: int = 60) -> str:
        """AppleScript を実行し stdout を返す。エラー時は例外。"""
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"AppleScript エラー:\n{r.stderr.strip()}")
        return r.stdout.strip()

    def list_notes(self) -> list[dict]:
        """全ノードのメタデータ一覧を返す (body は取得しない)。"""
        raw = self._run_jxa(self._JXA_LIST_NOTES, timeout=600)#ChatGPT曰く、メモ件数が多く、ここでタイムアウトしているとのなので、タイムアウト制御時間を60秒から600秒に変更。＃２０２６/5/10
        return json.loads(raw)

    def get_note_body(self, note_idx: int) -> str:
        """idx 番目のノートの HTML body を返す (読み取り専用)。"""
        script = f"Application('Notes').notes()[{note_idx}].body();"
        return self._run_jxa(script, timeout=30)

    def save_attachments(self, note_id: str, dest_dir: str) -> list[str]:
        """
        指定ノートの添付ファイルを dest_dir に保存する。
        AppleScript の save コマンドを使用 (書き戻しではなくエクスポート)。
        保存できたファイルのパス一覧を返す。
        """
        # シングルクォートをエスケープ
        safe_dest = dest_dir.replace("'", "\\'")
        safe_id   = note_id.replace('"', '\\"')
        script = f"""
tell application "Notes"
    set theNote to first note whose id is "{safe_id}"
    set savedPaths to {{}}
    repeat with att in attachments of theNote
        set attName to name of att
        if attName is missing value then set attName to "attachment"
        set destFile to POSIX file "{safe_dest}/" & attName
        try
            save att in destFile
            set end of savedPaths to "{safe_dest}/" & attName
        end try
    end repeat
    return savedPaths
end tell
"""
        try:
            raw = self._run_applescript(script, timeout=60)
            # AppleScript リストはカンマ区切りで返ってくる
            if not raw:
                return []
            return [p.strip() for p in raw.split(",") if p.strip()]
        except Exception as e:
            log.warning(f"添付保存失敗 (note_id={note_id}): {e}")
            return []


# ══════════════════════════════════════════════════════════════════════════════
# 2. IMAGE PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

class ImageProcessor:
    """
    画像を 4 MB 以下に段階的に圧縮する。
    PNG に透過がある場合は PNG のまま縮小、それ以外は JPEG 変換。
    """

    def compress(self, image_path: str) -> tuple[bytes, str]:
        """
        (圧縮済みバイト列, 推奨拡張子) を返す。
        """
        with Image.open(image_path) as img:
            has_alpha = img.mode in ("RGBA", "LA", "P")
            if has_alpha:
                img = img.convert("RGBA")
                data = self._compress_png(img)
                return data, "png"
            else:
                img = img.convert("RGB")
                data = self._compress_jpeg(img)
                return data, "jpg"

    def _compress_png(self, img: Image.Image) -> bytes:
        scale = 1.0
        while scale > 0.05:
            buf = BytesIO()
            w = max(1, int(img.width * scale))
            h = max(1, int(img.height * scale))
            target = img.resize((w, h), Image.LANCZOS) if scale < 1.0 else img
            target.save(buf, format="PNG", optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue()
            scale = round(scale - 0.15, 2)
        # 最終手段: RGB に変換して JPEG へ
        return self._compress_jpeg(img.convert("RGB"))

    def _compress_jpeg(self, img: Image.Image) -> bytes:
        for quality in (85, 70, 55, 40, 25):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue()
        # それでも超える場合はリサイズ + 圧縮
        scale = 0.7
        while scale > 0.1:
            w = max(1, int(img.width * scale))
            h = max(1, int(img.height * scale))
            resized = img.resize((w, h), Image.LANCZOS)
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=40, optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue()
            scale = round(scale - 0.15, 2)
        raise ValueError(f"画像を {MAX_IMAGE_BYTES} bytes 以下に圧縮できませんでした")


# ══════════════════════════════════════════════════════════════════════════════
# 3. HTML → NOTION BLOCKS CONVERTER
# ══════════════════════════════════════════════════════════════════════════════

class HtmlToNotionConverter:
    """
    Apple Notes の HTML を Notion ブロックリストに変換する。
    深いネストは _children キーで表現し、NotionUploader が段階的に送信する。
    """

    def __init__(self, attachment_dir: Optional[str] = None):
        self.attachment_dir = attachment_dir  # 添付ファイルの保存先

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def convert(self, html: str) -> list[dict]:
        """HTML 文字列を Notion ブロックリストに変換する。"""
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body") or soup
        blocks: list[dict] = []
        for el in body.children:
            blocks.extend(self._convert_element(el))
        return self._merge_adjacent_paragraphs(blocks)

    # ── 要素変換 ──────────────────────────────────────────────────────────────

    def _convert_element(self, el) -> list[dict]:
        """1つの HTML 要素を Notion ブロックのリストに変換する。"""
        if isinstance(el, NavigableString):
            text = str(el).strip()
            return [self._para(self._plain_rt(text))] if text else []

        if not isinstance(el, Tag) or not el.name:
            return []

        tag = el.name.lower()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            # h4〜h6 は h3 に丸める (Notion は h3 まで)
            level = min(int(tag[1]), 3)
            return [self._heading(level, self._get_rt(el))]

        if tag == "p":
            rt = self._get_rt(el)
            return [self._para(rt)] if rt else []

        if tag == "ul":
            return self._convert_list(el, ordered=False)

        if tag == "ol":
            return self._convert_list(el, ordered=True)

        if tag == "table":
            return self._convert_table(el)

        if tag == "img":
            return self._convert_img(el)

        if tag == "object" and "image" in el.get("type", ""):
            # Apple Notes が <object type="image/..."> を使う場合
            return self._convert_img(el)

        if tag in ("pre", "code") and el.parent and el.parent.name == "pre":
            return []  # <pre><code> の内側 code タグはスキップ

        if tag == "pre":
            code = el.find("code")
            text = (code or el).get_text()
            return [self._code_block(text)]

        if tag == "blockquote":
            return [self._quote(self._get_rt(el))]

        if tag == "hr":
            return [{"type": "divider", "divider": {}}]

        if tag == "br":
            return []  # トップレベルの <br> は無視

        # div / section / article / header / footer → 子要素を再帰処理
        if tag in ("div", "section", "article", "header", "footer", "html", "body"):
            blocks: list[dict] = []
            for child in el.children:
                blocks.extend(self._convert_element(child))
            return blocks

        # その他 (span, em, strong etc. がトップレベルに来た場合)
        rt = self._get_rt(el)
        return [self._para(rt)] if rt else []

    # ── リスト変換 ─────────────────────────────────────────────────────────────

    def _convert_list(self, list_el: Tag, ordered: bool) -> list[dict]:
        blocks = []
        for li in list_el.find_all("li", recursive=False):
            blocks.append(self._convert_li(li, ordered))
        return blocks

    def _convert_li(self, li: Tag, ordered: bool) -> dict:
        """
        <li> を Notion リストブロックに変換する。
        ネストした <ul>/<ol> は _children キーに格納し、
        後段の NotionUploader が段階的に API 送信する。
        """
        block_type = "numbered_list_item" if ordered else "bulleted_list_item"

        inline_nodes: list = []
        children: list[dict] = []

        for child in li.children:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                children.extend(
                    self._convert_list(child, ordered=(child.name == "ol"))
                )
            else:
                inline_nodes.append(child)

        # インライン部分からリッチテキストを取得
        rt: list[dict] = []
        for node in inline_nodes:
            rt.extend(self._get_rt_from_node(node))
        rt = self._clean_rt(rt)

        block: dict = {
            "type": block_type,
            block_type: {
                "rich_text": rt,
                "color": "default",
            },
        }
        if children:
            block["_children"] = children
        return block

    # ── テーブル変換 ───────────────────────────────────────────────────────────

    def _convert_table(self, table_el: Tag) -> list[dict]:
        """
        Apple Notes HTML テーブルを Notion table ブロックに変換する。

        重要: Apple Notes の <td> 内は <p><span>テキスト</span></p> のように
        ネストされており、単純に .text や .string を使うとセルが空になる。
        _get_rt() を使ってネスト内のテキストを正しく抽出する。
        """
        rows = table_el.find_all("tr")
        if not rows:
            return []

        max_cols = max(
            (len(row.find_all(["td", "th"])) for row in rows), default=0
        )
        if max_cols == 0:
            return []

        has_col_header = bool(rows[0].find("th"))

        row_blocks: list[dict] = []
        for row in rows:
            cells = row.find_all(["td", "th"])
            row_cells: list[list[dict]] = []
            for i in range(max_cols):
                if i < len(cells):
                    # ▼ ここが肝: _get_rt() でネストタグ内テキストを正しく取得
                    cell_rt = self._clean_rt(self._get_rt(cells[i]))
                else:
                    cell_rt = [self._text_el("")]
                row_cells.append(cell_rt)

            row_blocks.append(
                {"type": "table_row", "table_row": {"cells": row_cells}}
            )

        table_block: dict = {
            "type": "table",
            "table": {
                "table_width": max_cols,
                "has_column_header": has_col_header,
                "has_row_header": False,
            },
            "_children": row_blocks,  # NotionUploader が別途 append
        }
        return [table_block]

    # ── 画像変換 ───────────────────────────────────────────────────────────────

    def _convert_img(self, el: Tag) -> list[dict]:
        """
        <img> / <object> を _image_pending マーカーとして返す。
        実際のアップロードは Migrator が担う。
        """
        src  = el.get("src") or el.get("data") or ""
        alt  = el.get("alt") or el.get("title") or ""

        # data URI を一時ファイルに保存
        if src.startswith("data:image"):
            m = re.match(r"data:image/(\w+);base64,(.+)", src, re.DOTALL)
            if m and self.attachment_dir:
                ext, b64 = m.group(1), m.group(2)
                fname = f"inline_{abs(hash(b64[:30]))}.{ext}"
                fpath = Path(self.attachment_dir) / fname
                fpath.write_bytes(base64.b64decode(b64))
                src = str(fpath)

        if src.startswith("file://"):
            src = src[7:]

        if not src:
            return []

        return [{"type": "_image_pending", "_src": src, "_alt": alt}]

    # ── リッチテキスト ─────────────────────────────────────────────────────────

    def _get_rt(self, el: Tag) -> list[dict]:
        """要素の子ノードからリッチテキストリストを返す。"""
        result: list[dict] = []
        for child in el.children:
            result.extend(self._get_rt_from_node(child))
        return result

    def _get_rt_from_node(
        self,
        node,
        ann: Optional[dict] = None,
    ) -> list[dict]:
        """
        ノードを再帰的に処理してリッチテキスト要素リストを返す。
        ann には現在の annotation 状態を渡す (immutable に扱う)。
        """
        if ann is None:
            ann = self._default_ann()

        if isinstance(node, NavigableString):
            text = str(node)
            if text == "\n":
                return []
            return [self._text_el(text, ann)] if text else []

        if not isinstance(node, Tag) or not node.name:
            return []

        tag = node.name.lower()
        new_ann = {**ann}  # シャローコピー

        # アノテーションの更新
        if tag in ("b", "strong"):
            new_ann["bold"] = True
        elif tag in ("i", "em"):
            new_ann["italic"] = True
        elif tag == "u":
            new_ann["underline"] = True
        elif tag in ("s", "del", "strike"):
            new_ann["strikethrough"] = True
        elif tag == "code":
            new_ann["code"] = True
        elif tag == "mark":
            new_ann["color"] = "yellow_background"
        elif tag == "span":
            style = node.get("style", "")
            if "font-weight" in style and re.search(r"font-weight\s*:\s*(bold|[6-9]\d\d)", style):
                new_ann["bold"] = True
            if "font-style" in style and "italic" in style:
                new_ann["italic"] = True
            if "text-decoration" in style:
                if "underline" in style:
                    new_ann["underline"] = True
                if "line-through" in style:
                    new_ann["strikethrough"] = True

        if tag == "a":
            href = node.get("href", "")
            parts: list[dict] = []
            for child in node.children:
                for el in self._get_rt_from_node(child, new_ann):
                    el_copy = self._deep_copy_rt_el(el)
                    if href:
                        el_copy["text"]["link"] = {"url": href}
                    parts.append(el_copy)
            return parts

        if tag == "br":
            return [self._text_el("\n", new_ann)]

        if tag in ("p", "div", "li"):
            # ブロック要素がインラインに混じっている場合の処理
            parts: list[dict] = []
            for child in node.children:
                parts.extend(self._get_rt_from_node(child, new_ann))
            return parts

        # その他タグは再帰
        parts: list[dict] = []
        for child in node.children:
            parts.extend(self._get_rt_from_node(child, new_ann))
        return parts

    def _clean_rt(self, rt: list[dict]) -> list[dict]:
        """
        末尾の空白・改行専用要素を除去し、2000文字超を分割する。
        Notion のリッチテキスト制約に適合させる。
        """
        # 末尾の空/改行要素を除去
        while rt and not rt[-1]["text"]["content"].strip():
            rt.pop()

        result: list[dict] = []
        for item in rt:
            content = item["text"]["content"]
            while len(content) > MAX_RICH_TEXT_LEN:
                chunk, content = content[:MAX_RICH_TEXT_LEN], content[MAX_RICH_TEXT_LEN:]
                new_el = self._deep_copy_rt_el(item)
                new_el["text"]["content"] = chunk
                result.append(new_el)
            if content:
                new_el = self._deep_copy_rt_el(item)
                new_el["text"]["content"] = content
                result.append(new_el)

        return result or [self._text_el("")]

    def _plain_rt(self, text: str) -> list[dict]:
        return [self._text_el(text)]

    def _text_el(self, text: str, ann: Optional[dict] = None) -> dict:
        return {
            "type": "text",
            "text": {"content": text, "link": None},
            "annotations": ann if ann is not None else self._default_ann(),
        }

    @staticmethod
    def _default_ann() -> dict:
        return {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        }

    @staticmethod
    def _deep_copy_rt_el(el: dict) -> dict:
        return {
            "type": el["type"],
            "text": {**el["text"]},
            "annotations": {**el["annotations"]},
        }

    # ── ブロック構築ヘルパー ───────────────────────────────────────────────────

    def _para(self, rt: list[dict]) -> dict:
        return {"type": "paragraph", "paragraph": {"rich_text": self._clean_rt(rt), "color": "default"}}

    def _heading(self, level: int, rt: list[dict]) -> dict:
        t = f"heading_{level}"
        return {
            "type": t,
            t: {"rich_text": self._clean_rt(rt), "color": "default", "is_toggleable": False},
        }

    def _code_block(self, text: str) -> dict:
        return {"type": "code", "code": {"rich_text": [self._text_el(text)], "language": "plain text"}}

    def _quote(self, rt: list[dict]) -> dict:
        return {"type": "quote", "quote": {"rich_text": self._clean_rt(rt), "color": "default"}}

    # ── 後処理 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _merge_adjacent_paragraphs(blocks: list[dict]) -> list[dict]:
        """空の paragraph が連続している場合に1つにまとめる (任意)。"""
        merged: list[dict] = []
        for b in blocks:
            if (
                b.get("type") == "paragraph"
                and not b["paragraph"]["rich_text"]
                and merged
                and merged[-1].get("type") == "paragraph"
                and not merged[-1]["paragraph"]["rich_text"]
            ):
                continue
            merged.append(b)
        return merged


# ══════════════════════════════════════════════════════════════════════════════
# 4. NOTION UPLOADER
# ══════════════════════════════════════════════════════════════════════════════

class NotionUploader:
    """
    Notion API ラッパー。

    多段ネストの解決戦略:
    ─────────────────────────────────────────────
    Notion API は1リクエストで子ブロックを2階層まで受け付けるが、
    実装を単純かつ確実にするため「親ブロックを先に作成し、
    子ブロックは取得した parent_id に対して別途 append」する方式を採用。
    再帰呼び出しにより何階層でも対応できる。
    ─────────────────────────────────────────────
    """

    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            }
        )

    # ── 内部 API 呼び出し ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        delay = BASE_RETRY_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt == MAX_RETRIES:
                    raise
                log.warning(f"ネットワークエラー (試行 {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", delay))
                log.info(f"レートリミット。{wait:.1f}s 待機...")
                time.sleep(wait)
                delay = wait * 2
                continue

            if not resp.ok:
                log.error(f"Notion API エラー {resp.status_code}: {resp.text[:400]}")
                resp.raise_for_status()

            return resp.json()

        raise RuntimeError(f"{MAX_RETRIES} 回リトライしましたが失敗しました")

    def _append_children(self, parent_id: str, blocks: list[dict]) -> list[dict]:
        """blocks を parent_id に追加し、作成されたブロックリストを返す。"""
        resp = self._request(
            "PATCH",
            f"/blocks/{parent_id}/children",
            json={"children": blocks},
        )
        return resp.get("results", [])

    def _get_children(self, block_id: str) -> list[dict]:
        """ブロックの子ブロック一覧を返す。"""
        resp = self._request("GET", f"/blocks/{block_id}/children?page_size=100")
        return resp.get("results", [])

    # ── ページ作成 ─────────────────────────────────────────────────────────────

    def create_page(
        self,
        parent_id: str,
        title: str,
        is_database: bool = True,
        folder: str = "",
        title_property: str = "Name",
    ) -> str:
        """
        Notion にページを作成してページ ID を返す。
        is_database=True ならデータベース配下、False ならページ配下に作成。
        """
        title = title[:2000]  # Notion の title 上限

        if is_database:
            body = {
                "parent": {"database_id": parent_id},
                "properties": {
                    title_property: {
                        "title": [{"text": {"content": title}}]
                    }
                },
            }
            # フォルダ情報を Select プロパティとして追加 (プロパティが存在する場合のみ有効)
            if folder:
                body["properties"]["フォルダ"] = {"select": {"name": folder}}
        else:
            body = {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": {
                        "title": [{"text": {"content": title}}]
                    }
                },
            }

        resp = self._request("POST", "/pages", json=body)
        return resp["id"]

    # ── ブロック アップロード (ネスト制限の核心部) ──────────────────────────────

    def upload_blocks(self, parent_id: str, blocks: list[dict]) -> None:
        """
        blocks を parent_id 配下に再帰的にアップロードする。

        Notion API の制約対応:
          - 1リクエストあたり最大 100 ブロック → BATCH_SIZE ごとに分割
          - _children は「親ブロック作成後」に別リクエストで追加
            → 何階層ネストしていても再帰で解決

        テーブルの table_row も同じ仕組みで処理される。
        """
        if not blocks:
            return

        # _image_pending と通常ブロックに分ける
        clean: list[dict]  = []     # API に送るブロック (_children なし)
        work: list[list[dict]] = []  # 対応する _children リスト

        for block in blocks:
            if block.get("type") == "_image_pending":
                # 画像は Migrator がアップロード済みのブロックとして渡すので
                # ここには来ないはずだが、念のためフォールバック
                fallback = self._img_fallback(block.get("_alt", ""))
                clean.append(fallback)
                work.append([])
                continue

            children = block.get("_children", [])
            # _children と内部フィールドを除いたクリーンなブロックを作成
            stripped = self._strip_private(block)
            clean.append(stripped)
            work.append(children)

        # BATCH_SIZE ごとに分割して送信
        for i in range(0, len(clean), BATCH_SIZE):
            batch_clean = clean[i : i + BATCH_SIZE]
            batch_work  = work[i : i + BATCH_SIZE]

            created = self._append_children(parent_id, batch_clean)

            # 作成されたブロックに _children を再帰的にアップロード
            for created_block, children in zip(created, batch_work):
                if not children:
                    continue
                created_id = created_block["id"]
                self.upload_blocks(created_id, children)  # ← 再帰

    # ── 画像アップロード ───────────────────────────────────────────────────────

    def upload_image(self, image_bytes: bytes, filename: str) -> Optional[str]:
        """
        Notion Files API で画像をアップロードし file_upload_id を返す。
        失敗時は None を返す (呼び出し元でフォールバック処理)。
        """
        try:
            # Step 1: アップロード URL を取得
            r1 = self._request(
                "POST",
                "/file_uploads",
                json={"filename": filename, "content_type": "image/jpeg"},
            )
            upload_url: str = r1.get("upload_url", "")
            file_id: str    = r1.get("id", "")
            if not upload_url or not file_id:
                return None

            # Step 2: ファイルをアップロード (認証不要の presigned URL)
            resp = requests.put(
                upload_url,
                data=image_bytes,
                headers={"Content-Type": "image/jpeg"},
                timeout=60,
            )
            resp.raise_for_status()
            return file_id

        except Exception as e:
            log.warning(f"Notion 画像アップロード失敗: {e}")
            return None

    def make_image_block(self, file_id: str) -> dict:
        return {
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": file_id}},
        }

    # ── ヘルパー ───────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_private(block: dict) -> dict:
        """_ で始まるプライベートキーを除いた新しい dict を返す。"""
        return {k: v for k, v in block.items() if not k.startswith("_")}

    @staticmethod
    def _img_fallback(alt: str) -> dict:
        label = f"🖼️ 画像: {alt}" if alt else "🖼️ 画像 (未移行)"
        return {
            "type": "callout",
            "callout": {
                "rich_text": [
                    {"type": "text", "text": {"content": label, "link": None},
                     "annotations": HtmlToNotionConverter._default_ann()}
                ],
                "icon":  {"type": "emoji", "emoji": "🖼️"},
                "color": "gray_background",
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
# 5. MIGRATOR (オーケストレーター)
# ══════════════════════════════════════════════════════════════════════════════

class Migrator:
    """Apple Notes → Notion の移行フローを管理する。"""

    def __init__(
        self,
        api_key: str,
        parent_id: str,
        is_database: bool = True,
        title_property: str = "Name",
        image_output_dir: str = "./migration_images",
    ):
        self.exporter       = AppleNotesExporter()
        self.image_proc     = ImageProcessor()
        self.uploader       = NotionUploader(api_key)
        self.parent_id      = parent_id
        self.is_database    = is_database
        self.title_property = title_property
        self.image_dir      = Path(image_output_dir)
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self._ok:    list[str] = []
        self._fail:  list[str] = []

    # ── 全ノート移行 ───────────────────────────────────────────────────────────

    def migrate_all(
        self,
        folder_filter: Optional[str] = None,
        limit: Optional[int] = None,
        skip: int = 0,
        dry_run: bool = False,
    ) -> None:
        log.info("ノート一覧を取得中...")
        notes = self.exporter.list_notes()
        log.info(f"合計 {len(notes)} 件のノートが見つかりました")

        if folder_filter:
            notes = [n for n in notes if n["folder"] == folder_filter]
            log.info(f"フォルダ「{folder_filter}」でフィルタ → {len(notes)} 件")

        notes = notes[skip:]
        if limit:
            notes = notes[:limit]

        for i, meta in enumerate(notes, 1):
            log.info(f"[{i}/{len(notes)}] 「{meta['name']}」 (フォルダ: {meta['folder']})")
            if dry_run:
                log.info("  [dry-run] スキップ")
                continue
            try:
                self.migrate_note(meta)
                self._ok.append(meta["name"])
            except Exception as e:
                log.error(f"  移行失敗: {e}")
                self._fail.append(meta["name"])

        self._print_summary()

    # ── 1ノート移行 ────────────────────────────────────────────────────────────

    def migrate_note(self, meta: dict) -> None:
        with tempfile.TemporaryDirectory(prefix="notes_export_") as tmpdir:
            # 添付ファイルを一時ディレクトリに保存
            att_paths = self.exporter.save_attachments(meta["id"], tmpdir)
            log.info(f"  添付: {len(att_paths)} 件")

            # HTML body を取得 (読み取り専用)
            html = self.exporter.get_note_body(meta["idx"])

            # HTML → Notion ブロック変換
            converter = HtmlToNotionConverter(attachment_dir=tmpdir)
            blocks = converter.convert(html)

            # 画像ブロックを処理
            blocks = self._resolve_images(blocks, meta["name"])

            # Notion にページを作成
            page_id = self.uploader.create_page(
                parent_id       = self.parent_id,
                title           = meta["name"],
                is_database     = self.is_database,
                folder          = meta["folder"],
                title_property  = self.title_property,
            )
            log.info(f"  ページ作成: {page_id}")

            # ブロックを再帰的にアップロード (ネスト制限を自動解決)
            self.uploader.upload_blocks(page_id, blocks)
            log.info(f"  ✓ 完了")

    # ── 画像解決 ───────────────────────────────────────────────────────────────

    def _resolve_images(self, blocks: list[dict], note_name: str) -> list[dict]:
        """
        _image_pending ブロックを実際の image / callout ブロックに置き換える。
        Notion Files API が使えない場合は image_output_dir にコピーしてフォールバック。
        """
        result: list[dict] = []
        for block in blocks:
            if block.get("type") == "_image_pending":
                resolved = self._upload_image(block["_src"], block["_alt"], note_name)
                result.append(resolved)
            elif "_children" in block:
                block["_children"] = self._resolve_images(block["_children"], note_name)
                result.append(block)
            else:
                result.append(block)
        return result

    def _upload_image(self, src: str, alt: str, note_name: str) -> dict:
        path = Path(src)

        # ローカルファイルが存在しない場合はフォールバック
        if not path.exists():
            log.warning(f"  画像が見つかりません: {src}")
            return NotionUploader._img_fallback(alt or str(path.name))

        try:
            # 圧縮
            img_bytes, ext = self.image_proc.compress(str(path))
            safe_name = re.sub(r"[^\w\-.]", "_", path.stem) + f".{ext}"
            size_kb = len(img_bytes) // 1024
            log.info(f"  画像圧縮: {path.name} → {safe_name} ({size_kb} KB)")

            # ローカルにも保存 (バックアップ)
            local_copy = self.image_dir / safe_name
            local_copy.write_bytes(img_bytes)

            # Notion Files API でアップロード試行
            file_id = self.uploader.upload_image(img_bytes, safe_name)
            if file_id:
                log.info(f"  画像アップロード成功: {file_id}")
                return self.uploader.make_image_block(file_id)

        except Exception as e:
            log.warning(f"  画像処理エラー ({path.name}): {e}")

        # フォールバック: ローカルパスを案内する callout
        local_path = str(self.image_dir / safe_name) if path.exists() else str(path)
        return {
            "type": "callout",
            "callout": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"🖼️ 画像ファイル: {local_path}",
                            "link": None,
                        },
                        "annotations": HtmlToNotionConverter._default_ann(),
                    }
                ],
                "icon":  {"type": "emoji", "emoji": "📁"},
                "color": "gray_background",
            },
        }

    # ── サマリー出力 ───────────────────────────────────────────────────────────

    def _print_summary(self) -> None:
        print("\n" + "═" * 60)
        print(f"移行完了: 成功 {len(self._ok)} 件 / 失敗 {len(self._fail)} 件")
        if self._fail:
            print("失敗ノート一覧:")
            for name in self._fail:
                print(f"  ✗ {name}")
        print("═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# 6. エントリーポイント
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apple Notes → Notion 移行スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # データベースへ全ノートを移行
  python migrate.py --api-key secret_xxx --database-id abc123

  # 特定フォルダのみ (上限10件, ドライラン)
  python migrate.py --api-key secret_xxx --database-id abc123 \\
      --folder "仕事" --limit 10 --dry-run

  # ページ配下に移行 (データベースではなく通常ページ)
  python migrate.py --api-key secret_xxx --parent-page-id abc123

環境変数でも指定可能:
  NOTION_API_KEY, NOTION_PARENT_ID
""",
    )
    parser.add_argument("--api-key",        default=os.environ.get("NOTION_API_KEY"),
                        help="Notion Integration Token (secret_xxx)")
    parser.add_argument("--database-id",    default=os.environ.get("NOTION_DATABASE_ID"),
                        help="移行先 Notion データベース ID")
    parser.add_argument("--parent-page-id", default=None,
                        help="移行先 Notion ページ ID (--database-id の代わり)")
    parser.add_argument("--title-property", default="Name",
                        help="データベースのタイトルプロパティ名 (デフォルト: Name)")
    parser.add_argument("--folder",         default=None,
                        help="移行対象のフォルダ名でフィルタリング")
    parser.add_argument("--limit",          type=int, default=None,
                        help="移行するノートの最大件数")
    parser.add_argument("--skip",           type=int, default=0,
                        help="先頭 N 件をスキップ")
    parser.add_argument("--image-dir",      default="./migration_images",
                        help="画像の保存先ディレクトリ (デフォルト: ./migration_images)")
    parser.add_argument("--dry-run",        action="store_true",
                        help="実際には移行せず動作確認のみ")
    parser.add_argument("--verbose",        action="store_true",
                        help="詳細ログを表示")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # バリデーション
    if not args.api_key:
        parser.error("--api-key または NOTION_API_KEY 環境変数が必要です")

    is_database = True
    parent_id   = args.database_id

    if args.parent_page_id:
        parent_id   = args.parent_page_id
        is_database = False
    elif not parent_id:
        parser.error("--database-id または --parent-page-id が必要です")

    # 移行実行
    migrator = Migrator(
        api_key         = args.api_key,
        parent_id       = parent_id,
        is_database     = is_database,
        title_property  = args.title_property,
        image_output_dir= args.image_dir,
    )

    migrator.migrate_all(
        folder_filter = args.folder,
        limit         = args.limit,
        skip          = args.skip,
        dry_run       = args.dry_run,
    )


if __name__ == "__main__":
    main()
