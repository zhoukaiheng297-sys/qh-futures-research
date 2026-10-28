"""和 Hugging Face 私有数据集同步。

- upload()：把 site.db（网页数据）上传；加 raw=True 时连原始库 futures.db 一起上传（给 GitHub Actions 下次接着用）
- download_raw()：在新机器（如 GitHub Actions）上先拉回原始库，日更只补最近几天
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from .config import DB_PATH, HF_DATASET_REPO, SITE_DB_PATH


def _api():
    token = os.getenv("HF_TOKEN")
    if not (token and HF_DATASET_REPO):
        sys.exit("需要在 .env 里设置 HF_TOKEN（Write 权限）和 HF_DATASET_REPO（如 用户名/futures-site-data）")
    from huggingface_hub import HfApi

    return HfApi(token=token), token


def _checkpoint(path: Path) -> None:
    """把 WAL 里的改动合并回主文件，上传单个文件就是完整的库。"""
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def upload(raw: bool = False) -> None:
    api, _ = _api()
    if not SITE_DB_PATH.exists():
        sys.exit("还没有 site.db，先运行 python main.py update")
    api.create_repo(HF_DATASET_REPO, repo_type="dataset", private=True, exist_ok=True)
    api.upload_file(path_or_fileobj=str(SITE_DB_PATH), path_in_repo="site.db", repo_id=HF_DATASET_REPO,
                    repo_type="dataset", commit_message="更新网页数据")
    if raw and DB_PATH.exists():
        _checkpoint(DB_PATH)
        api.upload_file(path_or_fileobj=str(DB_PATH), path_in_repo="raw/futures.db", repo_id=HF_DATASET_REPO,
                        repo_type="dataset", commit_message="更新原始数据")
    print(f"已上传到 https://huggingface.co/datasets/{HF_DATASET_REPO}（私有）")


def download_raw() -> None:
    _, token = _api()
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError

    try:
        path = hf_hub_download(HF_DATASET_REPO, "raw/futures.db", repo_type="dataset", token=token)
    except EntryNotFoundError:
        print("数据集里还没有原始库，将从头回补（首次约 1~2 小时）")
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_bytes(Path(path).read_bytes())
    print(f"已下载原始库到 {DB_PATH}")
