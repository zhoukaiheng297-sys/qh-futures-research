FROM python:3.12-slim

# Hugging Face Spaces 用 uid 1000 运行容器
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements-web.txt .
RUN pip install --no-cache-dir --user -r requirements-web.txt

# 网站只读 site.db：启动时从私有数据集（HF_DATASET_REPO）下载，之后每小时检查更新
COPY --chown=user futures_analyst/__init__.py futures_analyst/config.py futures_analyst/
COPY --chown=user web web

EXPOSE 7860
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "7860"]
