"""
剪藏：手机上分享进来的文章，存在笔记库 Spark/剪藏/ 下，一篇一个 .md，图片在 Spark/剪藏/attachments/。
论文（Spark/论文/）的文件格式和剪藏完全一样，两个合称「剪藏式文件夹」（CLIP_DIRS）：每个文件夹各有自己的
索引.md、attachments/、.renames.json、.edits.json，都由这篇文档所在的文件夹推出来（clip_folder）。

手机（apps/mobile-ios/Spark/Clips.swift）抓全文、写摘要和标签，交一个 save 任务过来（core/remote_tasks.py）；
这里负责：按原地址把图片下到 attachments/（任务里只带地址，GitHub 的 Issue 放不下图片），再重写一遍索引。
索引按文件头里的标签归组，跟手机上的写法一致：两篇以上用到的标签成一组，剩下的放「其他」，组里新存的在前。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import threading
import time

from core import atomic
from core import doctime
from core import vault

CLIPS_DIR = f"{vault.SPARK_DIRNAME}/剪藏"
PAPERS_DIR = f"{vault.SPARK_DIRNAME}/论文"
CLIP_DIRS = (CLIPS_DIR, PAPERS_DIR)   # 剪藏式文件夹：就这两个（同手机 PathRules.clipFolders）
INDEX_NAME = "索引.md"
MAX_IMAGES = 80
MAX_PICKED = 20             # 剪藏综合小结一次最多挑几篇（core/review.py）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_RE_IMAGE_KEY = re.compile(rf"^(?:{'|'.join(map(re.escape, CLIP_DIRS))})/attachments/[0-9a-f]{{16}}\.(?:jpg|png|gif|webp)\Z")


def is_note_path(path: str) -> bool:
    """笔记库里能拿来写综合小结的一篇：Spark/ 或 output/ 下的 .md，不带 .. 和点开头的文件夹。"""
    if not path.startswith((f"{vault.SPARK_DIRNAME}/", f"{vault.REPORTS_DIRNAME}/")) or not path.endswith(".md"):
        return False
    return not any(not p or p.startswith(".") for p in path.split("/")) and "\\" not in path


def clip_folder(path: str) -> str:
    """剪藏式文档（Spark/剪藏/ 或 Spark/论文/ 下直接的一篇 .md，不含索引、点开头的、子文件夹里的）所在的文件夹；
    不是剪藏式文档返回空串（同手机 PathRules.clipFolderOf）。"""
    if not isinstance(path, str) or not path.endswith(".md"):
        return ""
    for folder in CLIP_DIRS:
        if path.startswith(folder + "/"):
            rest = path[len(folder) + 1:]
            return folder if "/" not in rest and rest != INDEX_NAME and not rest.startswith(".") else ""
    return ""


def is_clip_path(path: str) -> bool:
    """剪藏式文档：Spark/剪藏/ 或 Spark/论文/ 下直接的一篇 .md（不含索引、不在子文件夹里）。"""
    return bool(clip_folder(path))


def is_paper_path(path: str) -> bool:
    """论文：Spark/论文/ 下直接的一篇 .md（同手机 PathRules.isPaper）。"""
    return clip_folder(path) == PAPERS_DIR


def kind_name(folder: str) -> str:
    """剪藏式文件夹的种类名：剪藏、论文（就是文件夹名）。"""
    return folder.rsplit("/", 1)[-1]


def _journal_folder(rel: str) -> str:
    """一篇的修改记录、改名记录记在哪个文件夹：论文记在 Spark/论文/，别的（剪藏、复盘、随手记、做了笔记的文章）照旧记在 Spark/剪藏/。"""
    return clip_folder(rel) or CLIPS_DIR


# 迁移记录（tools/migrate_times.py 写，手机同步后也读）：
# {"renames": {旧相对路径: 新相对路径}, "rewritten": {现在的相对路径: {"before": [迁移前的 SHA-256…], "after": 迁移后的 SHA-256}}}
# 早先的版本整个文件就是 {旧: 新}，照样认。很少写（迁移时），会同步到手机
RENAMES_NAME = ".renames.json"
# 收下的手机修改（core/remote_tasks 写，常写，不放进上面那份）：
# {相对路径: [{"after": 写进去那版的 SHA-256, "content": 手机那份的 SHA-256, "ancestors": [它是从哪几版改来的…]}, …]}，每篇留最后 16 条
EDITS_NAME = ".edits.json"
EDITS_KEEP = 16
LOCK_DIR = ".spark-locks"   # 点开头的文件夹不同步（core/cable_sync、github_sync）

_JOURNAL_LOCK = threading.RLock()
# 写笔记库里的文档（手机交来的存、删）和清理工具移走图片，用这一把锁排开：清理工具在锁里对每张图最后查一遍再移走，
# 这时候不会有一篇正在写、刚引用了它的文档（core/cleanup.py）。先拿这把，再拿 journal_lock，顺序不反过来
DOC_LOCK = threading.RLock()
log = logging.getLogger(__name__)


class JournalError(RuntimeError):
    """迁移记录、修改记录读不出来：不写（坏文件已经挪到一边）。"""


def bad_copies(path: str) -> list[str]:
    folder, name = os.path.split(path)
    try:
        return sorted(n for n in os.listdir(folder) if n.startswith(name + ".bad-"))
    except OSError:
        return []


def journal_problems(root: str) -> list[str]:
    """给人看的：迁移记录坏过、挪到一边还没处理（同步状态里显示）。两个剪藏式文件夹各查各的。"""
    out = []
    for folder in CLIP_DIRS:
        bad = bad_copies(journal_path(root, folder))
        if bad:
            out.append(f"{kind_name(folder)}的迁移记录 {RENAMES_NAME} 坏过，挪到了 {bad[-1]}：在有人处理之前不再写它（改名不会跟着记），"
                       f"处理好后删掉或挪走 .bad- 那份")
    return out


def journal_path(root: str, folder: str = CLIPS_DIR) -> str:
    return os.path.join(root, *folder.split("/"), RENAMES_NAME)


def edits_path(root: str, folder: str = CLIPS_DIR) -> str:
    return os.path.join(root, *folder.split("/"), EDITS_NAME)


@contextlib.contextmanager
def journal_lock(root: str, folder: str = CLIPS_DIR):
    """读改写这两份记录时加锁：同一个进程里几个线程（数据线同步、后台任务、GitHub 任务）用 threading 锁，
    跟迁移脚本这类别的进程之间用 flock（锁文件在不同步的 .spark-locks/ 里，每个剪藏式文件夹一份）。"""
    folder = os.path.join(root, *folder.split("/"), LOCK_DIR)
    os.makedirs(folder, exist_ok=True)
    with _JOURNAL_LOCK:
        with open(os.path.join(folder, "journal.lock"), "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_json(path: str, writer: bool):
    """读一份记录：没有返回 None；JSON 坏了就把文件挪到一边（.bad-时间），读的人当没有，写的人报 JournalError（绝不拿空表盖掉）。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        if writer:
            raise JournalError(f"{os.path.basename(path)} 读不出来：{e}") from e
        log.warning("%s 读不出来：%s", path, e)
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("不是一个对象")
        return data
    except ValueError as e:
        bad = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 2
        while os.path.exists(bad):   # 同一秒又坏了一次：不盖掉上一份
            bad = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
            n += 1
        try:
            os.rename(path, bad)
        except OSError:
            bad = path
        msg = f"{os.path.basename(path)} 坏了（{e}），挪到了 {bad}，里面的记录先不用"
        log.warning(msg)
        if writer:
            raise JournalError(msg) from e
        return None


def _journal_of(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    if "renames" in raw or "rewritten" in raw:
        renames, rewritten = raw.get("renames"), raw.get("rewritten")
    else:
        renames, rewritten = raw, {}
    out = {"renames": {k: v for k, v in (renames if isinstance(renames, dict) else {}).items()
                       if isinstance(k, str) and isinstance(v, str)},
           "rewritten": {k: v for k, v in (rewritten if isinstance(rewritten, dict) else {}).items()
                         if isinstance(k, str) and isinstance(v, dict)}}
    # 清理工具合并重复图片的记录（core/cleanup.py）：{丢掉的那张: 留下的那张}。手机交来合并前那一版时照它把引用换过去
    images = raw.get("images") if "renames" in raw or "rewritten" in raw else None
    if isinstance(images, dict):
        images = {k: v for k, v in images.items() if isinstance(k, str) and isinstance(v, str)}
        if images:
            out["images"] = images
    return out


def load_journal(root: str, folder: str = CLIPS_DIR) -> dict:
    """迁移记录（读）。没有、读不出来都当空的；坏了的挪到一边并记日志。返回 {"renames": {...}, "rewritten": {...}}。
    folder：哪个剪藏式文件夹的那份（默认剪藏：清理工具合并图片、改写引用记的也在这份里，管整个笔记库）。"""
    return _journal_of(_read_json(journal_path(root, folder), writer=False))


def update_journal(root: str, change) -> dict:
    """加锁读改写迁移记录：change(journal) 原地改。读不出来报 JournalError，不写。
    旁边还有挪开的坏文件（.renames.json.bad-…）也不写：从空表开始写会把改名记录同步成「没有」，要人先处理。
    （修改记录 .edits.json 坏了就从空的重新记：只是记账，丢了最多让手机下一次修改算成冲突，看得到。）"""
    with journal_lock(root):
        bad = bad_copies(journal_path(root))
        if bad:
            raise JournalError(f"{RENAMES_NAME} 坏过、挪到了 {bad[-1]}，还没处理：先不写")
        journal = _journal_of(_read_json(journal_path(root), writer=True))
        change(journal)
        os.makedirs(os.path.dirname(journal_path(root)), exist_ok=True)
        atomic.write_json(journal_path(root), journal, indent=1)
        return journal


def load_edits(root: str, folder: str = CLIPS_DIR) -> dict:
    raw = _read_json(edits_path(root, folder), writer=False) or {}
    return {k: [e for e in v if isinstance(e, dict)] for k, v in raw.items() if isinstance(k, str) and isinstance(v, list)}


def edits_problem(root: str) -> str:
    """修改记录（墓碑也在里面）现在靠不靠得住：读不出来、不是 JSON 对象、或者旁边有挪开的坏文件（.edits.json.bad-…）
    就返回说明（带位置），不然空串。只读，不挪文件。清理工具据此暂停移走图片：墓碑丢了，废纸篓里文章的图就没人护着了。
    两个剪藏式文件夹的都查，返回第一个有问题的。"""
    for folder in CLIP_DIRS:
        problem = _edits_problem(edits_path(root, folder))
        if problem:
            return problem
    return ""


def _edits_problem(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = None
    except OSError as e:
        return f"{EDITS_NAME} 读不出来（{e.strerror or e}）：{path}"
    if text is not None:
        try:
            if not isinstance(json.loads(text), dict):
                raise ValueError
        except ValueError:
            return f"{EDITS_NAME} 坏了：{path}"
    bad = bad_copies(path)
    if bad:
        return f"{EDITS_NAME} 坏过，挪到了 {os.path.join(os.path.dirname(path), bad[-1])}"
    return ""


def update_edits(root: str, change, folder: str = CLIPS_DIR) -> None:
    """加锁读改写修改记录。坏了就挪到一边、从空的重新记（只是记账：丢了最多让手机下一次修改算成冲突，看得到）。
    folder：哪个剪藏式文件夹的那份（_journal_folder：论文记在论文文件夹，别的都记在剪藏文件夹）。"""
    path = edits_path(root, folder)
    with journal_lock(root, folder):
        try:
            raw = _read_json(path, writer=True) or {}
        except JournalError as e:
            log.warning("%s；从空的重新记", e)
            if os.path.exists(path):
                raise   # 读不出来又挪不开：不盖
            raw = {}
        edits = {k: [e for e in v if isinstance(e, dict)] for k, v in raw.items() if isinstance(k, str) and isinstance(v, list)}
        change(edits)
        # 墓碑、撤回、见过的任务标识不参与截断（截掉了，晚到的删除、重复的存就认不出来了）；只截修改记录
        edits = {k: [e for e in v if _kept(e)] + [e for e in v if not _kept(e)][-EDITS_KEEP:] for k, v in edits.items() if v}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic.write_json(path, edits)


def record_edit(root: str, rel: str, phone_text: str, written_text: str, bases: list[str]) -> None:
    """收下了一次手机修改：记下写进去的那版（after）、手机那份（content，跟 after 只差电脑接回的时间行），
    和它是从哪几版改来的（ancestors = 手机带的 bases + 手机那份）。之后据此认：
    - 手机下一次改带的 base 是它本地那份：跟 after 是同一版；
    - 晚到的一份修改，内容是现在这版的祖先：它的改动已经在里面。"""
    after, content = _sha256(written_text), _sha256(phone_text)
    entry = {"after": after, "content": content, "ancestors": list(dict.fromkeys([*bases, content]))}

    def change(edits):
        edits.setdefault(rel, []).append(entry)
    update_edits(root, change, _journal_folder(rel))


_KEY_RE = re.compile(r"[A-Za-z0-9-]{8,64}")


def _kept(e: dict) -> bool:
    return any(k in e for k in ("deleted", "cancelled", "seen"))


SEEN_KEEP = 256


def record_seen(root: str, rel: str, key: str) -> None:
    """电脑收到过的这篇的存（任务里的 key）：删这篇时全记进墓碑，这几个存晚到、重复到都不再建回来。每篇留最近 256 个。"""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return

    def change(edits):
        entries = edits.setdefault(rel, [])
        seen = next((e for e in entries if isinstance(e.get("seen"), list)), None)
        if seen is None:
            seen = {"seen": []}
            entries.insert(0, seen)
        if key not in seen["seen"]:
            seen["seen"] = (seen["seen"] + [key])[-SEEN_KEEP:]
    update_edits(root, change, _journal_folder(rel))


def seen_keys(root: str, rel: str) -> set[str]:
    out: set[str] = set()
    for e in load_edits(root, _journal_folder(rel)).get(rel, []):
        if isinstance(e.get("seen"), list):
            out.update(k for k in e["seen"] if isinstance(k, str))
    return out


def record_deleted(root: str, rel: str, shas: set[str], keys: set[str] = frozenset(),
                   images: list[str] = (), source: str = "phone") -> None:
    """手机上删掉、电脑移进了废纸篓（或者删的时候电脑上还没建出来）的一篇：记下删掉的那几版内容的 SHA-256 和
    手机删之前交过的那几个存的任务标识（keys），合称墓碑。之后晚到的那几个存不再把它建回来（core/remote_tasks.save_file）。
    跟修改记录记在同一份 .edits.json 里。
    images：这篇提到的图片（相对笔记库的路径）。文章在废纸篓里、还可能放回，清理工具不动这几张，见 held_images。
    source：phone（手机废纸篓，彻底删除时手机交 purge_images 来释放）或 mac（Mac 应用、网页删的，没有手机废纸篓）。"""
    good = sorted(h for h in shas if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h))
    ks = sorted(k for k in keys if isinstance(k, str) and _KEY_RE.fullmatch(k))
    if not good and not ks:
        return
    entry: dict = {"deleted": good, "keys": ks}
    imgs = sorted({i for i in images if isinstance(i, str)})
    if imgs:
        entry["images"] = imgs
        entry["from"] = "mac" if source == "mac" else "phone"

    def change(edits):
        edits.setdefault(rel, []).append(entry)
    update_edits(root, change, _journal_folder(rel))


def held_images(root: str) -> dict[str, list[tuple[str, str]]]:
    """墓碑里记着的图片：{图片路径: [(删掉的那篇, phone 或 mac)]}。手机彻底删除（purge_images）或放回原处（墓碑清掉）之前一直算。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for rel, entries in [kv for folder in CLIP_DIRS for kv in load_edits(root, folder).items()]:   # 两个剪藏式文件夹的墓碑都算
        for e in entries:
            if isinstance(e.get("deleted"), list) and isinstance(e.get("images"), list):
                src = "mac" if e.get("from") == "mac" else "phone"
                for i in e["images"]:
                    if isinstance(i, str):
                        out.setdefault(i, []).append((rel, src))
    return out


def release_images(root: str, rel: str) -> None:
    """手机废纸篓里彻底删除了这篇：墓碑还留着（挡晚到的旧任务），只是它记着的图片不再算在用。"""
    def change(edits):
        for e in edits.get(rel, []):
            if "deleted" in e:
                e.pop("images", None)
                e.pop("from", None)
    update_edits(root, change, _journal_folder(rel))


def clear_deleted(root: str, rel: str) -> None:
    """用户放回原处了、或者重新存了一篇：这篇的墓碑去掉。"""
    def change(edits):
        if rel in edits:
            edits[rel] = [e for e in edits[rel] if "deleted" not in e]
    update_edits(root, change, _journal_folder(rel))


def deleted_shas(root: str, rel: str) -> set[str]:
    """这篇删掉时记下的各版内容（墓碑），没删过是空的。"""
    out: set[str] = set()
    for e in load_edits(root, _journal_folder(rel)).get(rel, []):
        if isinstance(e.get("deleted"), list):
            out.update(h for h in e["deleted"] if isinstance(h, str))
    return out


def deleted_keys(root: str, rel: str) -> set[str]:
    """墓碑里记的、删之前交过的那几个存的任务标识。"""
    out: set[str] = set()
    for e in load_edits(root, _journal_folder(rel)).get(rel, []):
        if isinstance(e.get("keys"), list):
            out.update(k for k in e["keys"] if isinstance(k, str))
    return out


def record_cancelled(root: str, rel: str, keys: list[str]) -> None:
    """用户放回原处时取消的那几个删除任务（任务里的 key）：晚到的这个删除不再执行。"""
    ks = sorted(k for k in keys if isinstance(k, str) and _KEY_RE.fullmatch(k))
    if not ks:
        return

    def change(edits):
        edits.setdefault(rel, []).append({"cancelled": ks})
    update_edits(root, change, _journal_folder(rel))


def cancelled_keys(root: str, rel: str) -> set[str]:
    out: set[str] = set()
    for e in load_edits(root, _journal_folder(rel)).get(rel, []):
        if isinstance(e.get("cancelled"), list):
            out.update(k for k in e["cancelled"] if isinstance(k, str))
    return out


def known_versions(root: str, rel: str) -> set[str]:
    """修改记录里这篇的各版（写进去的、手机交来的、它们的来历）。"""
    out: set[str] = set()
    for e in load_edits(root, _journal_folder(rel)).get(rel, []):
        out.update(h for h in [e.get("after"), e.get("content"), *(e.get("ancestors") or [])] if isinstance(h, str))
    return out


def move_edits(root: str, pairs: list[tuple[str, str]]) -> None:
    """剪藏改了名：修改记录跟着挪到新名字下（各在各的剪藏式文件夹里改）。"""
    for folder in dict.fromkeys(_journal_folder(old) for old, _new in pairs):
        # 新旧名不在同一个文件夹的不挪：挪过去就挂在别的文件夹那份记录里，以后读不到
        mine = [(old, new) for old, new in pairs if _journal_folder(old) == folder == _journal_folder(new)]

        def change(edits, mine=mine):
            for old, new in mine:
                if old in edits:
                    edits[new] = edits.get(new, []) + edits.pop(old)
        update_edits(root, change, folder)


def same_content(root: str, rel: str, phone_sha: str, current_sha: str) -> bool:
    """手机那份（phone_sha）收下时写成了现在这版（只差接回的时间行）。"""
    return any(e.get("after") == current_sha and e.get("content") == phone_sha for e in load_edits(root, _journal_folder(rel)).get(rel, []))


def ancestors(root: str, rel: str, current_sha: str) -> set[str]:
    """现在这版是从哪些版本一路改来的（只算收下的手机修改记着的；顺着往前找）。"""
    entries = load_edits(root, _journal_folder(rel)).get(rel, [])
    out: set[str] = set()
    todo = [current_sha]
    while todo:
        h = todo.pop()
        for e in entries:
            if e.get("after") == h:
                for a in e.get("ancestors") or []:
                    if isinstance(a, str) and a not in out:
                        out.add(a)
                        todo.append(a)
    return out


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve(table: dict, rel: str) -> str:
    """顺着改名记录找到最后的名字（a→b→c 得 c；绕圈的停下）。"""
    seen = {rel}
    while isinstance(table.get(rel), str) and table[rel] not in seen:
        rel = table[rel]
        seen.add(rel)
    return rel


def renamed(root: str, rel: str) -> str:
    """手机上还按旧名字交来的剪藏（发件箱里没交的修改、删除）：旧文件已经没有、改名记录里有，就换成新名字。"""
    if not is_clip_path(rel) or os.path.exists(os.path.join(root, *rel.split("/"))):
        return rel
    new = resolve(load_journal(root, clip_folder(rel))["renames"], rel)   # 论文按论文文件夹的改名记录
    return new if clip_folder(new) == clip_folder(rel) else rel


def migrated_from(root: str, rel: str, base: str, current: bytes) -> bool:
    """手机改的是迁移前那一版（base 是迁移前的指纹）、电脑上这篇迁移之后没再改过：算同一版，手机的修改照收。"""
    import hashlib
    entry = load_journal(root)["rewritten"].get(rel) or {}
    before = entry.get("before")
    return (isinstance(before, list) and base in before
            and entry.get("after") == hashlib.sha256(current).hexdigest())


def image_key_ok(key: str, url: str) -> bool:
    return bool(isinstance(key, str) and _RE_IMAGE_KEY.match(key)
                and isinstance(url, str) and re.match(r"^https?://[^\s/]+\S*$", url) and len(url) <= 2000)


def download_images(images: dict, root: str = "") -> tuple[int, int]:
    """把还没有的图片按原地址下下来。返回 (下好的, 没下成的)。"""
    from core import sources   # 抓网页那一套（带防内网地址的检查），用到时再导入
    root = root or vault.vault_root()
    ok = bad = 0
    for key, url in list(images.items())[:MAX_IMAGES]:
        full = os.path.join(root, *key.split("/"))
        if os.path.exists(full):
            continue
        data = b""
        # 公众号原图下不来、太大时退回去下 /640（同手机 Clipper.fallbackURL），文件名不变
        for candidate in (url, fallback_url(url)):
            if not candidate:
                continue
            try:
                data = sources.http_get(candidate, timeout=20, max_bytes=MAX_IMAGE_BYTES)
            except Exception:  # noqa: BLE001  一张图下不来不影响文章
                data = b""
            if data:
                break
        if not data:
            bad += 1
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".partial"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        ok += 1
    return ok, bad


# ---------------------------------------------------------------- 索引

def frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k.strip()] = v
    return out


def _tags(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        val = json.loads(raw or "[]")
        if isinstance(val, list):
            return [str(t) for t in val]
    except ValueError:
        pass
    return [t.strip(" \"'") for t in str(raw).strip("[] ").split(",") if t.strip(" \"'")]


def _later_on(v) -> bool:
    v = str(v or "").strip().strip("'\"").strip()
    return bool(v) and v.lower() != "false"


def load_clips(root: str = "", rel_dir: str = CLIPS_DIR) -> list[dict]:
    """一个剪藏式文件夹（rel_dir：剪藏或论文）里的各篇，读文件头。"""
    root = root or vault.vault_root()
    folder = os.path.join(root, *rel_dir.split("/"))
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if clip_folder(f"{rel_dir}/{name}") != rel_dir:
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                fm = frontmatter(f.read(8192))
        except OSError:
            continue
        stem = name[:-3]
        d = stem[:8]
        # 收录那天以文件头的 clipped 为准；文件名前面的日期现在是发表那天，只在没有文件头的老文件里当收录日期用
        clipped = fm.get("clipped") or (f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else "")
        out.append({"name": name, "title": fm.get("title") or stem, "source": fm.get("source") or "",
                    "clipped": clipped, "published": fm.get("published") or "",
                    "clipped_at": fm.get("clipped_at") or "", "published_at": fm.get("published_at") or "",
                    "tags": _tags(fm.get("tags")), "tldr": fm.get("tldr") or "", "url": fm.get("url") or "",
                    # 稍后再读（文件头 later:，认法同 core/annotations.later_value）、占位剪藏（stub: true）
                    "later": _later_on(fm.get("later")), "stub": str(fm.get("stub") or "").strip() == "true"})
    return out


def order_clipped(c: dict) -> tuple:
    """「按收录时间」排（倒序用）：收录那一刻，没有时刻的排在同一天有时刻的后面，再按文件名。
    跟手机 ClipShelf、Mac 应用 ClipSort 一样。"""
    return doctime.sort_key(c.get("clipped_at"), c.get("clipped")) + (c.get("name") or c.get("path") or "",)


def order_published(c: dict) -> tuple:
    """「按发布时间」排（倒序用）：发表那一刻；只知道哪天的排在同一天有时刻的后面；
    一样的再按收录那一刻、文件名。不知道发表时间的按收录时间算。"""
    if c.get("published") or c.get("published_at"):
        first = doctime.sort_key(c.get("published_at"), c.get("published"))
    else:
        first = doctime.sort_key(c.get("clipped_at"), c.get("clipped"))
    return first + order_clipped(c)


def render_index(clips: list[dict], rel_dir: str = CLIPS_DIR) -> str:
    clips = sorted(clips, key=order_clipped, reverse=True)
    count: dict[str, int] = {}
    for c in clips:
        for t in set(c["tags"]):
            count[t] = count.get(t, 0) + 1
    groups = [t for t, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2]
    lines = [f"# {kind_name(rel_dir)}索引", "", f"共 {len(clips)} 篇。按主题归组，一篇可能出现在几个主题下；每组里新存的在前。", ""]

    def row(c: dict) -> str:
        src = "，".join(x for x in (c["source"], f"{c['clipped']} 存" if c["clipped"] else "") if x)
        title = c["title"].replace("]", "］")
        return f"- [{title}](<{c['name']}>)（{src}）" + (f"：{c['tldr']}" if c["tldr"] else "")

    for g in groups:
        members = [c for c in clips if g in c["tags"]]
        lines += [f"## {g}（{len(members)}）", ""] + [row(c) for c in members] + [""]
    rest = [c for c in clips if not any(t in groups for t in c["tags"])]
    if rest:
        lines += [f"## {'其他' if groups else '全部'}（{len(rest)}）", ""] + [row(c) for c in rest] + [""]
    return "\n".join(lines)


def rebuild_index(root: str = "", rel_dir: str = CLIPS_DIR) -> str:
    """重写一个剪藏式文件夹（剪藏或论文）的索引.md，另一个文件夹的不动。"""
    root = root or vault.vault_root()
    path = os.path.join(root, *rel_dir.split("/"), INDEX_NAME)
    atomic.write_text(path, render_index(load_clips(root, rel_dir), rel_dir))
    return path


# ---------------------------------------------------------------- 单张图：重下、删除（Mac App 的阅读页）

_RE_LOCAL_SRC = re.compile(r"^attachments/[0-9a-f]{16}\.(?:jpg|png|gif|webp)$")
_RE_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)


def local_src_ok(src: str) -> bool:
    return bool(isinstance(src, str) and _RE_LOCAL_SRC.match(src))


def local_name(url: str) -> str:
    """图片的本地文件名：attachments/<原地址的 sha1 前 16 位>.<扩展名>（同手机 Clipper.localName）。"""
    import hashlib
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(url)
    fmt = parse_qs(parts.query).get("wx_fmt")
    ext = (fmt[0] if fmt else os.path.splitext(parts.path)[1][1:]).lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("jpg", "png", "gif", "webp"):
        ext = "jpg"
    return f"attachments/{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}.{ext}"


def page_images(page_html: str, base: str) -> dict:
    """网页里的图：{本地文件名: 原地址}。取 data-src 或 src（带不带引号都认），同手机 HTMLToMarkdown。"""
    import html as html_lib
    from urllib.parse import urljoin
    out = {}
    for attrs in _RE_IMG_TAG.findall(page_html):
        m = None
        for name in ("data-src", "src"):
            m = (re.search(rf'(?:^|\s){name}\s*=\s*"([^"]*)"', attrs) or re.search(rf"(?:^|\s){name}\s*=\s*'([^']*)'", attrs)
                 or re.search(rf"(?:^|\s){name}\s*=\s*([^\s\"'>]+)", attrs))
            if m:
                break
        if not m or m.group(1).startswith("data:"):
            continue
        url = urljoin(base, html_lib.unescape(m.group(1)))
        if url.startswith(("http://", "https://")):
            out.setdefault(local_name(url), url)
    return out


def without_image(text: str, src: str) -> str:
    """删掉一张图：单独一行的 ![](地址)（同手机 DocEdit.removingImage）。图片文件留着，别的文章可能也用。"""
    lines = [ln for ln in text.split("\n") if not (ln.strip().startswith("![") and f"]({src})" in ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def redownload_image(clip_text: str, src: str, root: str = "", rel_dir: str = CLIPS_DIR) -> str:
    """打开原文找到这张图的地址再下。成功返回空串，不成返回原因。"""
    from core import sources
    url = frontmatter(clip_text).get("url") or ""
    if not url.startswith(("http://", "https://")):
        return "这篇没有记原文地址"
    try:
        # 小红书给电脑浏览器的是登录页，按手机浏览器去要
        ua = {"user_agent": IPHONE_UA} if is_xhs_page(url) else {}
        page = sources.http_get(url, timeout=20, max_bytes=5 * 1024 * 1024, **ua).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return "原文打不开，过一会儿再试"
    remote = remote_images(page, url).get(src)
    if not remote:
        return "原文里找不到这张图"
    root = root or vault.vault_root()
    full = os.path.join(root, *rel_dir.split("/"), *src.split("/"))
    if os.path.exists(full) and os.path.getsize(full) == 0:
        os.remove(full)   # 上次没下完留下的空文件
    ok, _ = download_images({f"{rel_dir}/{src}": remote}, root)
    return "" if ok or os.path.exists(full) else "图片还是下不下来"


# ---------------------------------------------------------------- 同一张图只存一份、同一篇只存一次（同手机 ClipImages、ClipLink）

_FORMAT_PARAMS = {"wx_fmt", "tp", "wxfrom", "from", "format", "wx_lazy", "wx_co"}
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/18.0 Mobile/15E148 Safari/604.1")   # 同手机 Clipper.userAgent；小红书给电脑浏览器的是登录页


def _is_wechat_host(host: str) -> bool:
    return host in ("mmbiz.qpic.cn", "mmbiz.qlogo.cn") or host.endswith((".mmbiz.qpic.cn", ".mmbiz.qlogo.cn"))


def _is_xhs_cdn(host: str) -> bool:
    return host.endswith("rednotecdn.com") or host.endswith("xhscdn.com")


def clean_image_url(s: str) -> str:
    r"""页面脚本里的地址还原：HTML 实体、\x26amp;、\x26、\u002F、\/（同手机 ClipImages.clean）。"""
    import html as html_lib
    t = s
    for _ in range(2):
        t = (t.replace("\\x26amp;", "&").replace("\\x26", "&").replace("\\u0026", "&")
             .replace("\\u002F", "/").replace("\\/", "/"))
        t = html_lib.unescape(t)
    return t.strip()


def xhs_file_id(url: str) -> str:
    """小红书图片的文件编号：最后一段「!」前面、至少 20 个字母数字；不是就返回空串（同手机 ClipImages.xhsFileID）。"""
    from urllib.parse import urlsplit
    p = urlsplit(url)
    if not _is_xhs_cdn((p.hostname or "").lower()):
        return ""
    parts = [x for x in p.path.split("/") if x]
    fid = parts[-1].split("!")[0] if parts else ""
    return fid if len(fid) >= 20 and re.fullmatch(r"[A-Za-z0-9_-]+", fid) else ""


def image_key(url: str) -> str:
    """同一张图的标识（同手机 ClipImages.key，逐字一样）。
    公众号（mmbiz.qpic.cn、mmbiz.qlogo.cn）：主机类别 + 路径到图片编号（后面的 /0、/640 是尺寸，参数不管）；
    小红书图床：文件编号；编号太短的用主机 + 路径 + 去掉格式参数的参数；
    别的网站：整个地址只去掉开头的 http:/https: 和 # 后面的部分。"""
    from urllib.parse import urlsplit
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if _is_wechat_host(host) and len(parts) >= 2:
        return ("qlogo" if host.endswith("qlogo.cn") else "qpic") + f"/{parts[0]}/{parts[1]}"
    if _is_xhs_cdn(host):
        fid = xhs_file_id(url)
        if fid:
            return "xhs/" + fid
        query = "&".join(q for q in p.query.split("&") if q and not (
            q.split("=", 1)[0].lower() in _FORMAT_PARAMS or q.split("=", 1)[0].lower().startswith(("imageview2", "imagemogr2"))))
        return f"//{host}{p.path}" + (f"?{query}" if query else "")
    t = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*:", "", url)
    return t.split("#", 1)[0]


def stable_name(url: str) -> str:
    """按 key 起的本地文件名（小红书签名地址、公众号页面数据里的图）：attachments/<key 的 sha1 前 16 位>.<扩展名>
    （同手机 ClipImages.stableName）。重新打开原文拿到的新地址也对得上。"""
    import hashlib
    ext = local_name(url).rsplit(".", 1)[1]
    return f"attachments/{hashlib.sha1(image_key(url).encode('utf-8')).hexdigest()[:16]}.{ext}"


def image_score(url: str) -> int:
    """同一张图几个版本的好坏：原图（公众号 /0）、不转 webp、不缩略的分高（同手机 ClipImages.score）。"""
    from urllib.parse import urlsplit
    p = urlsplit(url)
    host, q = (p.hostname or "").lower(), p.query.lower()
    s = 0
    if _is_wechat_host(host):
        parts = [x for x in p.path.split("/") if x]
        s += 2 if len(parts) >= 3 and parts[2] == "0" else 1 if len(parts) == 2 else 0
    if any(x in q for x in ("tp=webp", "wx_fmt=webp", "format/webp", "format=webp")):
        s -= 2
    if any(x in q for x in ("imageview2", "imagemogr2", "thumbnail")):
        s -= 3
    if p.scheme.lower() == "https":
        s += 1
    return s


def fallback_url(url: str) -> str:
    """公众号原图（尺寸段是 /0 或没有）下不来、太大时退回去下的 /640 版本；别的图返回空串（同手机 Clipper.fallbackURL）。"""
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    if not _is_wechat_host((p.hostname or "").lower()):
        return ""
    parts = [x for x in p.path.split("/") if x]
    if len(parts) == 2:
        parts.append("640")
    elif len(parts) >= 3 and parts[2] == "0":
        parts[2] = "640"
    else:
        return ""
    return urlunsplit((p.scheme, p.netloc, "/" + "/".join(parts), p.query, p.fragment))


def unique_image_urls(urls: list[str]) -> list[str]:
    """去重：每张图留第一次出现的位置，换成几个版本里最好的那个（同手机 ClipImages.unique）。"""
    order, best = [], {}
    for u in urls:
        k = image_key(u)
        if k not in best:
            order.append(k)
            best[k] = u
        elif image_score(u) > image_score(best[k]):
            best[k] = u
    return [best[k] for k in order]


_RE_IMAGE_BLOCK = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")


def drop_repeated_images(md: str) -> str:
    """单独一段的 ![](同一张图) 第二次出现时去掉（同手机 ClipImages.dropRepeatedImages）。"""
    seen, out = set(), []
    for b in md.split("\n\n"):
        m = _RE_IMAGE_BLOCK.fullmatch(b.strip())
        if m:
            if m.group(1) in seen:
                continue
            seen.add(m.group(1))
        out.append(b)
    return "\n\n".join(out)


def _local(key: str) -> str:
    for folder in CLIP_DIRS:
        if key.startswith(folder + "/"):
            return key[len(folder) + 1:]
    return key


def merge_variants(markdown: str, images: dict, drop_repeats: bool = False) -> tuple[str, dict]:
    """下载以前：同一张图的几个地址（image_key 一样）只留最好的那个，正文里的引用都改过去。
    旧版 Mac 应用交来的剪藏可能还带着这样的重复（新版在 Clipper 里已经去过一遍）。"""
    groups: dict[str, list[str]] = {}
    for key, url in images.items():
        groups.setdefault(image_key(url), []).append(key)
    md, out = markdown, dict(images)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        best = max(keys, key=lambda k: (image_score(images[k]), -keys.index(k)))
        for other in keys:
            if other != best:
                md = md.replace(f"]({_local(other)})", f"]({_local(best)})")
                out.pop(other, None)
    if drop_repeats:
        md = drop_repeated_images(md)
    return md, out


def merge_identical(markdown: str, images: dict, root: str, drop_repeats: bool = False) -> tuple[str, dict]:
    """下载以后：字节一样的图在两种情况下才当成同一张（同手机 ClipImages.mergeIdentical）：
    1. 两张图原地址的 image_key 一样；
    2. 纯图片的文章（drop_repeats=True）里，两张图那两段紧挨着（中间没有字，也没有别的图），
       并且这份字节只出现在这两个 key 上（三个以上不同 key 的图字节一样，多半是占位图，不按这条合并）。
    别的字节一样的图不合并：几张不同的图可能都下成了同一张占位图。合并的留正文里先出现的那张，引用改过去，images 里去掉别的。
    images 的键是笔记库里的相对路径（Spark/剪藏/attachments/x.jpg），正文里写的是 attachments/x.jpg。没下下来的图不动。
    纯图片的文章里同一张图出现第二次的那段去掉；普通文章两处引用都留着。
    合并掉的那份文件留在 attachments/ 里不删（别的剪藏可能也用着同一个文件名）。"""
    import hashlib
    blocks = markdown.split("\n\n")
    positions: dict[str, list[int]] = {}
    for i, b in enumerate(blocks):
        m = _RE_IMAGE_BLOCK.fullmatch(b.strip())
        if m:
            positions.setdefault(m.group(1), []).append(i)
    groups: dict[str, list[str]] = {}
    for key in images:
        try:
            with open(os.path.join(root, *key.split("/")), "rb") as f:
                data = f.read()
        except OSError:
            continue
        if data:
            groups.setdefault(hashlib.sha256(data).hexdigest(), []).append(key)

    def first_use(key: str) -> tuple:
        pos = positions.get(_local(key))
        return (pos[0] if pos else len(blocks) + 1, key)

    def related(a: str, b: str, distinct_keys: int) -> bool:
        if image_key(images[a]) == image_key(images[b]):
            return True
        return drop_repeats and distinct_keys == 2 and any(abs(i - j) == 1 for i in positions.get(_local(a), []) for j in positions.get(_local(b), []))

    md, out = markdown, dict(images)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        keys.sort(key=first_use)
        distinct_keys = len({image_key(images[k]) for k in keys})
        parent = list(range(len(keys)))

        def root_of(i: int) -> int:
            while parent[i] != i:
                i = parent[i]
            return i
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if related(keys[i], keys[j], distinct_keys):
                    a, b = root_of(i), root_of(j)
                    parent[max(a, b)] = min(a, b)   # 根总是块里最先出现的那张
        for i, other in enumerate(keys):
            r = root_of(i)
            if r != i:
                md = md.replace(f"]({_local(other)})", f"]({_local(keys[r])})")
                out.pop(other, None)
    if drop_repeats:
        md = drop_repeated_images(md)
    return md, out


def link_identity(url: str) -> str:
    """判断「已经在剪藏里了」用的标识（同手机 ClipLink.identity，逐字一样）：小红书是笔记编号；
    别的网站按字面：http、https 当一样，主机名小写，去掉 :80、:443；# 后面去掉，但「#/」「#!」开头的留着；
    参数原样留着、次序不变，只去掉 utm_*、fbclid、gclid、spm；不是 ASCII 的字按 UTF-8 写成 %XX。"""
    from urllib.parse import quote
    t = (url or "").strip()
    m = re.match(r"^(?i:https?)://([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$", t, re.S)
    if not m:
        return t
    host, path, query, frag = m.group(1).lower(), m.group(2), (m.group(3) or "")[1:], m.group(4) or ""
    bare = host.rsplit("@", 1)[-1].split(":")[0]
    if any(bare == d or bare.endswith("." + d) for d in ("xiaohongshu.com", "rednote.com")):
        nm = re.search(r"/(?:explore|discovery/item|item|notes?)/([0-9a-fA-F]{24})(?:[/?#]|$)", path + "/")
        if nm:
            return "xhs:" + nm.group(1).lower()
    for p in (":80", ":443"):
        if host.endswith(p):
            host = host[:-len(p)]
    kept = [q for q in query.split("&") if q and not (
        q.split("=", 1)[0].lower().startswith("utm_") or q.split("=", 1)[0].lower() in ("fbclid", "gclid", "spm"))]
    out = f"https://{host}{path}" + (f"?{'&'.join(kept)}" if kept else "")
    if frag.startswith(("#/", "#!")):
        out += frag
    return "".join(c if ord(c) < 128 else quote(c, safe="") for c in out)


def find_by_link(url: str, root: str = "", rel_dir: str = CLIPS_DIR) -> dict | None:
    """这个剪藏式文件夹里已经存过的同一篇（按 link_identity 比），没有返回 None。"""
    want = link_identity(url)
    if not want:
        return None
    return next((c for c in load_clips(root, rel_dir) if c.get("url") and link_identity(c["url"]) == want), None)


# ---------------------------------------------------------------- 小红书笔记页里的图（重新下载用，同手机 XHS）

def is_xhs_page(url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("xhslink.com", "xiaohongshu.com", "rednote.com"))


def _json_safe(s: str) -> str:
    """字符串外面的 undefined 换成 null（同手机 XHS.jsonSafe）。"""
    out, i, in_str, esc = [], 0, False, False
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif (s.startswith("undefined", i) and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] in "_$"))
              and (i + 9 == len(s) or not (s[i + 9].isalnum() or s[i + 9] in "_$"))):
            out.append("null")
            i += 9
            continue
        out.append(c)
        i += 1
    return "".join(out)


def xhs_page_images(page_html: str) -> list[str]:
    """小红书笔记页 window.__INITIAL_STATE__ 里的图，按笔记里的顺序（取图规则同手机 XHS.bestImage）。"""
    start = page_html.find("window.__INITIAL_STATE__")
    brace = page_html.find("{", start) if start >= 0 else -1
    end = page_html.find("</script>", brace) if brace >= 0 else -1
    if end < 0:
        return []
    try:
        state = json.loads(_json_safe(page_html[brace:end].strip().rstrip(";")))
    except ValueError:
        return []
    note = (((state.get("noteData") or {}).get("data") or {}).get("noteData")) or {}
    if not note:
        for v in ((state.get("note") or {}).get("noteDetailMap") or {}).values():
            if isinstance(v, dict) and v.get("note"):
                note = v["note"]
                break
    out = []
    for img in note.get("imageList") or []:
        if not isinstance(img, dict):
            continue
        info = [x for x in img.get("infoList") or [] if isinstance(x, dict)]
        dft = next((x for x in info if re.search(r"DFT|default", str(x.get("imageScene") or ""), re.I)), {})
        cand = [img.get("url"), dft.get("url"), img.get("urlDefault"), info[0].get("url") if info else None]
        u = next((clean_image_url(c) for c in cand if isinstance(c, str) and c), "")
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http://"):
            u = "https://" + u[len("http://"):]
        if u.startswith("https://"):
            out.append(u)
    return out


def remote_images(page_html: str, base: str) -> dict:
    """重新下载时在原文里找图：{本地文件名: 地址}。普通网页的 <img> 按地址起名（page_images）；
    小红书笔记、公众号图片消息页面数据里的图按 key 起名（stable_name），跟手机存的时候一样。"""
    out = page_images(page_html, base)
    if is_xhs_page(base):
        for u in xhs_page_images(page_html):
            out.setdefault(stable_name(u), u)
    for m in re.finditer(r"""cdn_url["']?\s*[:=]\s*["']([^"']+)["']""", page_html, re.I):
        u = clean_image_url(m.group(1))
        u = "https:" + u if u.startswith("//") else u
        if u.startswith(("http://", "https://")):
            out.setdefault(stable_name(u), u)
    return out


def refresh_signed_images(images: dict, clip_url: str, root: str = "") -> int:
    """小红书的图地址带签名，过一阵会失效：images 里还没下下来的，重新打开笔记拿新地址再下一次（文件名按文件编号起，对得上）。
    不是小红书、都下好了、笔记打不开都什么也不做。返回这次补下的张数。"""
    from core import sources
    root = root or vault.vault_root()
    missing = [k for k in images if not os.path.exists(os.path.join(root, *k.split("/")))]
    if not missing or not is_xhs_page(clip_url or ""):
        return 0
    try:
        page = sources.http_get(clip_url, timeout=20, max_bytes=5 * 1024 * 1024, user_agent=IPHONE_UA).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return 0
    fresh = remote_images(page, clip_url)
    renewed = {k: fresh[_local(k)] for k in missing if fresh.get(_local(k)) and fresh[_local(k)] != images[k]}
    return download_images(renewed, root)[0] if renewed else 0
