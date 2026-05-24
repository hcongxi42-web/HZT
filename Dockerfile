FROM python:3.11-slim
WORKDIR /app
COPY news_fetcher.py .
EXPOSE 8766
CMD ["python", "news_fetcher.py"]
