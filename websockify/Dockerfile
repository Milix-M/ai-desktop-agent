FROM python:3.12-alpine
RUN pip install --no-cache-dir websockify
EXPOSE 5900
ENTRYPOINT ["websockify"]
CMD ["--web", "/usr/local/lib/python3.12/site-packages/websockify/web", "0.0.0.0:5900", "vm:5900"]
