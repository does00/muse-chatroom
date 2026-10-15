FROM python:3.12-slim
WORKDIR /app
COPY chatroom.py .
RUN mkdir -p /data
ENV PORT=3007 DATA_DIR=/data PRESENCE_TTL=60
EXPOSE 3007
CMD ["python3", "chatroom.py"]
