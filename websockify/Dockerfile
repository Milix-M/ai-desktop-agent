FROM python:3.12-alpine
RUN pip install --no-cache-dir websockify
EXPOSE 5900
ENTRYPOINT ["websockify", "--auth-plugin=ExpectOrigin", "--auth-source=http://localhost:3000 http://127.0.0.1:3000"]
CMD ["--web", "/usr/local/lib/python3.12/site-packages/websockify/web", "0.0.0.0:5900", "vm:5900"]
