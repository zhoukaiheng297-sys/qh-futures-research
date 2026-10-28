"""一键部署网站到 Hugging Face Spaces（不需要 git）。

准备（都写在 .env 里）：
  HF_TOKEN          Write 权限的 token（创建 Space、上传代码）
  HF_READ_TOKEN     可选，Read 权限的 token；网站只用它读私有数据集（不填就用 HF_TOKEN）
  HF_DATASET_REPO   存网页数据的私有数据集，如 用户名/futures-site-data
  SITE_PASSCODE     网站访问口令，至少 8 位

运行：
  python deploy_hf.py                 部署到 你的用户名/qh-futures
  python deploy_hf.py 空间名           或指定空间名（也可以写 用户名/空间名）

网站代码是公开的（Space 公开才能分享网址），数据在私有数据集里、口令存成加密 Secret，都不公开。
部署前先运行一次 python main.py update 和 python main.py publish，网站启动时才有数据可读。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    from huggingface_hub import HfApi
except ImportError:
    sys.exit("先安装：pip install huggingface_hub")

IGNORE = [".env", ".venv/*", "data/*", "**/__pycache__/*", ".claude/*", ".git/*", ".github/*", "tests/*",
          ".pytest_cache/*", ".Rhistory", ".RData", "docs/*", "deploy/*"]
README_SPACE = """---
title: QH期货
emoji: 📈
colorFrom: red
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: 国内期货多因子信号与可靠性说明（口令访问）
---

访问需要口令。数据来自交易所和公开网站，仅供研究，不构成投资建议。
"""


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    name = args[0] if args else "qh-futures"
    token = os.getenv("HF_TOKEN")
    repo = os.getenv("HF_DATASET_REPO")
    passcode = os.getenv("SITE_PASSCODE", "")
    if not token:
        sys.exit("请在 .env 里设置 HF_TOKEN（Write 权限）。")
    if not repo:
        sys.exit("请在 .env 里设置 HF_DATASET_REPO（存网页数据的私有数据集）。")
    if len(passcode) < 8:
        sys.exit("SITE_PASSCODE 至少 8 位，建议用一串随机字符。")
    api = HfApi(token=token)
    me = api.whoami()
    repo_id = name if "/" in name else f"{me['name']}/{name}"

    print(f"创建/更新 Space：{repo_id}")
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    api.add_space_secret(repo_id, "SITE_PASSCODE", passcode)
    api.add_space_secret(repo_id, "HF_TOKEN", os.getenv("HF_READ_TOKEN") or token)
    api.add_space_variable(repo_id, "HF_DATASET_REPO", repo)
    print("  已设置 Secrets：SITE_PASSCODE、HF_TOKEN（读数据集用）；变量：HF_DATASET_REPO")

    readme = ROOT / "README.md"
    original = readme.read_text(encoding="utf-8") if readme.exists() else ""
    try:
        readme.write_text(README_SPACE, encoding="utf-8")   # Space 需要带配置头的 README
        api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=ROOT, ignore_patterns=IGNORE,
                          delete_patterns=["*"], commit_message="部署 QH期货")
    finally:
        readme.write_text(original, encoding="utf-8")

    host = repo_id.replace("/", "-").replace("_", "-").replace(".", "-").lower()
    url = f"https://{host}.hf.space"
    print(f"上传完成，构建进度：https://huggingface.co/spaces/{repo_id}")
    print("等待构建（通常 2~4 分钟）…")
    deadline = time.time() + 15 * 60
    last = None
    while time.time() < deadline:
        stage = api.get_space_runtime(repo_id).stage
        if stage != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {stage}")
            last = stage
        if stage == "RUNNING":
            print(f"\n网站已上线：{url}\n把网址和口令一起发给信任的人。")
            return
        if stage in {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"}:
            sys.exit(f"构建失败，去 https://huggingface.co/spaces/{repo_id} 看日志。")
        time.sleep(15)
    print("还在构建，稍后去 Space 页面看看。")


if __name__ == "__main__":
    main()
